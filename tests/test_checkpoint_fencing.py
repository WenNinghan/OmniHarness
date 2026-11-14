"""Opt-in tests in random schemas of the dedicated PostgreSQL test database.

No schemas or data are removed. No running user/QA database is touched.
"""
from __future__ import annotations

import asyncio
import operator
import os
import sys
from typing import Annotated, TypedDict
from uuid import uuid4

import psycopg
import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from omniharness.checkpoint_fencing import (
    FencingError,
    initialize_checkpoint_schema,
    open_fenced_saver,
    register_fence,
    upgrade_legacy_checkpoint_schema,
)
from omniharness.settings import Settings
from omniharness.worker import WorkerLock


class PrivatePGFixture:
    """Keep the PostgreSQL credential out of pytest fixture representations."""

    def __init__(self, raw, engine, settings, schema):
        self._values = (raw, engine, settings, schema)

    def __iter__(self):
        return iter(self._values)

    def __repr__(self):
        return "PrivatePGFixture(<redacted>)"


def run_async(coro):
    old = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop_policy(old)


@pytest.fixture
def pg(monkeypatch):
    raw = os.environ.get("OMNI_TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("Set OMNI_TEST_POSTGRES_URL to dedicated omniharness_test database")
    url = make_url(raw)
    if url.drivername != "postgresql+psycopg" or url.database != "omniharness_test" or "options" in url.query:
        pytest.fail("Checkpoint fencing tests require dedicated omniharness_test without URL options", pytrace=False)
    # Keep credentials out of function arguments and traceback frame reprs.
    # libpq and SQLAlchemy/psycopg both use PGPASSWORD when the URL omits it.
    if url.password:
        monkeypatch.setenv("PGPASSWORD", url.password)
        url = url.set(password=None)
        raw = url.render_as_string(hide_password=False)
    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as connection:
        if connection.scalar(text("SELECT current_database()")) != "omniharness_test":
            pytest.fail("Unexpected actual PostgreSQL database", pytrace=False)
    settings = Settings(_env_file=None, database_url=raw)
    yield PrivatePGFixture(raw, engine, settings, "omni_fence_" + uuid4().hex)
    engine.dispose()


class RootState(TypedDict):
    value: int
    payload: list[str]


def paused(state):
    return {"value": int(interrupt("continue")) * 2}


def root_graph(saver):
    builder = StateGraph(RootState)
    builder.add_node("paused", paused)
    builder.add_edge(START, "paused")
    builder.add_edge("paused", END)
    return builder.compile(checkpointer=saver)


class ParallelState(TypedDict):
    items: Annotated[list[str], operator.add]


def paused_child(state):
    assert interrupt("child") == "continue"
    return {"items": ["resumed"]}


def finished_child(state):
    return {"items": ["completed"]}


def parallel_graph(saver):
    def child(name, function):
        graph = StateGraph(ParallelState)
        graph.add_node(name, function)
        graph.add_edge(START, name)
        graph.add_edge(name, END)
        return graph.compile()

    builder = StateGraph(ParallelState)
    builder.add_node("child0", child("paused", paused_child))
    builder.add_node("child1", child("finished", finished_child))
    builder.add_edge(START, "child0")
    builder.add_edge(START, "child1")
    builder.add_edge("child0", END)
    builder.add_edge("child1", END)
    return builder.compile(checkpointer=saver)


def test_initialization_idempotent_and_unknown_schema_untouched(pg):
    raw, engine, _, schema = pg

    async def exercise():
        await initialize_checkpoint_schema(raw, schema=schema)
        await initialize_checkpoint_schema(raw, schema=schema)
        unsafe = schema + "_unowned"
        with engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {unsafe}"))
            connection.execute(text(f"CREATE TABLE {unsafe}.sentinel (id integer)"))
            connection.execute(text(f"INSERT INTO {unsafe}.sentinel VALUES (17)"))
        with pytest.raises(FencingError, match="not owned"):
            await initialize_checkpoint_schema(raw, schema=unsafe)
        with engine.connect() as connection:
            assert connection.scalar(text(f"SELECT id FROM {unsafe}.sentinel")) == 17

    run_async(exercise())


def test_concurrent_initializers_and_version_tampering_fail_closed(pg, monkeypatch):
    import omniharness.checkpoint_fencing as fencing

    raw, engine, _, schema = pg

    async def exercise():
        await asyncio.gather(initialize_checkpoint_schema(raw, schema=schema),
                             initialize_checkpoint_schema(raw, schema=schema))
        with engine.begin() as connection:
            connection.execute(text(f"DELETE FROM {schema}.checkpoint_migrations WHERE v=2"))
        with pytest.raises(FencingError, match="schema version"):
            await initialize_checkpoint_schema(raw, schema=schema)
        # Ready installations are never silently repaired/rewritten.
        with engine.connect() as connection:
            assert connection.scalar(text(f"SELECT count(*) FROM {schema}.checkpoint_migrations WHERE v=2")) == 0

    run_async(exercise())
    monkeypatch.setattr(fencing, "CHECKPOINTER_VERSION", "not-supported")
    with pytest.raises(FencingError, match="library version"):
        run_async(initialize_checkpoint_schema(raw, schema=schema + "_unknown"))
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname=:n"),
                                 {"n": schema + "_unknown"}) == 0


def test_missing_or_changed_trigger_rejects_open_without_repair(pg):
    raw, engine, settings, schema = pg

    async def exercise():
        await initialize_checkpoint_schema(raw, schema=schema)
        with WorkerLock(engine, settings) as owner:
            ticket = register_fence(owner, "run-1", "v1", schema=schema)
            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE {schema}.checkpoint_writes DISABLE TRIGGER omni_checkpoint_owner_fence"))
            with pytest.raises(FencingError, match="triggers"):
                async with open_fenced_saver(raw, ticket):
                    pass
            with pytest.raises(FencingError, match="triggers"):
                await initialize_checkpoint_schema(raw, schema=schema)
            with engine.connect() as connection:
                assert connection.scalar(text("""
                    SELECT t.tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=:schema AND c.relname='checkpoint_writes'
                    AND t.tgname='omni_checkpoint_owner_fence'
                """), {"schema": schema}) == "D"

    run_async(exercise())


def test_conditional_trigger_with_same_name_and_function_fails_closed(pg):
    """A WHEN clause can bypass fencing while keeping the name, type and function."""
    raw, engine, _, schema = pg

    async def exercise():
        await initialize_checkpoint_schema(raw, schema=schema)
        with engine.begin() as connection:
            connection.execute(text(f"DROP TRIGGER omni_checkpoint_owner_fence ON {schema}.checkpoint_writes"))
            connection.execute(text(
                f"CREATE TRIGGER omni_checkpoint_owner_fence BEFORE INSERT OR UPDATE OR DELETE "
                f"ON {schema}.checkpoint_writes FOR EACH ROW WHEN (false) "
                f"EXECUTE FUNCTION {schema}.fence_checkpoint()"
            ))
        # Initialization of a ready schema verifies instead of repairing it;
        # opening a saver uses this same verification routine.
        with pytest.raises(FencingError, match="triggers"):
            await initialize_checkpoint_schema(raw, schema=schema)
        with engine.connect() as connection:
            assert connection.scalar(text("""
                SELECT t.tgqual IS NOT NULL FROM pg_trigger t
                JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=:schema AND c.relname='checkpoint_writes'
                  AND t.tgname='omni_checkpoint_owner_fence'
            """), {"schema": schema})

    run_async(exercise())


def test_always_triggers_reject_unfenced_writes_in_replica_mode(pg):
    """The database must enforce fences even for replication-mode sessions."""
    raw, engine, _, schema = pg

    async def exercise():
        await initialize_checkpoint_schema(raw, schema=schema)
        with engine.connect() as connection:
            trigger_modes = connection.execute(text("""
                SELECT t.tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=:schema AND t.tgname='omni_checkpoint_owner_fence'
            """), {"schema": schema}).scalars().all()
            assert trigger_modes == ["A", "A", "A"]
        # The dedicated PG test user is a superuser; real production roles need
        # not have permission to change this setting for ALWAYS to protect them.
        async with await psycopg.AsyncConnection.connect(raw.replace("postgresql+psycopg://", "postgresql://"),
                                                           autocommit=True) as connection:
            await connection.execute("SET session_replication_role=replica")
            with pytest.raises(psycopg.Error) as rejected:
                await connection.execute(
                    f"INSERT INTO {schema}.checkpoint_writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob, task_path) "
                    "VALUES ('no-fence', '', 'c', 't', 0, 'test', 'empty', %s, '')", (b"",)
                )
            assert rejected.value.sqlstate == "P0001"
        with engine.connect() as connection:
            assert connection.scalar(text(f"SELECT count(*) FROM {schema}.checkpoint_writes")) == 0

    run_async(exercise())


def test_explicit_v1_upgrade_requires_stopped_worker_and_preserves_data(pg):
    raw, engine, settings, schema = pg

    async def exercise():
        await initialize_checkpoint_schema(raw, schema=schema)
        run_id = "run-" + uuid4().hex
        config = {"configurable": {"thread_id": run_id}}
        with WorkerLock(engine, settings) as owner:
            ticket = register_fence(owner, run_id, "v1", schema=schema)
            async with open_fenced_saver(raw, ticket) as saver:
                result = await root_graph(saver).ainvoke(
                    {"value": 0, "payload": ["persisted"]}, config, durability="sync"
                )
                assert result.get("__interrupt__")
        # Reproduce the earlier ready v1 installation without deleting its data.
        with engine.begin() as connection:
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                connection.execute(text(f"ALTER TABLE {schema}.{table} ENABLE TRIGGER omni_checkpoint_owner_fence"))
            connection.execute(text(f"UPDATE {schema}.component_metadata SET version=1 WHERE id=1"))
            connection.execute(text(f"CREATE TABLE {schema}.preservation_sentinel (v integer NOT NULL)"))
            connection.execute(text(f"INSERT INTO {schema}.preservation_sentinel VALUES (17)"))
        with pytest.raises(FencingError, match="not compatible"):
            await initialize_checkpoint_schema(raw, schema=schema)
        with WorkerLock(engine, settings):
            with pytest.raises(FencingError, match="Stop Workers"):
                await upgrade_legacy_checkpoint_schema(raw, schema=schema)
        with engine.connect() as connection:
            assert connection.scalar(text(f"SELECT version FROM {schema}.component_metadata WHERE id=1")) == 1
        await upgrade_legacy_checkpoint_schema(raw, schema=schema)
        # A version 2 installation is idempotently verified by normal init.
        await initialize_checkpoint_schema(raw, schema=schema)
        with engine.connect() as connection:
            assert connection.scalar(text(f"SELECT version FROM {schema}.component_metadata WHERE id=1")) == 2
            assert connection.scalar(text(f"SELECT v FROM {schema}.preservation_sentinel")) == 17
            modes = connection.execute(text("""
                SELECT t.tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=:schema AND t.tgname='omni_checkpoint_owner_fence'
            """), {"schema": schema}).scalars().all()
            assert modes == ["A", "A", "A"]
        with WorkerLock(engine, settings) as owner:
            ticket = register_fence(owner, run_id, "v1", schema=schema)
            async with open_fenced_saver(raw, ticket) as saver:
                result = await root_graph(saver).ainvoke(Command(resume=21), config, durability="sync")
                assert result["value"] == 42
                assert result["payload"] == ["persisted"]

    run_async(exercise())


def test_fencing_takeover_root_parallel_subgraphs_and_thread_binding(pg):
    raw, engine, settings, schema = pg

    async def exercise():
        await initialize_checkpoint_schema(raw, schema=schema)
        run_id, parallel_id = "run-" + uuid4().hex, "parallel-" + uuid4().hex
        config = {"configurable": {"thread_id": run_id}}
        parallel_config = {"configurable": {"thread_id": parallel_id}}
        with WorkerLock(engine, settings) as first:
            ticket = register_fence(first, run_id, "v1", schema=schema)
            parallel_ticket = register_fence(first, parallel_id, "v1", schema=schema)
            async with open_fenced_saver(raw, ticket) as saver_a, open_fenced_saver(raw, parallel_ticket) as parallel_a:
                graph = root_graph(saver_a)
                children = parallel_graph(parallel_a)
                assert (await graph.ainvoke({"value": 0, "payload": ["blob"]}, config,
                                            durability="sync"))["__interrupt__"]
                assert (await children.ainvoke({"items": []}, parallel_config,
                                               durability="sync"))["__interrupt__"]
                with pytest.raises(FencingError, match="thread"):
                    await saver_a.aget_tuple(parallel_config)
                with pytest.raises(FencingError, match="thread"):
                    await saver_a.aput_writes(parallel_config, [("proof", 1)], str(uuid4()))
                with pytest.raises(FencingError, match="maintenance"):
                    await saver_a.adelete_thread(run_id)
                with pytest.raises(FencingError, match="initialization"):
                    await saver_a.setup()
                checkpoint = (await graph.aget_state(config)).config
                with engine.connect() as control:
                    assert control.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": ticket.owner_pid})
                with pytest.raises(psycopg.Error):
                    await saver_a.aput_writes(checkpoint, [("proof", 1)], str(uuid4()))
                with WorkerLock(engine, settings) as second:
                    updated = register_fence(second, run_id, "v1", schema=schema)
                    updated_parallel = register_fence(second, parallel_id, "v1", schema=schema)
                    assert updated.epoch == ticket.epoch + 1
                    with pytest.raises(FencingError, match="version"):
                        register_fence(second, run_id, "different", schema=schema)
                    with pytest.raises(FencingError, match="no longer valid"):
                        async with open_fenced_saver(raw, ticket):
                            pass
                    with pytest.raises(psycopg.Error):
                        await graph.ainvoke(Command(resume=21), config, durability="sync")
                    async with open_fenced_saver(raw, updated) as saver_b:
                        result = await root_graph(saver_b).ainvoke(Command(resume=21), config, durability="sync")
                        assert result["value"] == 42
                    async with open_fenced_saver(raw, updated_parallel) as parallel_b:
                        result = await parallel_graph(parallel_b).ainvoke(Command(resume="continue"), parallel_config,
                                                                          durability="sync")
                        assert sorted(result["items"]) == ["completed", "resumed"]
        with engine.connect() as connection:
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                assert connection.scalar(text(f"SELECT count(*) FROM {schema}.{table} WHERE thread_id=:run"),
                                         {"run": run_id}) > 0
            assert connection.scalar(text(f"SELECT count(DISTINCT checkpoint_ns) FROM {schema}.checkpoints "
                                          "WHERE thread_id=:run"), {"run": parallel_id}) >= 3

    run_async(exercise())


def test_cross_schema_and_cross_thread_writes_rejected(pg):
    raw, engine, settings, schema = pg

    async def exercise():
        other = schema + "_other"
        await initialize_checkpoint_schema(raw, schema=schema)
        await initialize_checkpoint_schema(raw, schema=other)
        identity = "same-run-" + uuid4().hex
        another = "other-run-" + uuid4().hex
        config = {"configurable": {"thread_id": identity}}
        with WorkerLock(engine, settings) as owner:
            first = register_fence(owner, identity, "v1", schema=schema)
            second = register_fence(owner, identity, "v1", schema=other)
            register_fence(owner, another, "v1", schema=schema)
            async with open_fenced_saver(raw, first) as saver_a, open_fenced_saver(raw, second) as saver_b:
                await root_graph(saver_a).ainvoke({"value": 0, "payload": ["one"]}, config, durability="sync")
                await root_graph(saver_b).ainvoke({"value": 0, "payload": ["two"]}, config, durability="sync")
                # Same thread, same epoch, same owner, but another schema.
                with pytest.raises(psycopg.Error) as rejected:
                    await saver_a.conn.execute(f"UPDATE {other}.checkpoints SET thread_id=thread_id WHERE thread_id=%s",
                                               (identity,))
                assert rejected.value.sqlstate == "P0001"
                # A saver with a valid owner may never move a checkpoint to a different thread.
                with pytest.raises(psycopg.Error) as rejected:
                    await saver_a.conn.execute(f"UPDATE {schema}.checkpoints SET thread_id=%s WHERE thread_id=%s",
                                               (another, identity))
                assert rejected.value.sqlstate == "P0001"
                with pytest.raises(FencingError, match="thread"):
                    await saver_a.aget_tuple({"configurable": {"thread_id": another}})

    run_async(exercise())


def test_owner_takeover_waits_for_checkpoint_batch_and_cancellation_is_atomic(pg):
    raw, engine, settings, schema = pg

    async def exercise():
        await initialize_checkpoint_schema(raw, schema=schema)
        run_id = "run-" + uuid4().hex
        config = {"configurable": {"thread_id": run_id}}
        second = None
        with WorkerLock(engine, settings) as first:
            ticket = register_fence(first, run_id, "v1", schema=schema)
            async with open_fenced_saver(raw, ticket) as saver_a:
                graph = root_graph(saver_a)
                await graph.ainvoke({"value": 0, "payload": ["blob"]}, config, durability="sync")
                checkpoint = (await graph.aget_state(config)).config
                batch = str(uuid4())
                try:
                    async with saver_a.conn.transaction():
                        await saver_a.aput_writes(checkpoint, [("batch", index) for index in range(250)], batch)
                        first.connection.close()  # only the test's own owner backend
                        second = WorkerLock(engine, settings).__enter__()
                        # The registration is blocked on the prior saver transaction's row SHARE lock.
                        task = asyncio.create_task(asyncio.to_thread(register_fence, second, run_id, "v1", schema=schema))
                        await asyncio.sleep(0.3)
                        assert not task.done()
                    updated = await asyncio.wait_for(task, 10)
                    assert updated.epoch == ticket.epoch + 1
                    with engine.connect() as connection:
                        assert connection.scalar(text(f"SELECT count(*) FROM {schema}.checkpoint_writes "
                                                      "WHERE thread_id=:run AND task_id=:batch"),
                                                 {"run": run_id, "batch": batch}) == 250
                    with pytest.raises(psycopg.Error) as rejected:
                        await saver_a.aput_writes(checkpoint, [("stale", 1)], str(uuid4()))
                    assert rejected.value.sqlstate == "P0001"
                    async with open_fenced_saver(raw, updated) as saver_b:
                        # Hold the fence row for update on a separate connection,
                        # cancel the blocked saver write, and prove no partial batch remains.
                        cancelled_id = str(uuid4())
                        with engine.begin() as guard:
                            guard.execute(text(f"UPDATE {schema}.fences SET epoch=epoch WHERE thread_id=:run"),
                                          {"run": run_id})
                            blocked = asyncio.create_task(saver_b.aput_writes(
                                checkpoint, [("cancel", value) for value in range(100)], cancelled_id))
                            await asyncio.sleep(0.3)
                            assert not blocked.done()
                            blocked.cancel()
                            with pytest.raises(asyncio.CancelledError):
                                await asyncio.wait_for(blocked, 10)
                        with engine.connect() as connection:
                            assert connection.scalar(text(f"SELECT count(*) FROM {schema}.checkpoint_writes "
                                                          "WHERE thread_id=:run AND task_id=:batch"),
                                                     {"run": run_id, "batch": cancelled_id}) == 0
                        assert (await root_graph(saver_b).ainvoke(Command(resume=21), config,
                                                                  durability="sync"))["value"] == 42
                finally:
                    if second:
                        second.__exit__(None, None, None)

    run_async(exercise())


def test_terminated_saver_cannot_reconnect_and_rolls_back_batch(pg):
    raw, engine, settings, schema = pg

    async def exercise():
        await initialize_checkpoint_schema(raw, schema=schema)
        run_id = "run-" + uuid4().hex
        config = {"configurable": {"thread_id": run_id}}
        with WorkerLock(engine, settings) as owner:
            ticket = register_fence(owner, run_id, "v1", schema=schema)
            async with open_fenced_saver(raw, ticket) as saver:
                graph = root_graph(saver)
                await graph.ainvoke({"value": 0, "payload": ["blob"]}, config, durability="sync")
                checkpoint = (await graph.aget_state(config)).config
                result = await saver.conn.execute("SELECT pg_backend_pid() AS pid")
                saver_pid = (await result.fetchone())["pid"]
                batch = str(uuid4())
                with pytest.raises(psycopg.Error):
                    async with saver.conn.transaction():
                        await saver.aput_writes(checkpoint, [("proof", value) for value in range(100)], batch)
                        with engine.connect() as control:
                            assert control.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": saver_pid})
                        await saver.conn.execute("SELECT 1")
                with engine.connect() as control:
                    assert control.scalar(text(f"SELECT count(*) FROM {schema}.checkpoint_writes "
                                               "WHERE thread_id=:run AND task_id=:batch"),
                                          {"run": run_id, "batch": batch}) == 0
                with pytest.raises(psycopg.Error):
                    await saver.aput_writes(checkpoint, [("proof", "not-reconnected")], str(uuid4()))

    run_async(exercise())


def test_schema_validation_rejects_unsafe_names():
    with pytest.raises(ValueError, match="schema"):
        run_async(initialize_checkpoint_schema("postgresql+psycopg://invalid/test", schema="public;DROP"))


def test_connection_errors_do_not_expose_database_credentials(monkeypatch):
    secret = "test-only-checkpoint-secret"

    async def failed_connect(*args, **kwargs):
        raise psycopg.OperationalError(f"failed with {secret}")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", failed_connect)
    with pytest.raises(FencingError) as failure:
        run_async(initialize_checkpoint_schema(f"postgresql+psycopg://user:{secret}@127.0.0.1/db"))
    assert secret not in str(failure.value)
    assert failure.value.__suppress_context__
