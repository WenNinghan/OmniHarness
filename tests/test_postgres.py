"""Opt-in PostgreSQL integration tests; never use an application database.

Set OMNI_TEST_POSTGRES_URL to a postgresql+psycopg URL whose database is
omniharness_test. Each invocation creates a separate schema and leaves it in
that disposable database for inspection; no existing schema or data is deleted.
"""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

import pytest
from conftest import create_chat
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.schema import CreateSchema

import omniharness.worker as worker_module
from omniharness.api import create_app
from omniharness.cli import migrate
from omniharness.db import make_engine, make_session_factory
from omniharness.models import Conversation, Message, OperationRecord, Run, RunBudget, RunCommand, RunEvent
from omniharness.providers import ModelDelta
from omniharness.schemas import RunCommandRequest
from omniharness.security import create_pairing_code, init_secrets
from omniharness.services import (
    BudgetLimits,
    ServiceError,
    begin_operation,
    cancel_run,
    ensure_budget,
    reserve_budget,
    submit_command,
)
from omniharness.settings import Settings
from omniharness.worker import Worker, WorkerLock, WorkerOwnershipLost


@pytest.fixture(scope="module")
def postgres_environment(tmp_path_factory):
    configured = os.environ.get("OMNI_TEST_POSTGRES_URL")
    if not configured:
        pytest.skip("Set OMNI_TEST_POSTGRES_URL to a dedicated omniharness_test database")
    try:
        url = make_url(configured)
    except ArgumentError:
        pytest.fail("OMNI_TEST_POSTGRES_URL must be a valid PostgreSQL test URL", pytrace=False)
    if url.drivername != "postgresql+psycopg" or url.database != "omniharness_test":
        pytest.fail("PostgreSQL tests require postgresql+psycopg and database omniharness_test", pytrace=False)

    schema = f"omni_test_{uuid4().hex}"
    bootstrap = create_engine(url)
    try:
        with bootstrap.begin() as connection:
            # Check the actual target too: libpq URL options can override dbname.
            if connection.scalar(text("SELECT current_database()")) != "omniharness_test":
                pytest.fail("The connection did not select omniharness_test; no schema created", pytrace=False)
            connection.execute(CreateSchema(schema))
    finally:
        bootstrap.dispose()

    scoped_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    directory = tmp_path_factory.mktemp("postgres")
    settings = Settings(
        _env_file=None,
        database_url=scoped_url.render_as_string(hide_password=False),
        data_dir=directory / "data", secret_dir=directory / "secrets", static_dir=directory / "web",
        allowed_origins="http://testserver", poll_seconds=0.01, enable_demo=True,
    )
    init_secrets(settings)
    migrate(settings)
    engine = make_engine(settings)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
        yield settings, engine, make_session_factory(engine), schema
    finally:
        engine.dispose()


def test_postgres_migrations_are_repeatable_without_changing_existing_data(postgres_environment):
    settings, engine, factory, schema = postgres_environment
    identity = str(uuid4())
    with factory.begin() as db:
        db.add(Conversation(id=identity, title="Migration preservation sentinel", next_ordinal=17))
    with engine.connect() as connection:
        revision = connection.scalars(text("SELECT version_num FROM alembic_version")).all()
    tables = inspect(engine).get_table_names(schema=schema)
    assert len(revision) == 1

    migrate(settings)
    migrate(settings)

    with engine.connect() as connection:
        assert connection.scalars(text("SELECT version_num FROM alembic_version")).all() == revision
    assert inspect(engine).get_table_names(schema=schema) == tables
    with factory() as db:
        saved = db.get(Conversation, identity)
        assert saved.title == "Migration preservation sentinel"
        assert saved.next_ordinal == 17


def test_postgres_worker_lock_is_exclusive_and_released(postgres_environment):
    settings, engine, _, _ = postgres_environment
    other_engine = make_engine(settings)
    try:
        with WorkerLock(engine, settings) as first:
            first.check()
            with pytest.raises(RuntimeError, match="Another OmniHarness worker"):
                with WorkerLock(other_engine, settings):
                    pytest.fail("A second database worker acquired the exclusive lock")
            first.check()
        with WorkerLock(other_engine, settings) as replacement:
            replacement.check()
    finally:
        other_engine.dispose()


def _sse(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    identifiers = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
    assert identifiers == [item["sequence_number"] for item in events]
    return events


def test_postgres_cancel_and_pause_transition_serialize_on_run_lock(postgres_environment):
    settings, engine, factory, _ = postgres_environment
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        code = create_pairing_code(factory)
        assert client.post("/api/v1/auth/pair", json={"code": code}).status_code == 200
        _, _, queued = create_chat(client)
        run_id = queued["run_id"]
        worker = Worker(settings, engine)
        assert worker.claim() == run_id
        response = client.post(f"/api/v1/runs/{run_id}/commands", json={
            "command_type": "pause", "client_request_id": "postgres-pause",
        })
        assert response.status_code == 202
        worker.consume_commands()
        other_engine = make_engine(settings)
        other_worker = Worker(settings, other_engine)
        entered = Event()
        try:
            def attempt_pause():
                entered.set()
                return other_worker.mark_paused(run_id)

            with ThreadPoolExecutor(max_workers=1) as pool:
                with factory.begin() as db:
                    # Keep the API-side cancel transaction open while the
                    # independent worker connection attempts its transition.
                    assert cancel_run(db, run_id)["status"] == "cancel_requested"
                    future = pool.submit(attempt_pause)
                    assert entered.wait(timeout=3)
                    with pytest.raises(FutureTimeout):
                        future.result(timeout=0.2)
                assert future.result(timeout=5) == "cancelled"
            assert client.post(f"/api/v1/runs/{run_id}/cancel").json()["status"] == "cancelled"
            with factory() as db:
                run = db.get(Run, run_id)
                message = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
                events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id,
                                                               RunEvent.event_type == "run.cancelled")))
                assert run.status == "cancelled" and run.finished_at is not None
                assert message.status == "cancelled" and len(events) == 1

            # Reverse ordering: hold the worker's locked pause transition open
            # while a different connection tries to cancel it.
            _, _, another = create_chat(client)
            other_id = another["run_id"]
            assert worker.claim() == other_id
            assert client.post(f"/api/v1/runs/{other_id}/commands", json={
                "command_type": "pause", "client_request_id": "postgres-pause-reverse",
            }).status_code == 202
            worker.consume_commands()
            paused_locked, release_pause, cancel_entered = Event(), Event(), Event()
            actual_add_event = worker_module.add_event

            def held_pause_event(db, run, event_type, payload):
                if run.id == other_id and event_type == "run.paused":
                    paused_locked.set()
                    assert release_pause.wait(timeout=5)
                return actual_add_event(db, run, event_type, payload)

            def attempt_cancel():
                cancel_entered.set()
                with factory.begin() as db:
                    return cancel_run(db, other_id)["status"]

            with patch.object(worker_module, "add_event", held_pause_event):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    paused_future = pool.submit(other_worker.mark_paused, other_id)
                    try:
                        assert paused_locked.wait(timeout=3)
                        cancel_future = pool.submit(attempt_cancel)
                        assert cancel_entered.wait(timeout=3)
                        with pytest.raises(FutureTimeout):
                            cancel_future.result(timeout=0.2)
                    finally:
                        release_pause.set()
                    assert paused_future.result(timeout=5) == "paused"
                    assert cancel_future.result(timeout=5) == "cancelled"
            with factory() as db:
                run = db.get(Run, other_id)
                cancelled_events = list(db.scalars(select(RunEvent).where(
                    RunEvent.run_id == other_id, RunEvent.event_type == "run.cancelled")))
                assert run.status == "cancelled" and len(cancelled_events) == 1
        finally:
            other_engine.dispose()


async def test_postgres_chat_worker_and_sse_replay_survive_app_reconstruction(postgres_environment):
    settings, engine, factory, _ = postgres_environment
    headers = {"X-Omni-Request": "1"}
    with TestClient(create_app(settings, engine), headers=headers) as client:
        code = create_pairing_code(factory)
        assert client.post("/api/v1/auth/pair", json={"code": code}).status_code == 200
        _, conversation, queued = create_chat(client)
        run_id = queued["run_id"]
        worker = Worker(settings, engine)
        with WorkerLock(engine, settings) as lock:
            worker.lock = lock
            assert await worker.run_once() == run_id
            assert await worker.run_once() is None
        status = client.get(f"/api/v1/runs/{run_id}").json()
        assert status["status"] == "completed"
        assert status["mode"] == "mock"
        endpoint = f"/api/v1/runs/{run_id}/events"
        original = _sse(client.get(endpoint))
        assert original[0]["event_type"] == "run.created"
        assert original[-1]["event_type"] == "run.completed"
        assert "run.started" in [item["event_type"] for item in original]
        assert "message.delta" in [item["event_type"] for item in original]
        assert [item["sequence_number"] for item in original] == list(range(1, len(original) + 1))
        cookie = client.cookies.get("omniharness_session")

    # New connection pool and application instance must read durable rows/session,
    # without needing the original client, worker or any in-memory event buffer.
    engine.dispose()
    replacement_engine = make_engine(settings)
    try:
        with TestClient(create_app(settings, replacement_engine), headers=headers) as resumed:
            resumed.cookies.set("omniharness_session", cookie)
            assert _sse(resumed.get(endpoint)) == original
            cursor = original[1]["sequence_number"]
            expected = [item for item in original if item["sequence_number"] > cursor]
            assert _sse(resumed.get(endpoint, headers={"Last-Event-ID": str(cursor)})) == expected
            assert _sse(resumed.get(endpoint, params={"after": cursor})) == expected
            assert _sse(resumed.get(endpoint, params={"after": original[-1]["sequence_number"]})) == []
            messages = resumed.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
            answer = next(item for item in messages if item["role"] == "assistant")
            assert answer["status"] == "completed" and "[模拟]" in answer["content"]

        with make_session_factory(replacement_engine)() as db:
            run = db.get(Run, run_id)
            assert run.status == "completed"
            assert run.next_event_sequence == len(original) + 1
            stored = db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)
                                .order_by(RunEvent.sequence_number)).all()
            assert [row.event_type for row in stored] == [item["event_type"] for item in original]
            message = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
            assert message.content == answer["content"]
    finally:
        replacement_engine.dispose()


def test_postgres_cancel_and_command_are_serialized_before_terminal_event(postgres_environment):
    settings, engine, factory, _ = postgres_environment
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        code = create_pairing_code(factory)
        assert client.post("/api/v1/auth/pair", json={"code": code}).status_code == 200
        _, _, first = create_chat(client)
        _, _, second = create_chat(client)
    other_engine = make_engine(settings)
    other_factory = make_session_factory(other_engine)
    try:
        entered = Event()
        body = RunCommandRequest(command_type="pause", client_request_id="first")

        def late_submit():
            entered.set()
            try:
                with other_factory.begin() as db:
                    return submit_command(db, first["run_id"], body)
            except ServiceError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=1) as pool:
            with factory.begin() as db:
                assert cancel_run(db, first["run_id"])["status"] == "cancelled"
                future = pool.submit(late_submit)
                assert entered.wait(timeout=3)
                with pytest.raises(FutureTimeout):
                    future.result(timeout=0.2)
            assert future.result(timeout=5) == "RUN_TERMINAL"

        entered.clear()

        def late_cancel():
            entered.set()
            with other_factory.begin() as db:
                return cancel_run(db, second["run_id"])["status"]

        with ThreadPoolExecutor(max_workers=1) as pool:
            with factory.begin() as db:
                assert submit_command(db, second["run_id"], body)["status"] == "pending"
                future = pool.submit(late_cancel)
                assert entered.wait(timeout=3)
                with pytest.raises(FutureTimeout):
                    future.result(timeout=0.2)
            assert future.result(timeout=5) == "cancelled"

        with factory() as db:
            for identity in (first["run_id"], second["run_id"]):
                events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == identity)
                                         .order_by(RunEvent.sequence_number)))
                assert events[-1].event_type == "run.cancelled"
                commands = list(db.scalars(select(RunCommand).where(RunCommand.run_id == identity)))
                assert all(command.status == "rejected" for command in commands)
            assert len(list(db.scalars(select(RunCommand).where(RunCommand.run_id == first["run_id"])))) == 0
            assert len(list(db.scalars(select(RunCommand).where(RunCommand.run_id == second["run_id"])))) == 1
    finally:
        other_engine.dispose()


def test_postgres_operation_claim_is_unique_across_connection_pools(postgres_environment):
    settings, engine, factory, _ = postgres_environment
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        code = create_pairing_code(factory)
        assert client.post("/api/v1/auth/pair", json={"code": code}).status_code == 200
        _, _, queued = create_chat(client)
    run_id = queued["run_id"]
    other_engine = make_engine(settings)
    other_factory = make_session_factory(other_engine)
    barrier = Barrier(8)
    try:
        def attempt(index):
            source = factory if index % 2 == 0 else other_factory
            barrier.wait(timeout=10)
            with source.begin() as db:
                row, executable = begin_operation(db, run_id, "node", {"invocation_id": "one"}, "v1")
                return row.operation_id, executable

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))
        assert sum(executable for _, executable in outcomes) == 1
        assert len({identity for identity, _ in outcomes}) == 1
        with factory() as db:
            records = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id)))
            assert len(records) == 1 and records[0].status == "running"
    finally:
        other_engine.dispose()


def test_postgres_shared_budget_is_atomic_across_connection_pools(postgres_environment):
    """Two independent pools simulate main agent / subagent sharing one counter."""
    settings, engine, factory, _ = postgres_environment
    headers = {"X-Omni-Request": "1"}
    with TestClient(create_app(settings, engine), headers=headers) as client:
        code = create_pairing_code(factory)
        assert client.post("/api/v1/auth/pair", json={"code": code}).status_code == 200
        _, _, queued = create_chat(client)
        run_id = queued["run_id"]

    limits = BudgetLimits(max_model_calls=6, max_tool_calls=6, max_revisions=6, max_active_seconds=60)
    with factory.begin() as db:
        ensure_budget(db, run_id, limits)

    other_engine = make_engine(settings)
    other_factory = make_session_factory(other_engine)
    try:
        def attempt(index):
            source = factory if index % 2 == 0 else other_factory
            with source.begin() as db:
                return reserve_budget(db, run_id, "model_calls", limits)

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(24)))
        assert sum(outcomes) == 6
        with factory() as db:
            assert db.get(RunBudget, run_id).model_calls == 6
        with other_factory() as db:
            assert db.get(RunBudget, run_id).model_calls == 6
    finally:
        other_engine.dispose()


async def test_postgres_lost_lock_fences_old_worker_after_new_owner_recovers(postgres_environment):
    """Terminate only this test's owner backend; stale writes must never commit."""
    settings, engine, factory, _ = postgres_environment
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        code = create_pairing_code(factory)
        assert client.post("/api/v1/auth/pair", json={"code": code}).status_code == 200
        _, _, queued = create_chat(client)
        run_id = queued["run_id"]
    with factory.begin() as db:
        run = db.get(Run, run_id)
        run.status = "running"
        assistant = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
        assistant.status = "running"

    started, closed = asyncio.Event(), asyncio.Event()
    partial = "Already persisted before lock loss. " * 8

    async def stream(config, history):
        try:
            yield ModelDelta(text=partial)
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    other_engine = make_engine(settings)
    try:
        with WorkerLock(engine, settings) as old_lock:
            old = Worker(settings, engine, stream_fn=stream)
            old.lock = old_lock
            with old_lock.connection.begin():
                owner_pid = old_lock.connection.scalar(text("SELECT pg_backend_pid()"))
            old_task = asyncio.create_task(old.process(run_id))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                with factory() as db:
                    saved = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
                    assert saved.content == partial
                # This PID was obtained exclusively from this test's own lock
                # connection; no other database or user service is terminated.
                with other_engine.begin() as killer:
                    assert killer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": owner_pid})
                with WorkerLock(other_engine, settings) as new_lock:
                    replacement = Worker(settings, other_engine)
                    replacement.lock = new_lock
                    replacement.recover()
                    with pytest.raises(WorkerOwnershipLost):
                        await asyncio.wait_for(old_task, timeout=5)
                    for stale_write in (
                        lambda: old.append(run_id, "STALE"),
                        lambda: old.finish(run_id, "failed", "STALE", "must not write"),
                        old.claim,
                        old.consume_commands,
                    ):
                        with pytest.raises(WorkerOwnershipLost):
                            stale_write()
                    with factory() as db:
                        run = db.get(Run, run_id)
                        saved = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
                        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)
                                                 .order_by(RunEvent.sequence_number)))
                        assert run.status == "interrupted" and run.error_code == "WORKER_RESTARTED"
                        assert saved.content == partial
                        assert events[-1].event_type == "run.interrupted"
                        assert all(event.event_type != "run.failed" for event in events)
                assert closed.is_set()
            finally:
                if not old_task.done():
                    old_task.cancel()
                    with pytest.raises((asyncio.CancelledError, WorkerOwnershipLost)):
                        await old_task
    finally:
        other_engine.dispose()


def test_postgres_owner_transaction_rolls_back_when_lock_backend_is_terminated(postgres_environment):
    """Kill the exact owner connection while its update is still uncommitted."""
    settings, engine, factory, _ = postgres_environment
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        code = create_pairing_code(factory)
        assert client.post("/api/v1/auth/pair", json={"code": code}).status_code == 200
        _, _, queued = create_chat(client)
        run_id = queued["run_id"]
    other_engine = make_engine(settings)
    try:
        with WorkerLock(engine, settings) as old_lock:
            old = Worker(settings, engine)
            old.lock = old_lock
            with old_lock.connection.begin():
                owner_pid = old_lock.connection.scalar(text("SELECT pg_backend_pid()"))
            with pytest.raises(WorkerOwnershipLost):
                with old.transaction() as db:
                    run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
                    run.status = "failed"
                    run.error_code = "MUST_ROLL_BACK"
                    db.flush()
                    with other_engine.begin() as killer:
                        assert killer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": owner_pid})
                    with WorkerLock(other_engine, settings) as new_lock:
                        replacement = Worker(settings, other_engine)
                        replacement.lock = new_lock
                        replacement.finish(run_id, "interrupted", "NEW_OWNER", "replacement acquired lock")
            with factory() as db:
                saved = db.get(Run, run_id)
                assert saved.status == "interrupted" and saved.error_code == "NEW_OWNER"
            with pytest.raises(WorkerOwnershipLost):
                old.finish(run_id, "failed", "STALE", "must not write")
    finally:
        other_engine.dispose()
