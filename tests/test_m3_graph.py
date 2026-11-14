"""Graph component tests. PostgreSQL test uses a dedicated, isolated schema."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import create_chat
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema

from omniharness.cli import migrate
from omniharness.db import make_session_factory
from omniharness.m3_graph import (
    WORKFLOW_VERSION,
    GraphAlreadyExists,
    GraphError,
    GraphNotResumable,
    OperationContext,
    OperationLedger,
    OperationNeedsReview,
    VersionMismatch,
    build_graph,
    postgres_checkpointer,
    resume,
    start,
)
from omniharness.models import Conversation, ModelConfig, OperationRecord, Provider, Run
from omniharness.services import begin_operation
from omniharness.settings import Settings


def context(run_id, invocation_id="invoke-1", fingerprint=None):
    return OperationContext(
        run_id=run_id, node_id="model", invocation_id=invocation_id,
        workflow_version=WORKFLOW_VERSION,
        input_fingerprint=fingerprint or hashlib.sha256(b"input").hexdigest(),
    )


@pytest.mark.asyncio
async def test_sqlite_interrupt_resume_after_reopen(environment, tmp_path):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    identity = context(queued["run_id"])
    effects = []
    secret = "test-only-secret-not-in-state"

    async def should_pause(_):
        return True

    async def execute(value):
        assert secret.startswith("test-only-")  # held only in the host callback
        effects.append(value.invocation_id)
        return "artifact-1"

    database = tmp_path / "checkpoints.sqlite"
    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        graph = build_graph(saver, OperationLedger(factory), execute, should_pause)
        first = await start(graph, identity)
        assert first["__interrupt__"][0].value["type"] == "pause"
        assert effects == []
        with pytest.raises(GraphAlreadyExists):
            await start(graph, identity)
    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        graph = build_graph(saver, OperationLedger(factory), execute, should_pause)
        result = await resume(graph, identity.run_id)
        assert result["result_ref"] == "artifact-1"
        assert effects == ["invoke-1"]
        with pytest.raises(GraphNotResumable):
            await resume(graph, identity.run_id)
    assert secret.encode() not in database.read_bytes()


@pytest.mark.asyncio
async def test_same_invocation_reuses_result_changed_input_is_rejected(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    ledger = OperationLedger(factory)
    calls = []

    async def execute(value):
        calls.append(value.invocation_id)
        return "artifact-1"

    first = context(queued["run_id"])
    assert await ledger.execute_once(first, execute) == "artifact-1"
    assert await ledger.execute_once(first, execute) == "artifact-1"
    changed = context(first.run_id, fingerprint=hashlib.sha256(b"different").hexdigest())
    with pytest.raises(GraphError, match="input"):
        await ledger.execute_once(changed, execute)
    assert await ledger.execute_once(context(first.run_id, "invoke-2"), execute) == "artifact-1"
    assert calls == ["invoke-1", "invoke-2"]


@pytest.mark.asyncio
async def test_boundary_checkpoint_is_persisted_before_operation(environment, tmp_path):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    identity = context(queued["run_id"])

    async def should_pause(_):
        return False

    async def execute(_):
        snapshot = await graph.aget_state({"configurable": {"thread_id": identity.run_id}})
        assert snapshot.values["pause_required"] is False
        assert snapshot.next == ("operation",)
        return "artifact-before-operation"

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite")) as saver:
        graph = build_graph(saver, OperationLedger(factory), execute, should_pause)
        assert (await start(graph, identity))["result_ref"] == "artifact-before-operation"


@pytest.mark.asyncio
async def test_failure_and_abandoned_running_are_uncertain_without_replay(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    ledger = OperationLedger(factory)
    first = context(queued["run_id"])
    calls = []

    async def fail(_):
        calls.append("called")
        raise RuntimeError("secret-provider-body")

    with pytest.raises(OperationNeedsReview) as error:
        await ledger.execute_once(first, fail)
    assert "secret-provider-body" not in str(error.value)
    with pytest.raises(OperationNeedsReview):
        await ledger.execute_once(first, fail)
    abandoned = context(first.run_id, "invoke-abandoned")
    with factory.begin() as db:
        row, executable = begin_operation(db, abandoned.run_id, abandoned.node_id,
                                          ledger._parameters(abandoned), abandoned.workflow_version)
        assert executable
        row.result = {"input_fingerprint": abandoned.input_fingerprint}
        operation_id = row.operation_id
    with pytest.raises(OperationNeedsReview):
        await ledger.execute_once(abandoned, fail)
    with factory() as db:
        assert db.get(OperationRecord, operation_id).status == "uncertain"
    assert calls == ["called"]


@pytest.mark.asyncio
async def test_no_checkpoint_or_incompatible_version_rejected(environment, tmp_path):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)

    async def should_pause(_):
        return True

    async def execute(_):
        return "artifact-1"

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite")) as saver:
        graph = build_graph(saver, OperationLedger(factory), execute, should_pause)
        with pytest.raises(GraphNotResumable):
            await resume(graph, queued["run_id"])
        original = context(queued["run_id"])
        bad = OperationContext(original.run_id, original.node_id, original.invocation_id,
                               "unsupported-v2", original.input_fingerprint)
        with pytest.raises(VersionMismatch):
            await start(graph, bad)


def _seed_pg(url, run_id):
    settings = Settings(_env_file=None, database_url=url)
    migrate(settings)
    engine = create_engine(url)
    factory = make_session_factory(engine)
    with factory.begin() as db:
        provider = Provider(adapter_type="mock", enabled=True)
        db.add(provider)
        db.flush()
        model = ModelConfig(provider_id=provider.id, name="test", model_name="mock")
        conversation = Conversation(title="checkpoint test")
        db.add_all([model, conversation])
        db.flush()
        db.add(Run(id=run_id, conversation_id=conversation.id, model_config_id=model.id,
                   model_label="mock", ordinal=1, client_request_id="test", mode="mock", config_snapshot={}))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE graph_effect_probe (invocation_id TEXT NOT NULL)"))
    engine.dispose()


async def _child(action: str):
    url = os.environ["OMNI_M3_GRAPH_TEST_URL"]
    run_id = os.environ["OMNI_M3_GRAPH_TEST_RUN_ID"]
    engine = create_engine(url)
    ledger = OperationLedger(make_session_factory(engine))
    identity = context(run_id)

    async def should_pause(_):
        return True

    async def execute(value):
        if action == "resume":
            raise AssertionError("Completed operation was executed again")
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO graph_effect_probe VALUES (:value)"),
                         {"value": value.invocation_id})
        return "artifact-42"

    try:
        async with postgres_checkpointer(url, initialize=True) as saver:
            graph = build_graph(saver, ledger, execute, should_pause)
            if action == "start":
                result = await start(graph, identity)
                assert result["__interrupt__"]
            elif action == "effect":
                assert await ledger.execute_once(identity, execute) == "artifact-42"
            elif action == "resume":
                assert (await resume(graph, run_id))["result_ref"] == "artifact-42"
            else:
                raise AssertionError("unknown child action")
    finally:
        engine.dispose()


def test_postgres_fresh_process_resume_reuses_committed_side_effect():
    raw = os.environ.get("OMNI_TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("Set OMNI_TEST_POSTGRES_URL to dedicated omniharness_test database")
    url = make_url(raw)
    if url.drivername != "postgresql+psycopg" or url.database != "omniharness_test":
        pytest.fail("PostgreSQL graph test requires dedicated omniharness_test database", pytrace=False)
    schema = "omni_graph_" + uuid4().hex
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            if conn.scalar(text("SELECT current_database()")) != "omniharness_test":
                pytest.fail("Unexpected PostgreSQL database; schema not created", pytrace=False)
            conn.execute(CreateSchema(schema))
    finally:
        engine.dispose()
    scoped = url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    run_id = str(uuid4())
    _seed_pg(scoped, run_id)
    env = os.environ.copy()
    env["OMNI_M3_GRAPH_TEST_URL"] = scoped
    env["OMNI_M3_GRAPH_TEST_RUN_ID"] = run_id
    for action in ("start", "effect", "resume"):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), action], env=env,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"child action {action} failed; output redacted"
    scoped_engine = create_engine(scoped)
    try:
        with scoped_engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM graph_effect_probe")) == 1
            assert conn.scalar(text("SELECT count(*) FROM operation_records WHERE status = 'completed'")) == 1
    finally:
        scoped_engine.dispose()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_child(sys.argv[1]))
