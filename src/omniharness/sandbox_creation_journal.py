"""Candidate durable create journal; not connected to the production executor.

This journal records intent before an external request. It does not establish
that an unknown request stopped, verify sandbox reclamation, or release slots.
The caller must protect its storage and authenticate all inputs.
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


class CreationBindingError(ValueError):
    """A request does not match the immutable recorded identity."""


@dataclass(frozen=True)
class CreationBinding:
    instance_id: str
    run_id: str
    operation_id: str
    fingerprint: str
    service_fingerprint: str

    def __post_init__(self):
        for value in (self.instance_id, self.run_id, self.operation_id):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
                raise CreationBindingError("Invalid creation identity")
        for value in (self.fingerprint, self.service_fingerprint):
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise CreationBindingError("Invalid creation fingerprint")


@dataclass(frozen=True)
class CreationRecord:
    binding: CreationBinding
    state: str
    sandbox_id: str | None


class CreationJournal:
    """Atomic, one-dispatch-per-binding journal with conservative crash semantics.

    DISPATCHED may mean a request was never sent, is still running, or has
    completed without a durable receipt. It is never proof of resource absence.
    """

    def __init__(self, path: Path, journal_id: str):
        self.path = Path(path)
        if not isinstance(journal_id, str) or not re.fullmatch(r"[a-f0-9]{64}", journal_id):
            raise CreationBindingError("Invalid journal identity")
        self.journal_id = journal_id
        with closing(self._open_existing()) as db:
            self._verify(db)

    @classmethod
    def initialize(cls, path: Path, journal_id: str):
        """Explicitly provision a new journal; never replace an existing file."""
        if not isinstance(journal_id, str) or not re.fullmatch(r"[a-f0-9]{64}", journal_id):
            raise CreationBindingError("Invalid journal identity")
        path = Path(path).resolve()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE journal_identity (id TEXT NOT NULL PRIMARY KEY)")
            db.execute("INSERT INTO journal_identity VALUES (?)", (journal_id,))
            db.execute("""CREATE TABLE IF NOT EXISTS sandbox_creation_journal (
                instance_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                run_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                service_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('DISPATCHED', 'CREATED')),
                sandbox_id TEXT,
                PRIMARY KEY(instance_id, operation_id),
                CHECK ((state = 'DISPATCHED' AND sandbox_id IS NULL) OR
                       (state = 'CREATED' AND sandbox_id IS NOT NULL))
            )""")
            db.commit()
        return cls(path, journal_id)

    def _open_existing(self):
        uri = self.path.resolve().as_uri() + "?mode=rw"
        db = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _verify(self, db):
        try:
            rows = db.execute("SELECT id FROM journal_identity").fetchall()
        except sqlite3.Error:
            raise CreationBindingError("Journal identity is unavailable") from None
        if rows != [(self.journal_id,)]:
            raise CreationBindingError("Journal identity changed")

    def _connect(self):
        db = self._open_existing()
        try:
            self._verify(db)
            return db
        except BaseException:
            db.close()
            raise

    @staticmethod
    def _fetch(db, binding):
        row = db.execute("SELECT run_id, fingerprint, service_fingerprint, state, sandbox_id "
                         "FROM sandbox_creation_journal WHERE instance_id=? AND operation_id=?",
                         (binding.instance_id, binding.operation_id)).fetchone()
        if row is None:
            return None
        if row[:3] != (binding.run_id, binding.fingerprint, binding.service_fingerprint):
            raise CreationBindingError("Creation binding differs from its journal")
        return CreationRecord(binding, row[3], row[4])

    def claim_dispatch(self, binding: CreationBinding) -> bool:
        """Persist intent and return True once; False never authorizes a retry."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                claimed = self._claim_in_transaction(db, binding)
                db.commit()
                return claimed
            except BaseException:
                db.rollback()
                raise

    @classmethod
    def _claim_in_transaction(cls, db, binding):
        if cls._fetch(db, binding) is not None:
            return False
        db.execute("INSERT INTO sandbox_creation_journal VALUES (?, ?, ?, ?, ?, 'DISPATCHED', NULL)",
                   (binding.instance_id, binding.operation_id, binding.run_id,
                    binding.fingerprint, binding.service_fingerprint))
        return True

    @classmethod
    def _created_in_transaction(cls, db, binding, sandbox_id):
        if not isinstance(sandbox_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sandbox_id):
            raise CreationBindingError("Invalid sandbox identity")
        current = cls._fetch(db, binding)
        if current is None:
            raise CreationBindingError("Creation was not dispatched")
        if current.state == "CREATED":
            if current.sandbox_id != sandbox_id:
                raise CreationBindingError("Sandbox identity differs from its journal")
        else:
            db.execute("UPDATE sandbox_creation_journal SET state='CREATED', sandbox_id=? "
                       "WHERE instance_id=? AND operation_id=?",
                       (sandbox_id, binding.instance_id, binding.operation_id))
        return CreationRecord(binding, "CREATED", sandbox_id)

    def record_created(self, binding: CreationBinding, sandbox_id: str) -> CreationRecord:
        """Persist a trusted caller's creation response; this is not a deletion receipt."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = self._created_in_transaction(db, binding, sandbox_id)
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    def lookup(self, binding: CreationBinding) -> CreationRecord | None:
        with closing(self._connect()) as db:
            return self._fetch(db, binding)
