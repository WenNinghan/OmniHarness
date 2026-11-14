"""V10 owner-lifetime eligibility; it never authorizes model/tool execution.

Only the opt-in v10 preview uses this listener. A notification is a hint: the
owner verifies the authenticated origin and Run under its own locked session.
Eligibility is process-local, one-shot, and is discarded on either connection
loss. This component is deliberately not wired to the correction dispatcher.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass

import psycopg
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from .checkpoint_fencing import _conninfo
from .m3_worker import _checkpoint_url
from .models import (
    AgentRun,
    Approval,
    Clarification,
    Message,
    OperationRecord,
    Run,
    RunBudget,
    RunBudgetReservation,
    RunCommand,
    RunEvent,
    ToolOperation,
    Workspace,
    WorkspaceLease,
)

_CHALLENGE_CHANNEL = "omni_v10_owner_challenge"
_CREATED_CHANNEL = "omni_v10_owner_created"
_REPLY = re.compile(r"^v10r_[a-f0-9]{32}$")
_LIMIT = 10000
_MAX_PAYLOAD = 4096


@dataclass(frozen=True)
class _ConsumedCorrectionReceipt:
    """Process-local identity evidence, never a reservation or dispatch grant."""

    run_id: str
    origin_json: str
    owner: object
    owner_connection: object
    listener_connection: object
    ticket: object
    root_id: str
    operation_id: str
    binding_fingerprint: str
    closure_fingerprint: str


@dataclass(frozen=True)
class _PreparedCorrectionPhase:
    """Live-owner association only; never recoverable from durable records."""

    run_id: str
    phase_id: str
    ticket: object
    root_id: str
    owner: object
    owner_connection: object
    listener_connection: object
    execution_saver: object


def request_owner_challenge(settings):
    """Best-effort synchronous API challenge; failure never blocks Run creation."""
    from .v10_owner_auth import new_challenge, verify_challenge_response

    if not settings.v10_preview:
        return None
    try:
        with psycopg.connect(_conninfo(_checkpoint_url(settings.database_url)), autocommit=True,
                             connect_timeout=1) as conn:
            import secrets
            reply = "v10r_" + secrets.token_hex(16)
            challenge = new_challenge(reply)
            conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(reply)))
            conn.execute("SELECT pg_notify(%s,%s)", (_CHALLENGE_CHANNEL, json.dumps(challenge)))
            deadline = time.monotonic() + 0.7
            while time.monotonic() < deadline:
                notes = list(conn.notifies(timeout=max(0, deadline - time.monotonic()), stop_after=1))
                for note in notes:
                    if note.channel != reply or len(note.payload) > _MAX_PAYLOAD:
                        continue
                    try:
                        owner = verify_challenge_response(settings, json.loads(note.payload), challenge=challenge)
                        if owner is not None:
                            return owner
                    except (ValueError, TypeError, KeyError):
                        continue
    except Exception:
        return None
    return None


class V10OwnerListener:
    """Process-local pending/accepted/consumed/tainted qualification registry."""

    def __init__(self, worker, connection, identity, listener_pid):
        from .v10_owner_auth import OwnerEventRegistry

        self.worker, self.connection, self.identity = worker, connection, identity
        self.listener_pid = listener_pid
        self.alive = True
        self.events = OwnerEventRegistry(identity, worker.settings, max_events=_LIMIT)
        self.pending, self.committing, self.accepted = {}, {}, {}
        self.consumed, self.tainted = set(), set()
        self._correction_receipts = {}
        self._correction_phases = {}
        self.arrival = asyncio.Event()
        self.task = None

    @classmethod
    async def start(cls, worker):
        from .v10_owner_auth import new_owner_identity

        if (not worker.settings.v10_preview or worker.engine.dialect.name != "postgresql"
                or worker.lock is None or worker.lock.lost):
            return None
        connection = None
        try:
            with worker.transaction() as db:
                row = db.execute(text("SELECT pg_backend_pid() AS pid, backend_start FROM pg_stat_activity "
                                      "WHERE pid=pg_backend_pid()")).mappings().one()
                identity = new_owner_identity(row["pid"], row["backend_start"], worker.settings.instance_id)
            connection = await psycopg.AsyncConnection.connect(_conninfo(_checkpoint_url(worker.settings.database_url)),
                                                                 autocommit=True, connect_timeout=1)
            await connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(_CHALLENGE_CHANNEL)))
            await connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(_CREATED_CHANNEL)))
            listener_pid = (await (await connection.execute("SELECT pg_backend_pid()")).fetchone())[0]
            instance = cls(worker, connection, identity, listener_pid)
            instance.task = asyncio.create_task(instance._listen())
            return instance
        except Exception:
            if connection is not None:
                with contextlib.suppress(Exception):
                    await connection.close()
            return None

    def _invalidate(self):
        self.alive = False
        self.tainted.update(self.pending)
        self.tainted.update(self.committing)
        self.tainted.update(self.accepted)
        self.tainted.update(self._correction_receipts)
        self.tainted.update(self._correction_phases)
        self.pending.clear()
        self.committing.clear()
        self.accepted.clear()
        self._correction_receipts.clear()
        self._correction_phases.clear()

    async def close(self):
        self._invalidate()
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        with contextlib.suppress(Exception):
            await self.connection.close()

    async def _listen(self):
        from .v10_owner_auth import make_challenge_response

        try:
            while self.alive:
                notes = [n async for n in self.connection.notifies(timeout=0.25, stop_after=1)]
                # Another task may hold the owner's synchronous transaction
                # while awaiting a pinned checkpoint read. Never start a
                # second transaction on that same connection. Notification
                # authentication resumes after the owner transaction exits.
                while (self.alive and self.worker.lock is not None and self.worker.lock.connection is not None
                       and self.worker.lock.connection.in_transaction()):
                    await asyncio.sleep(0.005)
                if not self.alive:
                    break
                self.worker.lock.check()
                for note in notes:
                    if len(note.payload) > _MAX_PAYLOAD:
                        continue
                    try:
                        data = json.loads(note.payload)
                        if note.channel == _CHALLENGE_CHANNEL:
                            if not isinstance(data, dict) or not _REPLY.fullmatch(str(data.get("reply_to", ""))):
                                continue
                            response = make_challenge_response(self.worker.settings, self.identity, data)
                            await self.connection.execute("SELECT pg_notify(%s,%s)",
                                (data["reply_to"], json.dumps(response, separators=(",", ":"))))
                        elif note.channel == _CREATED_CHANNEL:
                            self._register(data)
                    except (ValueError, TypeError, KeyError):
                        continue
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._invalidate()

    def _pristine(self, db, run, origin):
        from .v10_owner_auth import verify_run_origin

        if (run is None or run.status != "queued" or run.started_at is not None
                or run.workflow_version != "stage-agent-v10" or not run.workspace_id or run.outcome):
            return False
        user = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "user"))
        assistant = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "assistant"))
        budget = db.get(RunBudget, run.id)
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run.id)))
        if (not user or not assistant or not budget or budget.model_calls or budget.tool_calls or budget.revisions
                or budget.active_seconds != 0 or budget.active_segment_open is not False
                or len(events) != 1 or events[0].event_type != "run.created"
                or not isinstance(events[0].payload, dict)
                or events[0].payload.get("message_id") != assistant.id
                or (run.config_snapshot or {}).get("v10_owner_origin") != origin):
            return False
        for model, key in ((AgentRun, AgentRun.id), (ToolOperation, ToolOperation.id),
                           (OperationRecord, OperationRecord.operation_id), (Clarification, Clarification.id),
                           (RunCommand, RunCommand.id), (RunBudgetReservation, RunBudgetReservation.invocation_id)):
            if db.scalar(select(key).where(model.run_id == run.id).limit(1)):
                return False
        if db.scalar(select(Approval.id).join(ToolOperation).where(ToolOperation.run_id == run.id).limit(1)):
            return False
        return verify_run_origin(self.worker.settings, origin, owner=self.identity, run=run, message=user)

    def _register(self, origin):
        """Authenticate before consuming any bounded in-memory identity."""
        from .v10_owner_auth import _authenticated_origin

        if not self.alive or not isinstance(origin, dict):
            return
        run_id, event_id = origin.get("run_id"), origin.get("event_id")
        if not isinstance(run_id, str) or not isinstance(event_id, str):
            return
        # Do not take a Run lock or use the owner connection for unauthenticated
        # traffic. A role that can NOTIFY still cannot manufacture a valid MAC.
        if not _authenticated_origin(self.worker.settings, origin, self.identity):
            return
        with self.worker.transaction() as db:
            try:
                with db.begin_nested():
                    # A contended or abandoned Run cannot hold up the owner
                    # loop indefinitely; rollback only this savepoint.
                    db.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
                    user = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
                    if not self.events.consume(origin, run=run, message=user) or run_id in self.tainted:
                        return
                    if not self._pristine(db, run, origin):
                        self.tainted.add(run_id)
                        return
            except SQLAlchemyError:
                self.claim_failed(run_id)
                return
        self.pending[run_id] = origin
        self.arrival.set()

    async def wait_before_claim(self):
        """Bound the commit/NOTIFY race; never infer eligibility from a scan."""
        if not self.alive:
            return
        from .services import ACTIVE_STATES
        from .v10_owner_auth import _authenticated_origin
        try:
            with self.worker.transaction() as db:
                active = select(Run.conversation_id).where(Run.status.in_(ACTIVE_STATES))
                candidates = db.scalars(select(Run).where(Run.status == "queued",
                    Run.conversation_id.not_in(active)).order_by(Run.created_at, Run.ordinal)
                    .with_for_update(skip_locked=True))
                candidate = None
                for run in candidates:
                    if run.workspace_id:
                        workspace = db.get(Workspace, run.workspace_id)
                        if not workspace or not workspace.enabled:
                            break
                        lease = db.get(WorkspaceLease, run.workspace_id)
                        if lease and lease.run_id != run.id:
                            continue
                    # Only the first ordinarily claimable Run matters.  A scan
                    # schedules a short wait, never grants owner presence.
                    origin = (run.config_snapshot or {}).get("v10_owner_origin")
                    if (run.workflow_version == "stage-agent-v10"
                            and isinstance(origin, dict) and origin.get("run_id") == run.id
                            and origin.get("workspace_id") == run.workspace_id
                            and _authenticated_origin(self.worker.settings, origin, self.identity)):
                        candidate = run.id
                    break
            if not candidate or candidate in self.pending or candidate in self.tainted:
                return
            deadline = asyncio.get_running_loop().time() + 0.1
            while self.alive and candidate not in self.pending and candidate not in self.tainted:
                self.arrival.clear()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self.arrival.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
        except Exception:
            self._invalidate()

    def before_claim(self, db, run):
        """Called under the same Run lock used by the ordinary claim path."""
        if run.workflow_version != "stage-agent-v10":
            return
        if len(self.tainted) >= _LIMIT:
            self._invalidate()
            return
        origin = self.pending.pop(run.id, None)
        if not self.alive or run.id in self.tainted or origin is None or not self._pristine(db, run, origin):
            self.tainted.add(run.id)
            return
        self.committing[run.id] = origin

    def claim_failed(self, run_id):
        if run_id:
            self.pending.pop(run_id, None)
            self.committing.pop(run_id, None)
            self.accepted.pop(run_id, None)
            self._correction_receipts.pop(run_id, None)
            self._correction_phases.pop(run_id, None)
            self.tainted.add(run_id)

    async def _live(self):
        if not self.alive:
            return False
        if self.worker.lock is None or self.worker.lock.lost:
            self._invalidate()
            return False
        try:
            self.worker.lock.check()
            pid = (await (await self.connection.execute("SELECT pg_backend_pid()")).fetchone())[0]
            if pid != self.listener_pid or not self.alive:
                raise RuntimeError("Listener identity changed")
            return True
        except Exception:
            self._invalidate()
            return False

    async def confirm_claim(self, run_id):
        """Grant in memory only after claim commit and both sessions recheck."""
        from .m4_services import validate_workspace_lease
        from .v10_owner_auth import verify_run_origin

        owner = self.worker.lock
        owner_connection = getattr(owner, "connection", None)
        listener_connection = self.connection

        def still_eligible():
            return (self.alive and getattr(self.worker, "_v10_owner_listener", None) is self
                    and self.worker.lock is owner and owner is not None and not owner.lost
                    and owner_connection is not None and owner.connection is owner_connection
                    and self.connection is listener_connection and run_id not in self.tainted
                    and run_id not in self.consumed and run_id not in self.accepted)

        origin = self.committing.pop(run_id, None)
        if origin is None or not still_eligible() or not await self._live() or not still_eligible():
            self.claim_failed(run_id)
            return False
        try:
            with self.worker.transaction() as db:
                run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
                user = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
                valid = bool(run and user and run.status == "running" and run.started_at
                    and validate_workspace_lease(db, run.workspace_id, run.id)
                    and (run.config_snapshot or {}).get("v10_owner_origin") == origin
                    and verify_run_origin(self.worker.settings, origin, owner=self.identity, run=run, message=user))
            if not valid or not await self._live() or not still_eligible():
                self.claim_failed(run_id)
                return False
        except Exception:
            self.claim_failed(run_id)
            return False
        self.accepted[run_id] = origin
        return True

    async def consume_owner_presence(self, run_id):
        """Consume a presence fact once; this is not closure or dispatch permission."""
        from .m4_services import validate_workspace_lease
        from .v10_owner_auth import verify_run_origin

        owner = self.worker.lock
        owner_connection = getattr(owner, "connection", None)
        listener_connection = self.connection

        def still_eligible():
            return (self.alive and getattr(self.worker, "_v10_owner_listener", None) is self
                    and self.worker.lock is owner and owner is not None and not owner.lost
                    and owner_connection is not None and owner.connection is owner_connection
                    and self.connection is listener_connection and run_id not in self.tainted
                    and run_id in self.consumed and run_id not in self.accepted)

        if run_id in self.tainted or run_id in self.consumed:
            self.claim_failed(run_id)
            return False
        origin = self.accepted.pop(run_id, None)
        self.consumed.add(run_id)
        if origin is None or not still_eligible() or not await self._live() or not still_eligible():
            self.claim_failed(run_id)
            return False
        try:
            with self.worker.transaction() as db:
                run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
                user = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
                valid = bool(run and user and run.status == "running"
                    and validate_workspace_lease(db, run.workspace_id, run.id)
                    and (run.config_snapshot or {}).get("v10_owner_origin") == origin
                    and verify_run_origin(self.worker.settings, origin, owner=self.identity, run=run, message=user))
            if valid and await self._live() and still_eligible():
                return True
        except Exception:
            pass
        self.claim_failed(run_id)
        return False

    def discard(self, run_id):
        """A returned/paused Run cannot silently reacquire this lifetime grant."""
        if (run_id in self.pending or run_id in self.committing or run_id in self.accepted
                or run_id in self._correction_receipts or run_id in self._correction_phases):
            self.claim_failed(run_id)
