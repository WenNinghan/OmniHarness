"""Opt-in product-path tests with deterministic models and dedicated PostgreSQL."""

import asyncio
import os
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from test_m3_agent_config import mark_tools, model
from test_postgres import postgres_environment  # noqa: F401 - isolated opt-in fixture

import omniharness.m3_worker as agent_worker
import omniharness.services as services
from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema
from omniharness.m3_delegation import DelegationDrainTimeout, HierarchicalDelegation
from omniharness.m3_host import FencedAgentHost
from omniharness.models import AgentRun, Message, OperationRecord, Run, RunBudget, RunEvent
from omniharness.schemas import RunCommandRequest
from omniharness.security import create_pairing_code
from omniharness.services import apply_commands, submit_command
from omniharness.worker import Worker, WorkerLock, WorkerOwnershipLost


class LocalAnswer(BaseChatModel):
    observed: Any = Field(default_factory=lambda: {"calls": []})

    @property
    def _llm_type(self):
        return "local-agent-product-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.observed["calls"].append([message.content for message in messages])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="local test answer"))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, **kwargs)


async def configured(request, monkeypatch):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    raw = os.environ["OMNI_TEST_POSTGRES_URL"]
    if make_url(raw).password is not None:
        pytest.fail("Use a password-free test URL and PGPASSWORD", pytrace=False)
    settings.enable_agent = True
    settings.checkpoint_schema = f"product_{uuid4().hex}"
    await initialize_checkpoint_schema(raw, schema=settings.checkpoint_schema)
    monkeypatch.setattr(services, "AGENT_RUNTIME_WIRED", True)
    # These tests exercise recovery of the earlier, persisted Deep Agents graph.
    # New product Runs use stage-agent-v1; seed the old version explicitly so
    # this suite continues to verify compatibility rather than the new graph.
    from omniharness.m3_roles import PLUGIN_VERSIONS, ROLE_VERSIONS, WORKFLOW_VERSION

    def freeze_legacy(db, run, app_settings, *, stage_overrides=None, strategy="staged"):
        assert stage_overrides is None and strategy == "staged"
        services.freeze_agent_run_config(db, run, app_settings, WORKFLOW_VERSION,
                                         role_versions=ROLE_VERSIONS, plugin_versions=PLUGIN_VERSIONS)

    monkeypatch.setattr(services, "freeze_stage_run_config", freeze_legacy)
    client = TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"})
    client.__enter__()
    assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
    configured_model = model(client, "test", url="http://127.0.0.1:11434/v1", key=None)
    mark_tools(factory, configured_model["id"])
    conversation = client.post("/api/v1/conversations", json={}).json()
    response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={
        "content": "perform task", "model_config_id": configured_model["id"],
        "client_request_id": "agent-1", "workflow_type": "agent",
    })
    assert response.status_code == 202
    observed = {"calls": []}
    worker = Worker(settings, engine)
    worker.agent_model_factory = lambda _role, _config: LocalAnswer(observed=observed)
    return settings, engine, factory, client, worker, response.json()["run_id"], observed


@pytest.mark.asyncio
async def test_context_growth_stops_before_next_model_call_and_reservation(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, observed = await configured(request, monkeypatch)

    class ExpandingModel(LocalAnswer):
        role_name: str

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.observed["calls"].append(self.role_name)
            if self.role_name == "coordinator":
                response = AIMessage(content="private-context-canary-" + "x" * 210_000, tool_calls=[{
                    "name": "task", "args": {"description": "research", "subagent_type": "researcher"},
                    "id": "context-child", "type": "tool_call",
                }])
            else:
                response = AIMessage(content="brief research result")
            return ChatResult(generations=[ChatGeneration(message=response)])

    worker.agent_model_factory = lambda role, _config: ExpandingModel(role_name=role, observed=observed)
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert await worker.run_once() == run_id
        with factory() as db:
            run, budget = db.get(Run, run_id), db.get(RunBudget, run_id)
            assert (run.status, run.error_code) == ("failed", "CONTEXT_LIMIT")
            assert (budget.model_calls, budget.tool_calls) == (2, 1)
            assert observed["calls"] == ["coordinator", "researcher"]
            assert "private-context-canary" not in run.error_message
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)
                                     .order_by(RunEvent.sequence_number)))
            assert events[-1].event_type == "run.failed"
            assert "private-context-canary" not in repr([event.payload for event in events])
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_parallel_context_limit_preserves_unknown_other_model_call(request, monkeypatch):
    from omniharness.m3_agents import SharedBoundaryMiddleware

    settings, engine, factory, client, worker, run_id, observed = await configured(request, monkeypatch)
    started = asyncio.Event()
    original = SharedBoundaryMiddleware.awrap_model_call
    child_boundaries = 0

    async def order_model_calls(self, model_request, handler):
        nonlocal child_boundaries
        if self.role == "researcher":
            child_boundaries += 1
            if child_boundaries == 2:
                await asyncio.wait_for(started.wait(), timeout=10)
                model_request = model_request.override(messages=[
                    *model_request.messages, HumanMessage(content="oversized-context-" + "x" * 210_000),
                ])
        return await original(self, model_request, handler)

    class ParallelContextModel(LocalAnswer):
        role_name: str

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            self.observed["calls"].append(self.role_name)
            if self.role_name == "coordinator":
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
                    {"name": "task", "args": {"description": "small", "subagent_type": "researcher"},
                     "id": "context-small", "type": "tool_call"},
                    {"name": "task", "args": {"description": "other", "subagent_type": "researcher"},
                     "id": "context-large", "type": "tool_call"},
                ]))])
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("The in-flight call must be cancelled")

    worker.agent_model_factory = lambda role, _config: ParallelContextModel(role_name=role, observed=observed)
    monkeypatch.setattr(SharedBoundaryMiddleware, "awrap_model_call", order_model_calls)
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert await asyncio.wait_for(worker.run_once(), timeout=25) == run_id
        with factory() as db:
            run, budget = db.get(Run, run_id), db.get(RunBudget, run_id)
            assert (run.status, run.error_code) == ("pending_review", "CONTEXT_LIMIT")
            assert (budget.model_calls, budget.tool_calls) == (2, 2)
            assert observed["calls"].count("researcher") == 1
            assert "oversized-context-" not in run.error_message
            operations = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id)))
            assert any(operation.status == "uncertain" for operation in operations)
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)
                                     .order_by(RunEvent.sequence_number)))
            assert events[-1].event_type == "run.pending_review"
            assert "oversized-context-" not in repr([event.payload for event in events])
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_model_budget_exhaustion_is_reported_as_budget_exceeded(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, observed = await configured(request, monkeypatch)

    class DelegateOnce(LocalAnswer):
        role_name: str

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.observed["calls"].append(self.role_name)
            response = AIMessage(content="", tool_calls=[{
                "name": "task", "args": {"description": "research", "subagent_type": "researcher"},
                "id": "budget-child", "type": "tool_call",
            }])
            return ChatResult(generations=[ChatGeneration(message=response)])

    worker.agent_model_factory = lambda role, _config: DelegateOnce(role_name=role, observed=observed)
    try:
        with factory.begin() as db:
            db.get(RunBudget, run_id).max_model_calls = 1
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert await worker.run_once() == run_id
        with factory() as db:
            run, budget = db.get(Run, run_id), db.get(RunBudget, run_id)
            assert (run.status, run.error_code) == ("failed", "BUDGET_EXCEEDED")
            assert "预算上限" in run.error_message
            assert (budget.model_calls, budget.tool_calls) == (1, 1)
            assert observed["calls"] == ["coordinator"]
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)
                                     .order_by(RunEvent.sequence_number)))
            assert events[-1].event_type == "run.failed"
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", ["model_calls", "tool_calls"])
async def test_parallel_budget_exhaustion_preserves_unknown_inflight_call(request, monkeypatch, exhausted):
    settings, engine, factory, client, worker, run_id, _ = await configured(request, monkeypatch)
    started = asyncio.Event()
    counters = {"contended_reservations": 0, "research_calls": 0}

    class ParallelModel(LocalAnswer):
        role_name: str

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if self.role_name == "coordinator":
                response = AIMessage(content="", tool_calls=[{
                    "name": "task", "args": {"description": f"research-{index}",
                                               "subagent_type": "researcher"},
                    "id": f"budget-child-{index}", "type": "tool_call",
                } for index in range(2)])
                return ChatResult(generations=[ChatGeneration(message=response)])
            counters["research_calls"] += 1
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("Cancelled call must not return")

    class OrderedHost(FencedAgentHost):
        async def reserve_invocation(self, kind, role, tool_name, identity):
            target = ((exhausted == "model_calls" and kind == "model_calls" and role == "researcher")
                      or (exhausted == "tool_calls" and kind == "tool_calls" and tool_name == "task"))
            if target:
                counters["contended_reservations"] += 1
                if counters["contended_reservations"] == 2:
                    await asyncio.wait_for(started.wait(), timeout=5)
            return await super().reserve_invocation(kind, role, tool_name, identity)

    worker.agent_model_factory = lambda role, _config: ParallelModel(role_name=role)
    monkeypatch.setattr(agent_worker, "FencedAgentHost", OrderedHost)
    try:
        with factory.begin() as db:
            budget = db.get(RunBudget, run_id)
            if exhausted == "model_calls":
                budget.max_model_calls = 2
            else:
                budget.max_tool_calls = 1
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert await asyncio.wait_for(worker.run_once(), timeout=15) == run_id
        with factory() as db:
            run, budget = db.get(Run, run_id), db.get(RunBudget, run_id)
            assert (run.status, run.error_code) == ("pending_review", "BUDGET_EXCEEDED")
            assert "预算上限" in run.error_message and "核对" in run.error_message
            assert budget.model_calls == 2 and counters["research_calls"] == 1
            assert budget.tool_calls == (2 if exhausted == "model_calls" else 1)
            operations = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id)))
            assert any(operation.status == "uncertain" for operation in operations)
            nodes = list(db.scalars(select(AgentRun).where(AgentRun.run_id == run_id)))
            assert not any(node.status == "running" for node in nodes)
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)
                                     .order_by(RunEvent.sequence_number)))
            assert events[-1].event_type == "run.pending_review"
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_product_agent_pause_resume_keeps_budget_and_real_model_node(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, observed = await configured(request, monkeypatch)
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            with factory.begin() as db:
                submit_command(db, run_id, RunCommandRequest(command_type="pause", client_request_id="pause"))
            await worker.process(run_id)
            with factory() as db:
                run, budget = db.get(Run, run_id), db.get(RunBudget, run_id)
                assert run.status == "paused" and observed["calls"] == []
                assert budget.model_calls == 0 and budget.active_seconds >= 1 and not budget.active_segment_open
            with factory.begin() as db:
                submit_command(db, run_id, RunCommandRequest(command_type="resume", client_request_id="resume"))
            assert await worker.run_once() == run_id
        with factory() as db:
            run, budget = db.get(Run, run_id), db.get(RunBudget, run_id)
            assert run.status == "completed" and budget.model_calls == 1 and budget.active_seconds >= 2
            assert not budget.active_segment_open
            assert db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant")).content == "local test answer"
            nodes = list(db.scalars(select(AgentRun).where(AgentRun.run_id == run_id)))
            assert len(nodes) == 1 and nodes[0].role == "coordinator" and nodes[0].status == "completed"
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence_number)))
            assert events[-1].event_type == "run.completed"
        assert client.get(f"/api/v1/runs/{run_id}/agent-runs").json()[0]["role"] == "coordinator"
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_pause_checkpoint_saved_then_worker_crash_recovers_paused_without_replay(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, observed = await configured(request, monkeypatch)

    class Crash(BaseException):
        pass

    original_pause = agent_worker._pause

    def crash_before_business_update(*_args, **_kwargs):
        raise Crash()

    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            with factory.begin() as db:
                submit_command(db, run_id, RunCommandRequest(command_type="pause", client_request_id="pause"))
            monkeypatch.setattr(agent_worker, "_pause", crash_before_business_update)
            with pytest.raises(Crash):
                await worker.process(run_id)
        monkeypatch.setattr(agent_worker, "_pause", original_pause)
        replacement = Worker(settings, engine)
        replacement.agent_model_factory = worker.agent_model_factory
        with WorkerLock(engine, settings) as owner:
            replacement.lock = owner
            await agent_worker.recover_agents(replacement)
            with factory() as db:
                assert db.get(Run, run_id).status == "paused"
                assert db.get(RunBudget, run_id).active_segment_open is True
            with factory.begin() as db:
                submit_command(db, run_id, RunCommandRequest(command_type="resume", client_request_id="resume"))
            assert await replacement.run_once() == run_id
        with factory() as db:
            assert db.get(Run, run_id).status == "pending_review"
            assert db.get(Run, run_id).error_code == "ACTIVE_TIME_UNCERTAIN"
            assert db.get(RunBudget, run_id).model_calls == 0 and observed["calls"] == []
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_resume_racing_checkpoint_pause_does_not_leave_phantom_running(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, observed = await configured(request, monkeypatch)
    original_pause = agent_worker._pause

    def race_pause(active_worker, identity, reason, *, started):
        with factory.begin() as db:
            submit_command(db, identity, RunCommandRequest(command_type="resume", client_request_id="racing-resume"))
            apply_commands(db, identity, defer_requirements=True)
        return original_pause(active_worker, identity, reason, started=started)

    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            with factory.begin() as db:
                submit_command(db, run_id, RunCommandRequest(command_type="pause", client_request_id="pause"))
            monkeypatch.setattr(agent_worker, "_pause", race_pause)
            await worker.process(run_id)
        with factory() as db:
            assert db.get(Run, run_id).status == "completed"
            assert db.get(RunBudget, run_id).model_calls == 1 and len(observed["calls"]) == 1
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_subagent_interrupt_fresh_worker_resume_verifies_matching_child_checkpoint(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, observed = await configured(request, monkeypatch)
    injected = []

    class DelegatingModel(LocalAnswer):
        role_name: str

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.observed["calls"].append(self.role_name)
            if self.role_name == "coordinator" and not any(isinstance(message, ToolMessage) for message in messages):
                response = AIMessage(content="", tool_calls=[{
                    "name": "task", "args": {"description": "research the supplied prompt",
                                               "subagent_type": "researcher"},
                    "id": "delegate-researcher", "type": "tool_call",
                }])
            else:
                response = AIMessage(content="research complete" if self.role_name == "researcher" else "final answer")
            return ChatResult(generations=[ChatGeneration(message=response)])

    class PauseResearcher(FencedAgentHost):
        async def check_control(self, kind, role, tool_name):
            if role == "researcher" and kind == "model_calls" and not injected:
                with factory.begin() as db:
                    submit_command(db, run_id, RunCommandRequest(command_type="pause", client_request_id="child-pause"))
                injected.append(True)
            return await super().check_control(kind, role, tool_name)

    worker.agent_model_factory = lambda role, _config: DelegatingModel(role_name=role, observed=observed)
    monkeypatch.setattr(agent_worker, "FencedAgentHost", PauseResearcher)
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            await worker.process(run_id)
        with factory() as db:
            assert db.get(Run, run_id).status == "paused"
            assert db.get(RunBudget, run_id).model_calls == 1
            assert db.get(RunBudget, run_id).tool_calls == 1
            assert not db.get(RunBudget, run_id).active_segment_open
        with factory.begin() as db:
            submit_command(db, run_id, RunCommandRequest(command_type="resume", client_request_id="resume"))
        replacement = Worker(settings, engine)
        replacement.agent_model_factory = worker.agent_model_factory
        with WorkerLock(engine, settings) as owner:
            replacement.lock = owner
            assert await replacement.run_once() == run_id
        with factory() as db:
            run, budget = db.get(Run, run_id), db.get(RunBudget, run_id)
            assert run.status == "completed" and run.thread_id == run_id
            assert (budget.model_calls, budget.tool_calls) == (3, 1)
            assert observed["calls"] == ["coordinator", "researcher", "coordinator"]
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
            assert sum(event.event_type == "agent.delegated" for event in events) == 1
            nodes = list(db.scalars(select(AgentRun).where(AgentRun.run_id == run_id)))
            assert [node.role for node in nodes].count("researcher") == 1
            assert all(node.status == "completed" for node in nodes)
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_grandchild_pause_new_owner_resumes_exact_two_level_chain(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, observed = await configured(request, monkeypatch)
    injected = []

    class NestedModel(LocalAnswer):
        role_name: str

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.observed["calls"].append(self.role_name)
            targets = {"coordinator": "researcher", "researcher": "coder"}
            target = targets.get(self.role_name)
            if target and not any(isinstance(message, ToolMessage) for message in messages):
                response = AIMessage(content="", tool_calls=[{
                    "name": "task", "args": {"description": "nested work", "subagent_type": target},
                    "id": f"to-{target}", "type": "tool_call",
                }])
            else:
                response = AIMessage(content=f"{self.role_name} complete")
            return ChatResult(generations=[ChatGeneration(message=response)])

    class PauseGrandchild(FencedAgentHost):
        async def check_control(self, kind, role, tool_name):
            if role == "coder" and kind == "model_calls" and not injected:
                with factory.begin() as db:
                    submit_command(db, run_id, RunCommandRequest(command_type="pause", client_request_id="leaf-pause"))
                injected.append(True)
            return await super().check_control(kind, role, tool_name)

    worker.agent_model_factory = lambda role, _config: NestedModel(role_name=role, observed=observed)
    monkeypatch.setattr(agent_worker, "FencedAgentHost", PauseGrandchild)
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            await worker.process(run_id)
        with factory() as db:
            assert db.get(Run, run_id).status == "paused"
            assert (db.get(RunBudget, run_id).model_calls, db.get(RunBudget, run_id).tool_calls) == (2, 2)
        with factory.begin() as db:
            submit_command(db, run_id, RunCommandRequest(command_type="resume", client_request_id="resume"))
        replacement = Worker(settings, engine)
        replacement.agent_model_factory = worker.agent_model_factory
        with WorkerLock(engine, settings) as owner:
            replacement.lock = owner
            assert await replacement.run_once() == run_id
        with factory() as db:
            run, budget = db.get(Run, run_id), db.get(RunBudget, run_id)
            assert run.status == "completed"
            assert (budget.model_calls, budget.tool_calls) == (5, 2)
            assert observed["calls"] == ["coordinator", "researcher", "coder", "researcher", "coordinator"]
            nodes = list(db.scalars(select(AgentRun).where(AgentRun.run_id == run_id)))
            assert [(n.role, n.depth) for n in nodes].count(("coder", 2)) == 1
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_two_nested_branches_share_global_active_limit(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, _ = await configured(request, monkeypatch)
    leaves_started = asyncio.Event()
    activity = {"leaves": 0, "max_leaves": 0, "max_active": 0}

    class ObservedScheduler(HierarchicalDelegation):
        async def _acquire(self):
            await super()._acquire()
            activity["max_active"] = max(activity["max_active"], self.active_count)

    class ParallelTree(LocalAnswer):
        role_name: str

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            prior = any(isinstance(message, ToolMessage) for message in messages)
            targets = (["researcher", "reviewer"] if self.role_name == "coordinator" else ["coder"])
            if self.role_name != "coder" and not prior:
                response = AIMessage(content="", tool_calls=[{
                    "name": "task", "args": {"description": f"{self.role_name} branch {i}",
                                               "subagent_type": target},
                    "id": f"{self.role_name}-{i}", "type": "tool_call",
                } for i, target in enumerate(targets)])
            else:
                if self.role_name == "coder":
                    activity["leaves"] += 1
                    activity["max_leaves"] = max(activity["max_leaves"], activity["leaves"])
                    if activity["leaves"] == 2:
                        leaves_started.set()
                    try:
                        await asyncio.wait_for(leaves_started.wait(), timeout=10)
                    finally:
                        activity["leaves"] -= 1
                response = AIMessage(content=f"{self.role_name} done")
            return ChatResult(generations=[ChatGeneration(message=response)])

    worker.agent_model_factory = lambda role, _config: ParallelTree(role_name=role)
    monkeypatch.setattr(agent_worker, "HierarchicalDelegation", ObservedScheduler)
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            await asyncio.wait_for(worker.process(run_id), timeout=30)
        with factory() as db:
            assert db.get(Run, run_id).status == "completed"
            nodes = list(db.scalars(select(AgentRun).where(AgentRun.run_id == run_id, AgentRun.role == "coder")))
            assert len(nodes) == 2 and all(node.depth == 2 for node in nodes)
        assert activity["max_active"] == 2 and activity["max_leaves"] == 2
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_nested_completed_branch_is_not_replayed_after_sibling_pause(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, observed = await configured(request, monkeypatch)
    waiting = asyncio.Event()

    class TreeModel(LocalAnswer):
        role_name: str

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.observed["calls"].append(self.role_name)
            prior = any(isinstance(message, ToolMessage) for message in messages)
            targets = (["researcher", "reviewer"] if self.role_name == "coordinator" else ["coder"])
            if self.role_name != "coder" and not prior:
                response = AIMessage(content="", tool_calls=[{
                    "name": "task", "args": {"description": f"{self.role_name} work {i}", "subagent_type": target},
                    "id": f"{self.role_name}-{i}", "type": "tool_call",
                } for i, target in enumerate(targets)])
            else:
                response = AIMessage(content=f"{self.role_name} complete")
            return ChatResult(generations=[ChatGeneration(message=response)])

    class PauseAfterNestedSibling(FencedAgentHost):
        async def check_control(self, kind, role, tool_name):
            if role == "coder" and kind == "model_calls" and not waiting.is_set():
                waiting.set()
                for _ in range(300):
                    with factory() as db:
                        nodes = list(db.scalars(select(AgentRun).where(
                            AgentRun.run_id == run_id, AgentRun.role.in_(["researcher", "reviewer"]),
                            AgentRun.status == "completed")))
                    if len(nodes) >= 3:
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise RuntimeError("nested sibling did not finish")
                await asyncio.sleep(0.1)
                with factory.begin() as db:
                    submit_command(db, run_id, RunCommandRequest(command_type="pause", client_request_id="nested-pause"))
            return await super().check_control(kind, role, tool_name)

    worker.agent_model_factory = lambda role, _config: TreeModel(role_name=role, observed=observed)
    monkeypatch.setattr(agent_worker, "FencedAgentHost", PauseAfterNestedSibling)
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            await asyncio.wait_for(worker.process(run_id), timeout=30)
        with factory() as db:
            assert db.get(Run, run_id).status == "paused"
            assert observed["calls"].count("coder") == 1
        with factory.begin() as db:
            submit_command(db, run_id, RunCommandRequest(command_type="resume", client_request_id="resume"))
        replacement = Worker(settings, engine)
        replacement.agent_model_factory = worker.agent_model_factory
        with WorkerLock(engine, settings) as owner:
            replacement.lock = owner
            assert await asyncio.wait_for(replacement.run_once(), timeout=30) == run_id
        with factory() as db:
            assert db.get(Run, run_id).status == "completed"
            assert observed["calls"].count("coder") == 2
            assert (db.get(RunBudget, run_id).model_calls, db.get(RunBudget, run_id).tool_calls) == (8, 4)
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_nested_inflight_model_owner_loss_never_replays(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, _ = await configured(request, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class FencedTree(LocalAnswer):
        role_name: str

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append(self.role_name)
            target = {"coordinator": "researcher", "researcher": "coder"}.get(self.role_name)
            if target:
                response = AIMessage(content="", tool_calls=[{
                    "name": "task", "args": {"description": "nested", "subagent_type": target},
                    "id": f"to-{target}", "type": "tool_call",
                }])
            else:
                entered.set()
                await release.wait()
                response = AIMessage(content="result")
            return ChatResult(generations=[ChatGeneration(message=response)])

    worker.agent_model_factory = lambda role, _config: FencedTree(role_name=role)
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            execution = asyncio.create_task(worker.process(run_id))
            await asyncio.wait_for(entered.wait(), timeout=15)
            with owner.connection.begin():
                owner_pid = owner.connection.scalar(text("select pg_backend_pid()"))
            with engine.begin() as other:
                assert other.scalar(text("select pg_terminate_backend(:pid)"), {"pid": owner_pid}) is True
            release.set()
            with pytest.raises(WorkerOwnershipLost):
                await asyncio.wait_for(execution, timeout=15)
        replacement = Worker(settings, engine)
        replacement.agent_model_factory = worker.agent_model_factory
        with WorkerLock(engine, settings) as owner:
            replacement.lock = owner
            await agent_worker.recover_agents(replacement)
        with factory() as db:
            assert db.get(Run, run_id).status == "pending_review"
            assert calls == ["coordinator", "researcher", "coder"]
            assert db.get(RunBudget, run_id).model_calls == 3
    finally:
        release.set()
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_cancel_after_model_return_before_finish_marks_node_and_operation_unknown(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, observed = await configured(request, monkeypatch)
    arrived = asyncio.Event()

    class BlockCompletion(FencedAgentHost):
        async def finish_invocation(self, kind, role, tool_name, identity):
            if kind == "model_calls":
                arrived.set()
                await asyncio.Event().wait()
            return await super().finish_invocation(kind, role, tool_name, identity)

    monkeypatch.setattr(agent_worker, "FencedAgentHost", BlockCompletion)
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            executing = asyncio.create_task(worker.process(run_id))
            await asyncio.wait_for(arrived.wait(), timeout=15)
            assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
            await asyncio.wait_for(executing, timeout=15)
        with factory() as db:
            run = db.get(Run, run_id)
            node = db.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            operation = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)
                                     .order_by(RunEvent.sequence_number)))
            assert run.status == "cancelled" and len(observed["calls"]) == 1
            assert node.status == "pending_review" and operation.status == "uncertain"
            assert events[-1].event_type == "run.cancelled"
            assert any(event.event_type == "agent.model_uncertain" for event in events[:-1])
            assert not db.get(RunBudget, run_id).active_segment_open
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_worker_repeated_cancel_drains_parallel_providers_before_terminal(request, monkeypatch, nested):
    settings, engine, factory, client, worker, run_id, _ = await configured(request, monkeypatch)
    entered, cleaning, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    counts = {"entered": 0, "cleaning": 0, "closed": 0}

    class BlockingModel(LocalAnswer):
        role_name: str

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if self.role_name == "coordinator":
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
                    {"name": "task", "args": {"description": f"child {i}", "subagent_type": "researcher"},
                     "id": f"parallel-{i}", "type": "tool_call"} for i in range(2)
                ]))])
            if nested and self.role_name == "researcher":
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
                    "name": "task", "args": {"description": "nested work", "subagent_type": "coder"},
                    "id": "nested-coder", "type": "tool_call",
                }]))])
            counts["entered"] += 1
            if counts["entered"] == 2:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                counts["cleaning"] += 1
                if counts["cleaning"] == 2:
                    cleaning.set()
                try:
                    await release.wait()
                finally:
                    counts["closed"] += 1
                    if counts["closed"] == 2:
                        closed.set()

    worker.agent_model_factory = lambda role, _config: BlockingModel(role_name=role)
    executing = None
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            executing = asyncio.create_task(worker.process(run_id))
            await asyncio.wait_for(entered.wait(), timeout=15)
            assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
            await asyncio.wait_for(cleaning.wait(), timeout=15)
            executing.cancel()
            executing.cancel()
            await asyncio.sleep(0.1)
            assert not executing.done()
            with factory() as db:
                assert db.get(Run, run_id).status not in {"completed", "cancelled", "failed"}
            release.set()
            await asyncio.gather(executing, return_exceptions=True)
            await asyncio.wait_for(closed.wait(), timeout=5)
    finally:
        release.set()
        if executing:
            await asyncio.gather(executing, return_exceptions=True)
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_worker_drain_timeout_does_not_mark_terminal_or_continue(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, _ = await configured(request, monkeypatch)
    entered, cleaning, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowCleanupModel(LocalAnswer):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                try:
                    await release.wait()
                finally:
                    closed.set()

    class ShortDrain(HierarchicalDelegation):
        async def aclose(self, *, timeout=10.0):
            return await super().aclose(timeout=0.05)

    worker.agent_model_factory = lambda _role, _config: SlowCleanupModel()
    monkeypatch.setattr(agent_worker, "HierarchicalDelegation", ShortDrain)
    executing = None
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            executing = asyncio.create_task(worker.process(run_id))
            await asyncio.wait_for(entered.wait(), timeout=15)
            assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
            await asyncio.wait_for(cleaning.wait(), timeout=15)
            with pytest.raises(DelegationDrainTimeout):
                await asyncio.wait_for(executing, timeout=15)
            with factory() as db:
                assert db.get(Run, run_id).status not in {"completed", "cancelled", "failed"}
                assert not any(event.event_type in {"run.completed", "run.cancelled", "run.failed"}
                               for event in db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
            release.set()
            await asyncio.wait_for(closed.wait(), timeout=5)
    finally:
        release.set()
        if executing:
            await asyncio.gather(executing, return_exceptions=True)
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_parallel_same_role_supplement_reaches_next_child_and_coordinator_once(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, _ = await configured(request, monkeypatch)
    arrived, proceed = asyncio.Event(), asyncio.Event()
    observed = {"calls": []}

    class ParallelModel(LocalAnswer):
        role_name: str

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            requirements = [m.content for m in messages if isinstance(m, HumanMessage)
                            and isinstance(m.id, str) and m.id.startswith("omni-command:")]
            self.observed["calls"].append((self.role_name, requirements))
            if self.role_name == "coordinator" and not any(isinstance(m, ToolMessage) for m in messages):
                response = AIMessage(content="", tool_calls=[{
                    "name": "task", "args": {"description": f"research {i}", "subagent_type": "researcher"},
                    "id": f"research-{i}", "type": "tool_call",
                } for i in range(2)])
            else:
                response = AIMessage(content="research complete" if self.role_name == "researcher" else "final answer")
            return ChatResult(generations=[ChatGeneration(message=response)])

    class ControlledHost(FencedAgentHost):
        async def stage_requirements(self, role_name, existing_messages):
            if role_name == "researcher" and not arrived.is_set():
                arrived.set()
                await proceed.wait()
            return await super().stage_requirements(role_name, existing_messages)

    worker.agent_model_factory = lambda role_name, _config: ParallelModel(role_name=role_name, observed=observed)
    monkeypatch.setattr(agent_worker, "FencedAgentHost", ControlledHost)
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            executing = asyncio.create_task(worker.process(run_id))
            await asyncio.wait_for(arrived.wait(), timeout=15)
            response = client.post(f"/api/v1/runs/{run_id}/commands", json={
                "command_type": "append_requirement", "client_request_id": "new-requirement",
                "content": "include boundary case",
            })
            assert response.status_code == 202
            proceed.set()
            await asyncio.wait_for(executing, timeout=30)
        with factory() as db:
            run, budget = db.get(Run, run_id), db.get(RunBudget, run_id)
            assert run.status == "completed"
            assert (budget.model_calls, budget.tool_calls) == (4, 2)
            nodes = list(db.scalars(select(AgentRun).where(AgentRun.run_id == run_id)))
            children = [node for node in nodes if node.role == "researcher"]
            assert len(children) == 2 and len({node.id for node in children}) == 2
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
            delegations = [event for event in events if event.event_type == "agent.delegated"]
            assert len(delegations) == 2 and len({event.payload["invocation_id"] for event in delegations}) == 2
            command_events = [event for event in events if event.event_type == "run.command"
                              and event.payload.get("command_id") == response.json()["command_id"]]
            assert len(command_events) == 1 and command_events[0].payload["delivery"] == "prepared_for_model"
        research_calls = [requirements for role_name, requirements in observed["calls"] if role_name == "researcher"]
        assert len(research_calls) == 2 and any(reqs == ["include boundary case"] for reqs in research_calls)
        coordinator_calls = [requirements for role_name, requirements in observed["calls"] if role_name == "coordinator"]
        assert len(coordinator_calls) == 2 and coordinator_calls[-1] == ["include boundary case"]
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_completed_same_role_sibling_is_not_replayed_after_other_child_pauses(request, monkeypatch):
    settings, engine, factory, client, worker, run_id, _ = await configured(request, monkeypatch)
    first_waiting, already_injected = asyncio.Event(), []
    observed = {"calls": []}

    class ParallelModel(LocalAnswer):
        role_name: str

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.observed["calls"].append(self.role_name)
            if self.role_name == "coordinator" and not any(isinstance(m, ToolMessage) for m in messages):
                response = AIMessage(content="", tool_calls=[{
                    "name": "task", "args": {"description": f"research {i}", "subagent_type": "researcher"},
                    "id": f"research-{i}", "type": "tool_call",
                } for i in range(2)])
            else:
                response = AIMessage(content="research complete" if self.role_name == "researcher" else "final answer")
            return ChatResult(generations=[ChatGeneration(message=response)])

    class PauseAfterSibling(FencedAgentHost):
        async def check_control(self, kind, role, tool_name):
            if role == "researcher" and kind == "model_calls" and not first_waiting.is_set():
                first_waiting.set()
                for _ in range(300):
                    with factory() as db:
                        sibling_done = db.scalar(select(AgentRun.id).where(
                            AgentRun.run_id == run_id, AgentRun.role == "researcher", AgentRun.status == "completed"))
                    if sibling_done:
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise RuntimeError("sibling did not finish")
                # Allow the framework to checkpoint the completed sibling's
                # task result before interrupting the other subgraph.
                await asyncio.sleep(0.1)
                with factory.begin() as db:
                    submit_command(db, run_id, RunCommandRequest(command_type="pause", client_request_id="pause-one"))
                already_injected.append(True)
            return await super().check_control(kind, role, tool_name)

    worker.agent_model_factory = lambda role_name, _config: ParallelModel(role_name=role_name, observed=observed)
    monkeypatch.setattr(agent_worker, "FencedAgentHost", PauseAfterSibling)
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            await worker.process(run_id)
        with factory() as db:
            assert already_injected == [True] and db.get(Run, run_id).status == "paused"
            assert observed["calls"].count("researcher") == 1
            assert db.get(RunBudget, run_id).tool_calls == 2
        with factory.begin() as db:
            submit_command(db, run_id, RunCommandRequest(command_type="resume", client_request_id="resume"))
        replacement = Worker(settings, engine)
        replacement.agent_model_factory = worker.agent_model_factory
        with WorkerLock(engine, settings) as owner:
            replacement.lock = owner
            assert await replacement.run_once() == run_id
        with factory() as db:
            run, budget = db.get(Run, run_id), db.get(RunBudget, run_id)
            assert run.status == "completed"
            assert observed["calls"].count("researcher") == 2
            assert (budget.model_calls, budget.tool_calls) == (4, 2)
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
            assert sum(event.event_type == "agent.delegated" for event in events) == 2
            children = [node for node in db.scalars(select(AgentRun).where(AgentRun.run_id == run_id))
                        if node.role == "researcher"]
            assert len(children) == 2 and children[0].id != children[1].id
    finally:
        client.__exit__(None, None, None)
