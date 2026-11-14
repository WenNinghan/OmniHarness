"""Durable request allowance for the explicitly authorized evaluation campaign.

This is an experiment ledger, not another Run scheduler. Unknown external calls
retain their whole reservation until evidence permits explicit reconciliation.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

AUTHORIZATION = "m4-optimization-20261109"


class EvaluationBudgetError(RuntimeError):
    pass


class EvaluationBudgetExhausted(EvaluationBudgetError):
    pass


class BudgetLedger:
    def __init__(self, path: str | Path, *, authorization_id: str = AUTHORIZATION, limit: int = 1500):
        if type(limit) is not int or not 1 <= limit <= 1500 or authorization_id != AUTHORIZATION:
            raise EvaluationBudgetError("The approved authorization and ceiling must be explicit")
        self.path, self.authorization_id, self.limit = Path(path), authorization_id, limit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS campaign (id TEXT PRIMARY KEY, ceiling INTEGER NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY, maximum INTEGER NOT NULL, purpose TEXT NOT NULL,
                state TEXT NOT NULL, actual INTEGER, metadata TEXT NOT NULL DEFAULT '{}')""")
            db.execute("INSERT OR IGNORE INTO campaign VALUES (?, ?)", (authorization_id, limit))
            if db.execute("SELECT id, ceiling FROM campaign").fetchall() != [(authorization_id, limit)]:
                raise EvaluationBudgetError("Existing campaign authorization or ceiling differs")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _committed(db):
        return db.execute("SELECT COALESCE(SUM(CASE WHEN state='finished' THEN actual ELSE maximum END),0) "
                          "FROM reservations").fetchone()[0]

    def reserve(self, identity: str, max_calls: int, purpose: str, *, metadata: dict | None = None) -> bool:
        if (not identity or len(identity) > 200 or type(max_calls) is not int
                or not 1 <= max_calls <= 50 or not purpose or len(purpose) > 120):
            raise EvaluationBudgetError("Invalid evaluation reservation")
        encoded = json.dumps(metadata or {}, sort_keys=True, allow_nan=False)
        if len(encoded) > 4096:
            raise EvaluationBudgetError("Reservation metadata is too large")
        with self._transaction() as db:
            previous = db.execute("SELECT maximum,purpose,metadata FROM reservations WHERE id=?", (identity,)).fetchone()
            if previous:
                if previous != (max_calls, purpose, encoded):
                    raise EvaluationBudgetError("Reservation identity cannot change its meaning")
                return False
            if self._committed(db) + max_calls > self.limit:
                raise EvaluationBudgetExhausted("NOT_RUN_BUDGET: insufficient unreserved allowance")
            db.execute("INSERT INTO reservations(id,maximum,purpose,state,metadata) VALUES(?,?,?,'reserved',?)",
                       (identity, max_calls, purpose, encoded))
            return True

    def start(self, identity: str) -> bool:
        with self._transaction() as db:
            return db.execute("UPDATE reservations SET state='active' WHERE id=? AND state='reserved'",
                              (identity,)).rowcount == 1

    def finish(self, identity: str, actual_calls: int) -> None:
        with self._transaction() as db:
            row = db.execute("SELECT maximum,state,actual FROM reservations WHERE id=?", (identity,)).fetchone()
            if not row or type(actual_calls) is not int or not 0 <= actual_calls <= row[0]:
                raise EvaluationBudgetError("Actual usage is not within its reserved allowance")
            if row[1] == "finished":
                if row[2] != actual_calls:
                    raise EvaluationBudgetError("A completed ledger entry is immutable")
                return
            if row[1] == "reserved" and actual_calls:
                raise EvaluationBudgetError("A request must be started before consumption")
            # Calling finish on unknown is explicit evidence-based reconciliation;
            # it never authorizes dispatching the same request again.
            db.execute("UPDATE reservations SET state='finished', actual=? WHERE id=?", (actual_calls, identity))

    def unknown(self, identity: str) -> None:
        with self._transaction() as db:
            row = db.execute("SELECT state FROM reservations WHERE id=?", (identity,)).fetchone()
            if not row or row[0] == "finished":
                raise EvaluationBudgetError("Cannot mark a missing/completed reservation unknown")
            db.execute("UPDATE reservations SET state='unknown' WHERE id=?", (identity,))

    def snapshot(self) -> dict:
        with self._transaction() as db:
            rows = db.execute("SELECT id,maximum,purpose,state,actual,metadata FROM reservations ORDER BY rowid").fetchall()
            committed = self._committed(db)
            return {"authorization_id": self.authorization_id, "limit": self.limit,
                    "committed": committed, "available": self.limit - committed,
                    "observed_calls": sum(row[4] or 0 for row in rows),
                    "reservations": [dict(zip(("id", "maximum", "purpose", "state", "actual_calls", "metadata"),
                                              (*row[:5], json.loads(row[5])), strict=True)) for row in rows]}

    @contextmanager
    def runner_lock(self):
        """One benchmark driver per campaign; OS releases the lock after a crash."""
        handle = self.path.with_suffix(".runner.lock").open("a+b")
        acquired = False
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise EvaluationBudgetError("Another evaluation runner holds this campaign") from exc
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
