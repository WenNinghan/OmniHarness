"""Durable accounting is separate from authority to execute operations."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from conftest import create_chat
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from test_m3_agents import Hooks, role
from test_postgres import postgres_environment  # noqa: F401

from omniharness.api import create_app
from omniharness.db import Base
from omniharness.m3_agents import AgentBoundaryError, build_controlled_agent, invocation_identity
from omniharness.m3_budget import ReservationConflict, ReservationResult, reservations, reserve_invocation
from omniharness.models import RunBudget
from omniharness.security import create_pairing_code
from omniharness.services import BudgetLimits, ensure_budget

IDENTITY = "a" * 64
BINDING = "b" * 64
LIMITS = BudgetLimits(max_model_calls=2, max_tool_calls=2, max_revisions=1, max_active_seconds=60)


def prepare(factory, client, limits=LIMITS):
    _, _, queued = create_chat(client)
    with factory.begin() as db:
        ensure_budget(db, queued["run_id"], limits)
    return queued["run_id"]


def reserve(db, run_id, *, identity=IDENTITY, binding=BINDING, kind="model_calls"):
    return reserve_invocation(db, run_id=run_id, kind=kind,
                              invocation_id=identity, binding_fingerprint=binding)


def test_sqlite_retry_conflict_exhaustion_and_rollback(environment):
    _, engine, factory, client = environment
    run_id = prepare(factory, client)
    with factory.begin() as db:
        assert reserve(db, run_id) == ReservationResult.RESERVED
    with factory.begin() as db:
        assert reserve(db, run_id) == ReservationResult.ALREADY_RESERVED
    with pytest.raises(ReservationConflict), factory.begin() as db:
        reserve(db, run_id, binding="c" * 64)
    with pytest.raises(ReservationConflict), factory.begin() as db:
        reserve(db, run_id, kind="tool_calls")
    with pytest.raises(RuntimeError), factory.begin() as db:
        assert reserve(db, run_id, identity="d" * 64) == ReservationResult.RESERVED
        raise RuntimeError("transaction abort")
    with factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 1
        assert db.scalar(select(func.count()).select_from(reservations)) == 1
    with factory.begin() as db:
        assert reserve(db, run_id, identity="d" * 64) == ReservationResult.RESERVED
        assert reserve(db, run_id, identity="e" * 64) == ReservationResult.EXHAUSTED
    with factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 2
        assert db.scalar(select(func.count()).select_from(reservations)) == 2
    with engine.connect() as conn:
        changes = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert not [item for item in changes if "run_budget_reservations" in str(item)]


def test_missing_frozen_budget_and_invalid_identity_fail_closed(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    with pytest.raises(ReservationConflict), factory.begin() as db:
        reserve(db, queued["run_id"])
    with pytest.raises(ValueError), factory.begin() as db:
        reserve(db, queued["run_id"], identity="not-a-hash")
    with pytest.raises(ReservationConflict, match="transaction"), factory() as db:
        reserve(db, queued["run_id"])


def test_runtime_identity_is_stable_bound_to_payload_and_fails_closed():
    from types import SimpleNamespace

    info = SimpleNamespace(checkpoint_ns="child", task_id="task-123", thread_id="run-123")
    runtime = SimpleNamespace(execution_info=info)
    tool = SimpleNamespace(runtime=runtime, tool_call={"id": "call-1", "args": {"x": 3}})
    first = invocation_identity(tool, kind="tool_calls", role="coder", tool_name="test_tool")
    again = invocation_identity(tool, kind="tool_calls", role="coder", tool_name="test_tool")
    assert first == again and first.thread_id == "run-123"
    changed = SimpleNamespace(runtime=runtime, tool_call={"id": "call-1", "args": {"x": 4}})
    second = invocation_identity(changed, kind="tool_calls", role="coder", tool_name="test_tool")
    assert first.invocation_id == second.invocation_id
    assert first.binding_fingerprint != second.binding_fingerprint
    model = SimpleNamespace(runtime=runtime, messages=[HumanMessage(content="hello")], system_message=None)
    assert len(invocation_identity(model, kind="model_calls", role="coder", tool_name=None).invocation_id) == 64
    with pytest.raises(AgentBoundaryError):
        invocation_identity(SimpleNamespace(runtime=None), kind="model_calls", role="coder", tool_name=None)
    with pytest.raises(AgentBoundaryError):
        invocation_identity(SimpleNamespace(runtime=runtime, tool_call={"args": {}}),
                            kind="tool_calls", role="coder", tool_name="test_tool")


def test_durable_mode_without_saver_or_durable_host_is_rejected():
    with pytest.raises(ValueError, match="checkpointer"):
        build_controlled_agent(coordinator=role("coordinator", "parent"), subagents=[],
                               hooks=Hooks(), require_durable_ids=True)


def test_postgres_cross_pool_idempotent_reservation(request):
    pg_env = request.getfixturevalue("postgres_environment")
    settings, engine, factory, _ = pg_env
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        run_id = prepare(factory, client)
    second_engine = create_engine(settings.database_url, pool_pre_ping=True)
    second_factory = sessionmaker(second_engine, expire_on_commit=False)
    barrier = Barrier(8)

    def attempt(index):
        barrier.wait(timeout=20)
        with (factory if index % 2 == 0 else second_factory).begin() as db:
            return reserve(db, run_id)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))
        assert outcomes.count(ReservationResult.RESERVED) == 1
        assert outcomes.count(ReservationResult.ALREADY_RESERVED) == 7
        with second_factory.begin() as db:
            assert reserve(db, run_id, identity="d" * 64) == ReservationResult.RESERVED
        with factory.begin() as db:
            assert reserve(db, run_id, identity="e" * 64) == ReservationResult.EXHAUSTED
        with pytest.raises(ReservationConflict), second_factory.begin() as db:
            reserve(db, run_id, binding="f" * 64)
        with pytest.raises(RuntimeError), second_factory.begin() as db:
            assert reserve(db, run_id, identity="b" * 64, kind="tool_calls") == ReservationResult.RESERVED
            raise RuntimeError("transaction abort")
        with factory() as db:
            assert db.get(RunBudget, run_id).model_calls == 2
            assert db.get(RunBudget, run_id).tool_calls == 0
            assert db.scalar(select(func.count()).select_from(reservations)) == 2
        with engine.connect() as conn:
            changes = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert not [item for item in changes if "run_budget_reservations" in str(item)]
    finally:
        second_engine.dispose()


def test_deepagents_cross_process_interrupt_does_not_double_reserve(environment, tmp_path):
    settings, _, factory, client = environment
    run_id = prepare(factory, client, BudgetLimits(max_model_calls=10, max_tool_calls=10))
    with factory.begin() as db:
        db.execute(text("CREATE TABLE test_control (id INTEGER PRIMARY KEY)"))
    checkpoint = tmp_path / "checkpoints.sqlite"
    script = tmp_path / "agent_probe.py"
    script.write_text(r'''
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path("tests").resolve()))
from test_m3_agents import Hooks, LocalModel
from omniharness.m3_agents import AgentRole, build_controlled_agent
from omniharness.m3_budget import ReservationResult, reserve_invocation
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command, interrupt
from langchain_core.tools import tool
from langsmith import tracing_context
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

dburl, checkpoint, run_id, phase = sys.argv[1:]
engine = create_engine(dburl)
factory = sessionmaker(engine, expire_on_commit=False)
class Durable(Hooks):
    async def check_control(self, kind, role, tool_name):
        if kind == 'model_calls' and role == 'child0':
            with factory.begin() as db:
                already = db.scalar(text('SELECT count(*) FROM test_control'))
            if not already:
                assert interrupt({'marker':'child-before-model'}) == 'continue'
                with factory.begin() as db:
                    db.execute(text('INSERT INTO test_control (id) VALUES (1)'))
    async def reserve_invocation(self, kind, role, tool_name, identity):
        assert identity.thread_id == run_id
        with factory.begin() as db:
            result = reserve_invocation(db, run_id=run_id, kind=kind,
                invocation_id=identity.invocation_id, binding_fingerprint=identity.binding_fingerprint)
            return result != ReservationResult.EXHAUSTED

@tool
async def probe_work(label: str) -> str:
    """Test only."""
    return 'ok'

async def main():
    async with AsyncSqliteSaver.from_conn_string(checkpoint) as saver:
        agent = build_controlled_agent(coordinator=AgentRole('coordinator','test', LocalModel(scenario='parent',delegates=1)),
            subagents=[AgentRole('child0','test',LocalModel(scenario='child0'),tools=(probe_work,))],
            hooks=Durable(),checkpointer=saver,require_durable_ids=True)
        payload = {'messages':[('user','delegate')]} if phase == 'start' else Command(resume='continue')
        with tracing_context(enabled=False):
            result = await agent.ainvoke(payload, {'configurable':{'thread_id':run_id}}, durability='sync')
        print('INTERRUPTED' if result.get('__interrupt__') else 'DONE')
if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
asyncio.run(main())
''', encoding="utf-8")
    for phase, expected in [("start", "INTERRUPTED"), ("resume", "DONE")]:
        result = subprocess.run([sys.executable, str(script), settings.database_url, str(checkpoint), run_id, phase],
                                cwd=str(Path(__file__).resolve().parents[1]), env=os.environ.copy(),
                                capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stderr
        assert expected in result.stdout
    with factory() as db:
        budget = db.get(RunBudget, run_id)
        assert (budget.model_calls, budget.tool_calls) == (4, 2)
        assert db.scalar(select(func.count()).select_from(reservations)) == 6
