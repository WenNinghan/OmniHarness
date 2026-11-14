"""Host callbacks are isolated components, not a product Agent runtime."""

import asyncio
import json
import os
import sys
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, Interrupt
from langsmith import tracing_context
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from test_m3_agent_config import ROLE_VERSIONS, enqueue_and_freeze, mark_tools, model
from test_m3_agents import LocalModel, role
from test_postgres import postgres_environment  # noqa: F401 - opt-in dedicated fixture

from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema, open_fenced_saver, register_fence
from omniharness.m3_agents import (
    AgentBoundaryError,
    AgentExternalCallFailed,
    AgentRole,
    InvocationIdentity,
    SharedBoundaryMiddleware,
    build_controlled_agent,
)
from omniharness.m3_host import (
    AgentControlPending,
    AgentOperationNeedsReview,
    FencedAgentHost,
    _failure_diagnostic,
)
from omniharness.m3_worker import _verified_child_interrupt
from omniharness.models import OperationRecord, Run, RunBudget, RunCommand, RunEvent
from omniharness.providers import ProviderError
from omniharness.schemas import RunCommandRequest
from omniharness.security import create_pairing_code
from omniharness.services import submit_command
from omniharness.worker import Worker, WorkerLock, WorkerOwnershipLost


@pytest.fixture
def event_loop_policy():
    # Psycopg async connections require Selector on Windows. No global policy
    # change or password-bearing fixture repr is needed.
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


def prepared(settings, engine, factory, client):
    configured = model(client, "host", url="http://localhost:11434/v1", key=None)
    mark_tools(factory, configured["id"])
    conversation = client.post("/api/v1/conversations", json={}).json()
    with factory.begin() as db:
        run_id = enqueue_and_freeze(db, conversation["id"], configured["id"], settings)
        db.get(Run, run_id).status = "running"
    return run_id


def host_for(worker, run_id, *, verifier=None):
    return FencedAgentHost(worker, run_id, "agent-v1", role_versions=ROLE_VERSIONS, plugin_versions={},
                           role_bindings={"coordinator": "think", "researcher": "plan"},
                           verify_delegation_resume=verifier)


def identity(run_id, digest="a", *, delegated_role=None):
    return InvocationIdentity(thread_id=run_id, invocation_id=digest * 64, binding_fingerprint="b" * 64,
                              delegated_role=delegated_role)


@pytest.mark.parametrize("error,code,status,category", [
    (TimeoutError("private-error-canary"), "PROVIDER_TIMEOUT", None, "timeout"),
    (httpx.HTTPStatusError("private-error-canary", request=httpx.Request("POST", "https://example.invalid"),
                          response=httpx.Response(502, text="private-error-canary")),
     "PROVIDER_UNAVAILABLE", 502, "http_status"),
    (SQLAlchemyError("private-error-canary"), "PROVIDER_ERROR", None, "database"),
    (asyncio.CancelledError("private-error-canary"), "PROVIDER_ERROR", None, "cancelled"),
    (ProviderError("private-error-canary", "private-error-canary"), "PROVIDER_ERROR", None, "unknown"),
])
@pytest.mark.asyncio
async def test_unknown_diagnostics_are_bounded_and_never_allow_replay(environment, error, code, status, category):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host, call = host_for(worker, run_id), identity(run_id)
        await host.reserve_invocation("model_calls", "coordinator", None, call)
        with factory() as db:
            before = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id)).result
        await host.mark_invocation_uncertain_with_diagnostics(
            "model_calls", "coordinator", None, call, error=error, transport_mode="ainvoke_default")
        with pytest.raises(AgentOperationNeedsReview):
            await host.reserve_invocation("model_calls", "coordinator", None, call)
    expected = {"error_code": code, "http_status": status,
                "exception_category": category, "transport_mode": "ainvoke_default"}
    with factory() as db:
        operation = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
        assert (operation.status, operation.error_code, operation.attempts) == ("uncertain", "RESULT_UNKNOWN", 1)
        assert operation.result == {**before, "diagnostic": expected}
        event = db.scalar(select(RunEvent).where(RunEvent.run_id == run_id,
                                              RunEvent.event_type == "agent.model_uncertain"))
        assert event.payload["diagnostic"] == expected
        assert "private-error-canary" not in json.dumps([operation.result, event.payload])
        assert db.get(RunBudget, run_id).model_calls == 1


@pytest.mark.parametrize("status", [True, "502 private-error-canary", -1, 600])
def test_failure_diagnostic_rejects_non_http_status_and_arbitrary_transport(status):
    error = RuntimeError("private-error-canary")
    error.status_code = status
    diagnostic = _failure_diagnostic(error, "private-error-canary")
    assert diagnostic["http_status"] is None
    assert diagnostic["exception_category"] == "unknown"
    assert diagnostic["transport_mode"] == "unknown"
    assert "private-error-canary" not in json.dumps(diagnostic)


@pytest.mark.asyncio
async def test_owner_reservation_completion_and_ambiguous_reentry_fail_closed(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    with pytest.raises(AgentBoundaryError):
        host_for(worker, run_id)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = host_for(worker, run_id)
        call = identity(run_id)
        await host.check_control("model_calls", "coordinator", None)
        assert await host.reserve_invocation("model_calls", "coordinator", None, call)
        with factory() as db:
            assert db.get(RunBudget, run_id).model_calls == 1
            assert db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id)).status == "running"
        await host.finish_invocation("model_calls", "coordinator", None, call)
        with factory() as db:
            assert db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id)).status == "completed"
        with pytest.raises(AgentOperationNeedsReview):
            await host.reserve_invocation("model_calls", "coordinator", None, call)
        with pytest.raises(AgentBoundaryError):
            await host.reserve_invocation("model_calls", "coordinator", None, identity("wrong-run"))
        with factory() as db:
            assert db.get(RunBudget, run_id).model_calls == 1


@pytest.mark.asyncio
async def test_pending_control_blocks_dispatch_before_budget_or_operation(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    with factory.begin() as db:
        submit_command(db, run_id, RunCommandRequest(command_type="pause", client_request_id="pause-1"))
    worker = Worker(settings, engine)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = host_for(worker, run_id)
        with pytest.raises(AgentControlPending):
            await host.reserve_invocation("model_calls", "coordinator", None, identity(run_id))
    with factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 0
        assert db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id)) is None


@pytest.mark.asyncio
async def test_agent_pause_checkpoints_before_dispatch_and_resumes_same_thread(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    with factory.begin() as db:
        saved = submit_command(db, run_id, RunCommandRequest(command_type="pause", client_request_id="pause-1"))
    calls = []

    class CaptureModel(LocalModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append(True)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="completed"))])

    worker = Worker(settings, engine)
    config = {"configurable": {"thread_id": run_id}}
    coordinator = AgentRole(name="coordinator", description="test", model=CaptureModel(scenario="capture"))
    with WorkerLock(engine, settings) as owner:
        worker.lock = owner
        host = host_for(worker, run_id)
        async with AsyncSqliteSaver.from_conn_string(str(settings.data_dir / "pause-host-test.db")) as saver:
            agent = build_controlled_agent(coordinator=coordinator, subagents=[], hooks=host,
                                           checkpointer=saver, require_durable_ids=True)
            with tracing_context(enabled=False):
                paused = await agent.ainvoke({"messages": [("user", "do task")]}, config, durability="sync")
            assert paused["__interrupt__"] and calls == []
            snapshot = await agent.aget_state(config)
            assert snapshot.interrupts and snapshot.next
            with factory() as db:
                assert db.get(Run, run_id).status == "pause_requested"
                assert db.get(RunCommand, saved["command_id"]).status == "applied"
                assert db.get(RunBudget, run_id).model_calls == 0
            # Production Worker must only persist paused after verifying the
            # saved interrupt, then set running before resuming the same Run.
            with factory.begin() as db:
                db.get(Run, run_id).status = "running"
            with tracing_context(enabled=False):
                result = await agent.ainvoke(Command(resume="continue"), config, durability="sync")
            assert result["messages"][-1].content == "completed" and calls == [True]


@pytest.mark.asyncio
async def test_pause_arriving_after_control_check_becomes_durable_interrupt(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    calls = []

    class CaptureModel(LocalModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append(True)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="completed"))])

    class RaceHost(FencedAgentHost):
        async def reserve_invocation(self, kind, role_name, tool_name, identity):
            with factory.begin() as db:
                submit_command(db, run_id, RunCommandRequest(command_type="pause", client_request_id="race-pause"))
            return await super().reserve_invocation(kind, role_name, tool_name, identity)

    worker = Worker(settings, engine)
    config = {"configurable": {"thread_id": run_id}}
    coordinator = AgentRole(name="coordinator", description="test", model=CaptureModel(scenario="capture"))
    with WorkerLock(engine, settings) as owner:
        worker.lock = owner
        host = RaceHost(worker, run_id, "agent-v1", role_versions=ROLE_VERSIONS, plugin_versions={},
                        role_bindings={"coordinator": "think"})
        async with AsyncSqliteSaver.from_conn_string(str(settings.data_dir / "pause-race.db")) as saver:
            agent = build_controlled_agent(coordinator=coordinator, subagents=[], hooks=host,
                                           checkpointer=saver, require_durable_ids=True)
            with tracing_context(enabled=False):
                result = await agent.ainvoke({"messages": [("user", "do task")]}, config, durability="sync")
            snapshot = await agent.aget_state(config)
            assert result.get("__interrupt__") and snapshot.interrupts and calls == []
        with factory() as db:
            assert db.get(Run, run_id).status == "pause_requested"
            assert db.get(RunBudget, run_id).model_calls == 0


@pytest.mark.asyncio
async def test_delegation_reentry_requires_explicit_checkpoint_proof(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        call = identity(run_id, "c", delegated_role="researcher")
        host = host_for(worker, run_id)
        assert await host.reserve_invocation("tool_calls", "coordinator", "task", call)
        with pytest.raises(AgentOperationNeedsReview):
            await host.reserve_invocation("tool_calls", "coordinator", "task", call)

        async def verified(received):
            return received == call

        proven = host_for(worker, run_id, verifier=verified)
        assert await proven.reserve_invocation("tool_calls", "coordinator", "task", call)
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == 1
            assert db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id)) is None


@pytest.mark.asyncio
async def test_inspect_child_checkpoint_for_verified_resume(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker, identities = Worker(settings, engine), []

    class PauseChildHost(FencedAgentHost):
        async def reserve_invocation(self, kind, role_name, tool_name, invocation):
            identities.append((kind, role_name, tool_name, invocation))
            return await super().reserve_invocation(kind, role_name, tool_name, invocation)

        async def check_control(self, kind, role_name, tool_name):
            if role_name == "child0" and kind == "model_calls":
                with self.worker.transaction() as db:
                    db.get(Run, run_id).status = "pause_requested"
            return await super().check_control(kind, role_name, tool_name)

    with WorkerLock(engine, settings) as owner:
        worker.lock = owner
        host = PauseChildHost(worker, run_id, "agent-v1", role_versions=ROLE_VERSIONS, plugin_versions={},
                              role_bindings={"coordinator": "think", "child0": "plan", "child1": "execute"})

        class PlainModel(LocalModel):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])

        async with AsyncSqliteSaver.from_conn_string(str(settings.data_dir / "inspect-child.db")) as saver:
            agent = build_controlled_agent(coordinator=role("coordinator", "parent", delegates=2),
                                           subagents=[role("child0", "child0"),
                                                      AgentRole("child1", "test", PlainModel(scenario="plain"))], hooks=host,
                                           checkpointer=saver, require_durable_ids=True)
            config = {"configurable": {"thread_id": run_id}}
            with tracing_context(enabled=False):
                result = await agent.ainvoke({"messages": [("user", "delegate")]}, config, durability="sync")
            assert result.get("__interrupt__") and identities
            delegated = [x for _kind, _role, name, x in identities if name == "task"]
            assert len(delegated) == 2 and delegated[0].task_id != delegated[1].task_id
            for item in delegated:
                assert await _verified_child_interrupt(agent, saver, config, item)
            assert not await _verified_child_interrupt(agent, saver, config, replace(delegated[0], task_id="wrong"))
            assert not await _verified_child_interrupt(agent, saver, config, replace(delegated[0], thread_id="wrong"))
@pytest.mark.asyncio
async def test_middleware_preserves_interrupt_and_marks_unknown():
    seen = []

    class Hooks:
        async def mark_invocation_uncertain(self, kind, role, tool_name, invocation):
            seen.append((kind, role, tool_name, invocation))

    from asyncio import Semaphore

    middleware = SharedBoundaryMiddleware(hooks=Hooks(), role="coordinator", allowed_tools=frozenset(),
                                          allowed_agents=frozenset(), semaphore=Semaphore(2), parent=True)
    signal, invocation = GraphInterrupt([Interrupt("pause")]), identity("run")

    async def interrupted(_):
        raise signal

    with pytest.raises(GraphInterrupt) as error:
        await middleware._dispatch(SimpleNamespace(), interrupted, invocation, "model_calls", None)
    assert error.value is signal
    assert seen == [("model_calls", "coordinator", None, invocation)]


@pytest.mark.asyncio
async def test_real_deepagents_loop_uses_owner_bound_host_and_shared_budget(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)

    @tool
    async def probe_work(label: str) -> str:
        """Perform a deterministic in-memory test only."""
        return "ok-" + label

    with WorkerLock(engine, settings) as owner:
        worker.lock = owner
        host = FencedAgentHost(worker, run_id, "agent-v1", role_versions=ROLE_VERSIONS, plugin_versions={},
                               role_bindings={"coordinator": "think", "child0": "plan", "child1": "execute"})
        async with AsyncSqliteSaver.from_conn_string(str(settings.data_dir / "agent-host-test.db")) as saver:
            agent = build_controlled_agent(coordinator=role("coordinator", "parent"),
                                           subagents=[role("child0", "child0", tools=[probe_work]),
                                                      role("child1", "child1", tools=[probe_work])],
                                           hooks=host, checkpointer=saver, require_durable_ids=True)
            with tracing_context(enabled=False):
                result = await agent.ainvoke({"messages": [("user", "delegate test")]},
                                             {"configurable": {"thread_id": run_id}}, durability="sync")
        assert "ok-child0" in result["messages"][-1].content
        assert "ok-child1" in result["messages"][-1].content
        with factory() as db:
            budget = db.get(RunBudget, run_id)
            operations = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id)))
            assert (budget.model_calls, budget.tool_calls) == (6, 4)
            assert len(operations) == 8 and all(op.status == "completed" for op in operations)


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_point", ["before_reserve", "after_reserve"])
async def test_requirement_checkpoint_crash_windows_never_silently_lose_or_replay(environment, crash_point):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    with factory.begin() as db:
        saved = submit_command(db, run_id, RunCommandRequest(
            command_type="append_requirement", client_request_id="requirement-1", content="include the boundary case",
        ))
    calls = []

    class CaptureModel(LocalModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append([(m.id, m.content) for m in messages if isinstance(m, HumanMessage)])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="completed"))])

    class CrashHost(FencedAgentHost):
        async def reserve_invocation(self, kind, role_name, tool_name, identity):
            if crash_point == "after_reserve":
                assert await super().reserve_invocation(kind, role_name, tool_name, identity)
            raise RuntimeError("injected-crash")

    worker = Worker(settings, engine)
    config = {"configurable": {"thread_id": run_id}}
    coordinator = AgentRole(name="coordinator", description="test", model=CaptureModel(scenario="capture"))
    with WorkerLock(engine, settings) as owner:
        worker.lock = owner
        crashed = CrashHost(worker, run_id, "agent-v1", role_versions=ROLE_VERSIONS, plugin_versions={},
                            role_bindings={"coordinator": "think"})
        async with AsyncSqliteSaver.from_conn_string(str(settings.data_dir / "requirement-recovery.db")) as saver:
            agent = build_controlled_agent(coordinator=coordinator, subagents=[], hooks=crashed,
                                           checkpointer=saver, require_durable_ids=True)
            with tracing_context(enabled=False), pytest.raises(AgentExternalCallFailed):
                await agent.ainvoke({"messages": [("user", "do task")]}, config, durability="sync")
            snapshot = await agent.aget_state(config)
            command_id = f"omni-command:{saved['command_id']}"
            persisted = [m for m in snapshot.values["messages"] if isinstance(m, HumanMessage) and m.id == command_id]
            assert len(persisted) == 1 and persisted[0].content == "include the boundary case"
            with factory() as db:
                command = db.get(RunCommand, saved["command_id"])
                assert command.status == ("pending" if crash_point == "before_reserve" else "applied")
            resumed = host_for(worker, run_id)
            restored = build_controlled_agent(coordinator=coordinator, subagents=[], hooks=resumed,
                                              checkpointer=saver, require_durable_ids=True)
            if crash_point == "before_reserve":
                with tracing_context(enabled=False):
                    result = await restored.ainvoke(None, config, durability="sync")
                assert result["messages"][-1].content == "completed"
                assert len(calls) == 1 and calls[0].count((command_id, "include the boundary case")) == 1
            else:
                with tracing_context(enabled=False), pytest.raises(AgentOperationNeedsReview):
                    await restored.ainvoke(None, config, durability="sync")
                assert calls == []
        with factory() as db:
            assert db.get(RunCommand, saved["command_id"]).status == "applied"
            matching = [event for event in db.scalars(select(RunEvent).where(RunEvent.run_id == run_id))
                        if event.event_type == "run.command" and event.payload["command_id"] == saved["command_id"]]
            assert len(matching) == 1 and matching[0].payload["delivery"] == "prepared_for_model"
            assert db.get(RunBudget, run_id).model_calls == 1


@pytest.mark.asyncio
async def test_postgres_old_owner_cannot_finish_after_connection_loss(request):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        run_id = prepared(settings, engine, factory, client)
        worker = Worker(settings, engine)
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            host = host_for(worker, run_id)
            call = identity(run_id, "d")
            assert await host.reserve_invocation("model_calls", "coordinator", None, call)
            with owner.connection.begin():
                own_pid = owner.connection.scalar(text("select pg_backend_pid()"))
            with engine.begin() as other:
                assert other.scalar(text("select pg_terminate_backend(:pid)"), {"pid": own_pid}) is True
            with pytest.raises(WorkerOwnershipLost):
                await host.mark_invocation_uncertain("model_calls", "coordinator", None, call)
        with WorkerLock(engine, settings) as replacement:
            other_worker = Worker(settings, engine)
            other_worker.lock = replacement
            new_host = host_for(other_worker, run_id)
            with pytest.raises(AgentOperationNeedsReview):
                await new_host.reserve_invocation("model_calls", "coordinator", None, call)
        with factory() as db:
            op = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
            assert op.status == "running"
            assert db.get(RunBudget, run_id).model_calls == 1


@pytest.mark.asyncio
async def test_postgres_deepagents_host_and_fenced_checkpointer(request):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    database_url = os.environ["OMNI_TEST_POSTGRES_URL"]
    # The test passes authentication through PGPASSWORD; pytest traceback
    # locals must never contain the plaintext test credential.
    if make_url(database_url).password is not None:
        pytest.fail("Use a password-free test URL and PGPASSWORD", pytrace=False)
    checkpoint_schema = f"agenthost_{uuid4().hex}"
    await initialize_checkpoint_schema(database_url, schema=checkpoint_schema)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        run_id = prepared(settings, engine, factory, client)
        worker = Worker(settings, engine)

        @tool
        async def probe_work(label: str) -> str:
            """Perform a deterministic in-memory test only."""
            return "ok-" + label

        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            host = FencedAgentHost(worker, run_id, "agent-v1", role_versions=ROLE_VERSIONS,
                                   plugin_versions={}, role_bindings={"coordinator": "think", "child0": "plan",
                                                                     "child1": "execute"})
            ticket = register_fence(owner, run_id, "agent-v1", schema=checkpoint_schema)
            async with open_fenced_saver(database_url, ticket) as saver:
                agent = build_controlled_agent(coordinator=role("coordinator", "parent"),
                                               subagents=[role("child0", "child0", tools=[probe_work]),
                                                          role("child1", "child1", tools=[probe_work])],
                                               hooks=host, checkpointer=saver, require_durable_ids=True)
                with tracing_context(enabled=False):
                    result = await agent.ainvoke({"messages": [("user", "delegate test")]},
                                                 {"configurable": {"thread_id": run_id}}, durability="sync")
                snapshot = await agent.aget_state({"configurable": {"thread_id": run_id}})
                assert snapshot.values and not snapshot.next
            assert "ok-child0" in result["messages"][-1].content
            assert "ok-child1" in result["messages"][-1].content
            with factory() as db:
                budget = db.get(RunBudget, run_id)
                operations = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id)))
                assert (budget.model_calls, budget.tool_calls) == (6, 4)
                assert len(operations) == 8 and all(op.status == "completed" for op in operations)


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_point", ["before_reserve", "after_reserve"])
async def test_postgres_fenced_requirement_recovery_across_new_owner(request, crash_point):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    database_url = os.environ["OMNI_TEST_POSTGRES_URL"]
    if make_url(database_url).password is not None:
        pytest.fail("Use a password-free test URL and PGPASSWORD", pytrace=False)
    checkpoint_schema = f"reqhost_{uuid4().hex}"
    await initialize_checkpoint_schema(database_url, schema=checkpoint_schema)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        run_id = prepared(settings, engine, factory, client)
        with factory.begin() as db:
            saved = submit_command(db, run_id, RunCommandRequest(
                command_type="append_requirement", client_request_id="requirement-1", content="include the boundary case",
            ))
        calls = []

        class CaptureModel(LocalModel):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                calls.append([(m.id, m.content) for m in messages if isinstance(m, HumanMessage)])
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="completed"))])

        class CrashHost(FencedAgentHost):
            async def reserve_invocation(self, kind, role_name, tool_name, invocation):
                if crash_point == "after_reserve":
                    assert await super().reserve_invocation(kind, role_name, tool_name, invocation)
                raise RuntimeError("injected-crash")

        config = {"configurable": {"thread_id": run_id}}
        coordinator = AgentRole(name="coordinator", description="test", model=CaptureModel(scenario="capture"))
        first_worker = Worker(settings, engine)
        with WorkerLock(engine, settings) as first_owner:
            first_worker.lock = first_owner
            crashed = CrashHost(first_worker, run_id, "agent-v1", role_versions=ROLE_VERSIONS, plugin_versions={},
                                role_bindings={"coordinator": "think"})
            first_ticket = register_fence(first_owner, run_id, "agent-v1", schema=checkpoint_schema)
            async with open_fenced_saver(database_url, first_ticket) as saver:
                agent = build_controlled_agent(coordinator=coordinator, subagents=[], hooks=crashed,
                                               checkpointer=saver, require_durable_ids=True)
                with tracing_context(enabled=False), pytest.raises(AgentExternalCallFailed):
                    await agent.ainvoke({"messages": [("user", "do task")]}, config, durability="sync")
                snapshot = await agent.aget_state(config)
                command_id = f"omni-command:{saved['command_id']}"
                persisted = [m for m in snapshot.values["messages"]
                             if isinstance(m, HumanMessage) and m.id == command_id]
                assert len(persisted) == 1
        # A fresh owner connection, new fence epoch and new saver simulate
        # replacement without using another Worker or touching user data.
        second_worker = Worker(settings, engine)
        with WorkerLock(engine, settings) as second_owner:
            second_worker.lock = second_owner
            restored_host = host_for(second_worker, run_id)
            ticket = register_fence(second_owner, run_id, "agent-v1", schema=checkpoint_schema)
            assert ticket.epoch > first_ticket.epoch
            async with open_fenced_saver(database_url, ticket) as saver:
                restored = build_controlled_agent(coordinator=coordinator, subagents=[], hooks=restored_host,
                                                  checkpointer=saver, require_durable_ids=True)
                if crash_point == "before_reserve":
                    with tracing_context(enabled=False):
                        result = await restored.ainvoke(None, config, durability="sync")
                    assert result["messages"][-1].content == "completed"
                    assert len(calls) == 1 and calls[0].count((command_id, "include the boundary case")) == 1
                else:
                    with tracing_context(enabled=False), pytest.raises(AgentOperationNeedsReview):
                        await restored.ainvoke(None, config, durability="sync")
                    assert calls == []
        with factory() as db:
            assert db.get(RunCommand, saved["command_id"]).status == "applied"
            events = [event for event in db.scalars(select(RunEvent).where(RunEvent.run_id == run_id))
                      if event.event_type == "run.command" and event.payload["command_id"] == saved["command_id"]]
            assert len(events) == 1 and events[0].payload["delivery"] == "prepared_for_model"
            assert db.get(RunBudget, run_id).model_calls == 1
