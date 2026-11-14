"""Test-only API/owner handshake; no runtime integration or model calls."""
# ruff: noqa: F811
import asyncio
import copy
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import text
from test_m4_collaboration import prepare_v4
from test_postgres import postgres_environment  # noqa: F401
from v10_owner_handshake_protocol import run_digest, tag

from omniharness.api import create_app
from omniharness.checkpoint_fencing import _conninfo
from omniharness.m3_worker import _checkpoint_url
from omniharness.models import Conversation, Message, Run, RunBudget
from omniharness.security import create_pairing_code
from omniharness.services import add_event


@pytest.fixture(scope="module")
def event_loop_policy():
    return asyncio.WindowsSelectorEventLoopPolicy() if sys.platform == "win32" else asyncio.get_event_loop_policy()


@pytest.mark.asyncio
async def test_fresh_owner_challenge_lifetime_and_transactional_notification(postgres_environment, tmp_path):
    settings, engine, factory, _ = postgres_environment
    ready, log = tmp_path / "owner-ready", tmp_path / "owner-events"
    env = os.environ.copy()
    env.update({"V10_HANDSHAKE_DATABASE_URL": settings.database_url,
                "V10_HANDSHAKE_DATA_DIR": str(settings.data_dir),
                "V10_HANDSHAKE_SECRET_DIR": str(settings.secret_dir),
                "V10_HANDSHAKE_READY": str(ready), "V10_HANDSHAKE_LOG": str(log)})

    async def start_owner(*, lose_ack=False):
        ready.unlink(missing_ok=True)
        child_env = {**env, "V10_HANDSHAKE_LOSE_CLAIM_ACK": "1" if lose_ack else "0"}
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("v10_owner_handshake_child.py"))],
            env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if not ready.exists():
            if child.poll() is None:
                child.kill()
                await asyncio.to_thread(child.wait, 10)
            pytest.fail("Owner listener failed to start")
        return child, int(ready.read_text(encoding="ascii"))

    async def stop_owner(child):
        if child.poll() is None:
            child.kill()
        await asyncio.to_thread(child.wait, 10)

    async def challenge(conn, old_response=None):
        value, reply = secrets.token_hex(32), "v10r_" + uuid4().hex
        await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(reply)))
        if old_response:
            await conn.execute("SELECT pg_notify(%s,%s)", (reply, json.dumps(old_response)))
        await conn.execute("SELECT pg_notify(%s,%s)", ("v10_test_owner_challenge",
            json.dumps({"challenge": value, "reply_to": reply})))
        rejected_old = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            notes = [item async for item in conn.notifies(timeout=0.5, stop_after=1)]
            for note in notes:
                if note.channel != reply:
                    continue
                response = json.loads(note.payload)
                signature = response.pop("mac")
                if response.get("challenge") != value:
                    rejected_old = True
                    continue
                assert signature == tag(settings, "challenge-response", response)
                assert not old_response or rejected_old
                return response, signature
        pytest.fail("Expected owner challenge response was not observed")

    async def wait_log(kind, run_id, *, count=1):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
            if sum(e.get("event") == kind and e.get("run_id") == run_id for e in entries) >= count:
                return
            await asyncio.sleep(0.05)
        pytest.fail(f"Expected owner event {kind} was not observed")

    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        _, template_id, *_ = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            template = db.get(Run, template_id)
            from omniharness.m4_services import release_workspace_lease
            template.status = "completed"
            assert release_workspace_lease(db, template_id)
            seed = {key: copy.deepcopy(getattr(template, key)) for key in
                ("conversation_id", "model_config_id", "workspace_id", "model_label", "mode", "config_snapshot")}
        ordinal = 100

        def create_run(owner_response, *, notify=True, rollback=False, preclaimed=False, gate=None):
            nonlocal ordinal
            ordinal += 1
            run_id, message_id, event_id = str(uuid4()), str(uuid4()), uuid4().hex
            with factory() as db:
                with db.begin():
                    conversation = Conversation(id=str(uuid4()))
                    db.add(conversation)
                    db.flush()
                    run = Run(id=run_id, conversation_id=conversation.id, model_config_id=seed["model_config_id"],
                        workspace_id=seed["workspace_id"], model_label=seed["model_label"], mode=seed["mode"],
                        config_snapshot=copy.deepcopy(seed["config_snapshot"]), workflow_version="stage-agent-v10",
                        ordinal=ordinal, client_request_id=uuid4().hex, status="running" if preclaimed else "queued")
                    message = Message(id=message_id, conversation_id=run.conversation_id, run_id=run_id,
                                      role="user", content="create a file", status="completed")
                    assistant = Message(id=str(uuid4()), conversation_id=run.conversation_id, run_id=run_id,
                                        role="assistant", content="", status="queued")
                    db.add(run)
                    db.flush()
                    db.add_all([message, assistant, RunBudget(run_id=run_id)])
                    db.flush()
                    add_event(db, run, "run.created", {"message_id": assistant.id})
                    owner = {key: owner_response[key] for key in ("pid", "backend_start", "owner_nonce")}
                    payload = {"run_id": run_id, "workspace_id": run.workspace_id, "event_id": event_id,
                               "owner": owner, "digest": run_digest(run, message)}
                    event = {"payload": payload, "mac": tag(settings, "run-created", payload)}
                    run.config_snapshot = {**run.config_snapshot, "v10_test_owner_origin": event}
                    db.flush()
                    if notify:
                        db.execute(text("SELECT pg_notify(:channel,:payload)"),
                                   {"channel": "v10_test_new_run", "payload": json.dumps(event)})
                    if gate:
                        gate["result"] = (run_id, event)
                        gate["ready"].set()
                        if not gate["release"].wait(5):
                            raise RuntimeError("Test commit gate timed out")
                    if rollback:
                        db.rollback()
            return run_id, event

        conn = await psycopg.AsyncConnection.connect(_conninfo(_checkpoint_url(settings.database_url)), autocommit=True)
        child = None
        try:
            child, _ = await start_owner()
            owner1, signature1 = await challenge(conn)
            owner1_again, _ = await challenge(conn, {**owner1, "mac": signature1})
            assert owner1_again["challenge"] != owner1["challenge"]
            assert signature1 != tag(settings, "challenge-response", {**owner1, "challenge": owner1_again["challenge"]})

            rolled_back, _ = create_run(owner1, rollback=True)
            await asyncio.sleep(0.3)
            with factory() as db:
                assert db.get(Run, rolled_back) is None
            assert not log.exists() or rolled_back not in log.read_text(encoding="utf-8")

            gate = {"ready": threading.Event(), "release": threading.Event()}
            create_task = asyncio.create_task(asyncio.to_thread(create_run, owner1, gate=gate))
            assert await asyncio.to_thread(gate["ready"].wait, 5)
            run_id, event = gate["result"]
            try:
                # A forged pre-commit notification is rejected before it can
                # consume the Run's identity. The true one is invisible until commit.
                forged = {**event, "mac": "0" * 64}
                await conn.execute("SELECT pg_notify(%s,%s)", ("v10_test_new_run", json.dumps(forged)))
                await wait_log("invalid_owner_or_mac", run_id)
                with factory() as db:
                    assert db.get(Run, run_id) is None
                assert "claimed" not in [json.loads(line)["event"] for line in log.read_text().splitlines()
                                         if json.loads(line).get("run_id") == run_id]
            finally:
                gate["release"].set()
            assert await create_task == (run_id, event)
            await wait_log("claimed", run_id)
            with factory() as db:
                ticket = db.get(Run, run_id).outcome["v10_test_owner_ticket"]
                assert ticket["pid"] == owner1["pid"] and ticket["owner_nonce"] == owner1["owner_nonce"]
            await conn.execute("SELECT pg_notify(%s,%s)", ("v10_test_new_run", json.dumps(event)))
            await wait_log("duplicate_or_limit", run_id)

            conflict, _ = create_run(owner1)
            await wait_log("claim_unavailable", conflict)
            with factory() as db:
                assert db.get(Run, conflict).status == "queued"
                assert not (db.get(Run, conflict).outcome or {}).get("v10_test_owner_ticket")

            late, late_event = create_run(owner1, notify=False, preclaimed=True)
            await conn.execute("SELECT pg_notify(%s,%s)", ("v10_test_new_run", json.dumps(late_event)))
            await wait_log("not_pristine_or_late", late)
            with factory() as db:
                assert not (db.get(Run, late).outcome or {}).get("v10_test_owner_ticket")

            dirty, dirty_event = create_run(owner1, notify=False)
            with factory.begin() as db:
                run = db.get(Run, dirty)
                run.started_at = run.created_at
            await conn.execute("SELECT pg_notify(%s,%s)", ("v10_test_new_run", json.dumps(dirty_event)))
            await wait_log("not_pristine_or_late", dirty)

            dirty_history, dirty_history_event = create_run(owner1, notify=False)
            with factory.begin() as db:
                run = db.get(Run, dirty_history)
                add_event(db, run, "run.started", {})
            await conn.execute("SELECT pg_notify(%s,%s)", ("v10_test_new_run", json.dumps(dirty_history_event)))
            await wait_log("not_pristine_or_late", dirty_history)

            missing_budget, missing_budget_event = create_run(owner1, notify=False)
            with factory.begin() as db:
                db.delete(db.get(RunBudget, missing_budget))
            await conn.execute("SELECT pg_notify(%s,%s)", ("v10_test_new_run", json.dumps(missing_budget_event)))
            await wait_log("not_pristine_or_late", missing_budget)

            lost, _ = create_run(owner1, notify=False)
            await asyncio.sleep(0.3)
            with factory() as db:
                assert not (db.get(Run, lost).outcome or {}).get("v10_test_owner_ticket")

            await stop_owner(child)
            child = None
            # Simulate restoration of the old pre-claim business row. An old
            # authenticated creation event still bears the previous nonce.
            with factory.begin() as db:
                run = db.get(Run, run_id)
                run.status, run.outcome = "queued", {}
            child, _ = await start_owner()
            owner2, _ = await challenge(conn)
            assert owner2["owner_nonce"] != owner1["owner_nonce"]
            await conn.execute("SELECT pg_notify(%s,%s)", ("v10_test_new_run", json.dumps(event)))
            await wait_log("invalid_owner_or_mac", run_id)
            with factory() as db:
                assert not (db.get(Run, run_id).outcome or {}).get("v10_test_owner_ticket")

            # The actual lock-owner backend can disappear while the listener
            # remains connected. The process must lose all eligibility.
            await conn.execute("SELECT pg_terminate_backend(%s)", (owner2["pid"],))
            deadline = time.monotonic() + 5
            while child.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert child.poll() is not None
            await stop_owner(child)
            child = None

            # Remove only synthetic test Runs from contention, then simulate a
            # successful real claim whose acknowledgement is lost in-process.
            with factory.begin() as db:
                for identity in (run_id, conflict, late, dirty, dirty_history, missing_budget, lost):
                    db.get(Run, identity).status = "cancelled"
                from omniharness.m4_services import release_workspace_lease
                assert release_workspace_lease(db, run_id)
            child, listener_pid = await start_owner(lose_ack=True)
            owner3, _ = await challenge(conn)
            uncertain, uncertain_event = create_run(owner3)
            await wait_log("claim_uncertain", uncertain)
            with factory() as db:
                run = db.get(Run, uncertain)
                assert run.status == "running" and run.started_at is not None
                assert not (run.outcome or {}).get("v10_test_owner_ticket")
            await conn.execute("SELECT pg_notify(%s,%s)", ("v10_test_new_run", json.dumps(uncertain_event)))
            await wait_log("duplicate_or_limit", uncertain)

            # Losing the listener destroys this experiment's entire owner
            # lifetime; a new connection never rehydrates eligibility.
            await conn.execute("SELECT pg_terminate_backend(%s)", (listener_pid,))
            deadline = time.monotonic() + 5
            while child.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert child.poll() is not None
        finally:
            if child is not None:
                await stop_owner(child)
            await conn.close()
