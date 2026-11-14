"""Isolated PG owner/listener experiment; never dispatches model or tools."""
import asyncio
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

import psycopg
from sqlalchemy import select, text
from v10_owner_handshake_protocol import run_digest, tag

from omniharness.checkpoint_fencing import _conninfo
from omniharness.m3_worker import _checkpoint_url
from omniharness.m4_services import validate_workspace_lease
from omniharness.models import (
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
)
from omniharness.settings import Settings
from omniharness.worker import Worker, WorkerLock


def log(kind, run_id=None):
    with Path(os.environ["V10_HANDSHAKE_LOG"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": kind, "run_id": run_id}) + "\n")


async def main():
    settings = Settings(_env_file=None, database_url=os.environ["V10_HANDSHAKE_DATABASE_URL"],
        data_dir=Path(os.environ["V10_HANDSHAKE_DATA_DIR"]),
        secret_dir=Path(os.environ["V10_HANDSHAKE_SECRET_DIR"]))
    worker = Worker(settings)
    seen, seen_runs, pending, accepted, tainted = set(), set(), {}, {}, set()
    nonce = secrets.token_hex(32)
    with WorkerLock(worker.engine, settings) as owner:
        worker.lock = owner
        with worker.transaction() as db:
            info = db.execute(text("SELECT pg_backend_pid() AS pid, backend_start FROM pg_stat_activity "
                                   "WHERE pid=pg_backend_pid()")).mappings().one()
            owner_identity = {"pid": info["pid"], "backend_start": info["backend_start"].isoformat(),
                              "owner_nonce": nonce}
        async with await psycopg.AsyncConnection.connect(_conninfo(_checkpoint_url(settings.database_url)),
                                                         autocommit=True) as listener:
            await listener.execute("LISTEN v10_test_owner_challenge")
            await listener.execute("LISTEN v10_test_new_run")
            listener_pid = (await (await listener.execute("SELECT pg_backend_pid()" )).fetchone())[0]
            ready_path = Path(os.environ["V10_HANDSHAKE_READY"])
            staged_ready = ready_path.with_name(ready_path.name + ".tmp")
            staged_ready.write_text(str(listener_pid), encoding="ascii")
            staged_ready.replace(ready_path)
            try:
                while True:
                    notes = [note async for note in listener.notifies(timeout=0.5, stop_after=1)]
                    owner.check()  # Never reconnect either session after loss.
                    for note in notes:
                        try:
                            data = json.loads(note.payload)
                            if note.channel == "v10_test_owner_challenge":
                                challenge, reply = data["challenge"], data["reply_to"]
                                if (not isinstance(challenge, str) or len(challenge) != 64
                                        or not isinstance(reply, str) or not reply.startswith("v10r_")
                                        or len(reply) != 37 or not reply[5:].isalnum()):
                                    continue
                                response = {"challenge": challenge, **owner_identity}
                                response["mac"] = tag(settings, "challenge-response", response)
                                await listener.execute("SELECT pg_notify(%s,%s)", (reply, json.dumps(response)))
                                continue
                            if note.channel != "v10_test_new_run":
                                continue
                            payload, signature = data["payload"], data["mac"]
                            run_id, event_id = payload["run_id"], payload["event_id"]
                            if (not isinstance(run_id, str) or not isinstance(event_id, str)
                                    or len(seen) >= 10000 or (run_id, event_id) in seen or run_id in seen_runs):
                                log("duplicate_or_limit", run_id)
                                continue
                            if (payload.get("owner") != owner_identity or not isinstance(signature, str)
                                    or not hmac.compare_digest(signature, tag(settings, "run-created", payload))):
                                log("invalid_owner_or_mac", run_id)
                                continue
                            # Only authenticated events consume the bounded
                            # seen set; rejected ones cannot poison a Run ID.
                            seen.add((run_id, event_id))
                            seen_runs.add(run_id)
                            with worker.transaction() as db:
                                run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
                                message = db.scalar(select(Message).where(Message.run_id == run_id,
                                                                          Message.role == "user"))
                                assistant = db.scalar(select(Message).where(Message.run_id == run_id,
                                                                            Message.role == "assistant"))
                                budget = db.get(RunBudget, run_id)
                                old = (run.outcome or {}).get("v10_test_owner_ticket") if run else None
                                used = any(db.scalar(select(model.id if model is not OperationRecord else model.operation_id)
                                    .where(model.run_id == run_id).limit(1)) for model in
                                    (AgentRun, ToolOperation, OperationRecord, Clarification, RunCommand))
                                reserved = db.scalar(select(RunBudgetReservation.invocation_id)
                                                     .where(RunBudgetReservation.run_id == run_id).limit(1))
                                approval = db.scalar(select(Approval.id).join(ToolOperation)
                                    .where(ToolOperation.run_id == run_id).limit(1))
                                events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
                                if (run is None or message is None or assistant is None or run.status != "queued"
                                        or run.started_at is not None
                                        or run.workflow_version != "stage-agent-v10"
                                        or payload.get("workspace_id") != run.workspace_id or old or used or reserved or approval
                                        or budget is None or budget.model_calls or budget.tool_calls or budget.revisions
                                        or len(events) != 1 or events[0].event_type != "run.created"
                                        or events[0].payload.get("message_id") != assistant.id
                                        or (run.config_snapshot or {}).get("v10_test_owner_origin") != data
                                        or payload.get("digest") != run_digest(run, message)):
                                    log("not_pristine_or_late", run_id)
                                    continue
                            pending[run_id] = event_id
                            try:
                                claimed = worker.claim()
                                if claimed == run_id and os.environ.get("V10_HANDSHAKE_LOSE_CLAIM_ACK") == "1":
                                    raise RuntimeError("synthetic lost claim acknowledgement")
                                if claimed != run_id:
                                    pending.pop(run_id, None)
                                    tainted.add(run_id)
                                    log("claim_unavailable", run_id)
                                    continue
                                owner.check()
                                await listener.execute("SELECT 1")
                                with worker.transaction() as db:
                                    run = db.get(Run, run_id)
                                    if (run is None or run.status != "running"
                                            or not validate_workspace_lease(db, run.workspace_id, run_id)):
                                        raise RuntimeError("Claim or lease changed")
                                    ticket = {"run_id": run_id, "event_id": event_id, **owner_identity}
                                    ticket["mac"] = tag(settings, "owner-ticket", ticket)
                                    run.outcome = {**(run.outcome or {}), "v10_test_owner_ticket": ticket}
                                owner.check()
                                await listener.execute("SELECT 1")
                            except Exception:
                                pending.pop(run_id, None)
                                tainted.add(run_id)
                                log("claim_uncertain", run_id)
                                continue
                            pending.pop(run_id, None)
                            accepted[run_id] = ticket  # Only this memory grants test eligibility.
                            log("claimed", run_id)
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                            log("invalid_message")
            finally:
                # The test process does not rehydrate its memory on restart.
                log("listener_closed")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
