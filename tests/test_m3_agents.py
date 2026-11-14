"""Real Deep Agents with deterministic local models; never a live provider test."""

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langsmith import tracing_context
from pydantic import Field

from omniharness.m3_agents import (
    AgentBudgetExceeded,
    AgentContextLimitExceeded,
    AgentExternalCallFailed,
    AgentRole,
    AgentToolDenied,
    SharedBoundaryMiddleware,
    build_controlled_agent,
    validate_model_context,
)
from omniharness.m3_delegation import HierarchicalDelegation


@dataclass
class Hooks:
    max_models: int = 100
    max_tools: int = 100
    model_calls: int = 0
    tool_calls: int = 0
    controls: list = field(default_factory=list)
    reservations: list = field(default_factory=list)
    paused: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def check_control(self, kind, role, tool_name):
        self.controls.append((kind, role, tool_name))
        if self.paused:
            raise RuntimeError("PAUSED")

    async def reserve(self, kind, role, tool_name):
        async with self.lock:
            self.reservations.append((kind, role, tool_name))
            if kind == "model_calls":
                if self.model_calls >= self.max_models:
                    return False
                self.model_calls += 1
            elif kind == "tool_calls":
                if self.tool_calls >= self.max_tools:
                    return False
                self.tool_calls += 1
            else:
                raise AssertionError("Unexpected budget kind")
            return True


class LocalModel(BaseChatModel):
    scenario: str
    delegates: int = 2
    api_key: str = Field(default="", repr=False)

    @property
    def _llm_type(self):
        return "local-m3-control-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        prior = [message for message in messages if isinstance(message, ToolMessage)]
        if prior:
            response = AIMessage(content="finished " + " | ".join(str(m.content) for m in prior))
        elif self.scenario == "parent":
            response = AIMessage(content="", tool_calls=[{
                "name": "task", "args": {"description": f"Probe {index}", "subagent_type": f"child{index}"},
                "id": f"task-{index}", "type": "tool_call",
            } for index in range(self.delegates)])
        elif self.scenario == "unknown":
            response = AIMessage(content="", tool_calls=[{
                "name": "write_file", "args": {"file_path": "/secret", "content": "wrong"},
                "id": "denied-tool", "type": "tool_call",
            }])
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "probe_work", "args": {"label": self.scenario},
                "id": f"work-{self.scenario}", "type": "tool_call",
            }])
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, **kwargs)


def test_context_limit_counts_system_and_structured_content_without_echoing_secret():
    secret = "context-canary-never-echo-5b71"
    request = SimpleNamespace(messages=[HumanMessage(content=[{"type": "text", "text": secret}])],
                              system_message=SystemMessage(content="system"))
    validate_model_context(request, 2, 2000)
    with pytest.raises(AgentContextLimitExceeded) as error:
        validate_model_context(request, 1, 2000)
    assert secret not in str(error.value)
    with pytest.raises(AgentContextLimitExceeded) as error:
        validate_model_context(request, 2, 10)
    assert secret not in str(error.value)


@pytest.mark.asyncio
async def test_subagent_context_limit_stops_before_reservation_and_dispatch():
    hooks = Hooks()
    called = []
    middleware = SharedBoundaryMiddleware(
        hooks=hooks, role="child", allowed_tools=frozenset(), allowed_agents=frozenset(),
        semaphore=asyncio.Semaphore(2), parent=False, max_context_messages=2, max_context_characters=120,
    )
    request = SimpleNamespace(messages=[HumanMessage(content="private-canary-" + "x" * 150)],
                              system_message=SystemMessage(content="system"))

    async def handler(_request):
        called.append(True)

    with pytest.raises(AgentContextLimitExceeded) as error:
        await middleware.awrap_model_call(request, handler)
    assert "private-canary" not in str(error.value)
    assert called == [] and hooks.model_calls == 0 and hooks.reservations == []


def role(name, scenario, *, tools=(), delegates=2):
    return AgentRole(name=name, description=f"local test role {name}",
                     model=LocalModel(scenario=scenario, delegates=delegates), tools=tuple(tools))


@pytest.mark.asyncio
@pytest.mark.parametrize("child_count", [1, 2])
async def test_repeated_cancel_waits_for_inflight_child_model_to_finish(child_count):
    entered = asyncio.Event()
    cancelling = asyncio.Event()
    allow_cleanup = asyncio.Event()
    closed = asyncio.Event()
    provider_tasks = []
    cancelling_tasks = []
    closed_tasks = []

    class BlockingModel(LocalModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            provider_tasks.append(asyncio.current_task())
            if len(provider_tasks) == child_count:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelling_tasks.append(asyncio.current_task())
                if len(cancelling_tasks) == child_count:
                    cancelling.set()
                try:
                    await allow_cleanup.wait()
                finally:
                    closed_tasks.append(asyncio.current_task())
                    if len(closed_tasks) == child_count:
                        closed.set()

    scheduler = HierarchicalDelegation(max_active=2, max_depth=1)
    agent = build_controlled_agent(
        coordinator=role("coordinator", "parent", delegates=child_count),
        subagents=[AgentRole(name=f"child{i}", description="blocking provider", model=BlockingModel(scenario="child"))
                   for i in range(child_count)],
        hooks=Hooks(), scheduler=scheduler,
    )
    with tracing_context(enabled=False):
        execution = asyncio.create_task(agent.ainvoke({"messages": [("user", "delegate")]}))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            execution.cancel()
            await asyncio.wait_for(cancelling.wait(), timeout=5)
            execution.cancel()
            await asyncio.sleep(0.05)
            drain = asyncio.create_task(scheduler.aclose(timeout=5))
            await asyncio.sleep(0.05)
            drain_finished_before_child = drain.done() and not closed.is_set()
        finally:
            allow_cleanup.set()
            await asyncio.gather(execution, return_exceptions=True)
            await drain
            await asyncio.wait_for(closed.wait(), timeout=5)
            await asyncio.sleep(0)
    assert not drain_finished_before_child, "drain completed while its child provider was still cleaning up"
    assert all(task.done() for task in provider_tasks)


@pytest.mark.asyncio
async def test_shared_hooks_real_subagents_and_maximum_two_concurrent():
    hooks = Hooks()
    activity: dict[str, Any] = {"active": 0, "maximum": 0, "seen": []}
    both_active = asyncio.Event()

    @tool
    async def probe_work(label: str) -> str:
        """A harmless deterministic probe."""
        activity["active"] += 1
        activity["maximum"] = max(activity["maximum"], activity["active"])
        if activity["active"] == 2:
            both_active.set()
        try:
            await asyncio.wait_for(both_active.wait(), timeout=8)
            await asyncio.sleep(0.02)
            activity["seen"].append(label)
            return "ok-" + label
        finally:
            activity["active"] -= 1

    agent = build_controlled_agent(
        coordinator=role("coordinator", "parent", delegates=3),
        subagents=[role(f"child{i}", f"child{i}", tools=[probe_work]) for i in range(3)],
        hooks=hooks,
    )
    with tracing_context(enabled=False):
        result = await asyncio.wait_for(agent.ainvoke({"messages": [("user", "Run the probes")] }), timeout=30)
    assert activity["maximum"] == 2
    assert sorted(activity["seen"]) == ["child0", "child1", "child2"]
    assert all("ok-" + name in result["messages"][-1].content for name in activity["seen"])
    assert hooks.model_calls == 8  # two per coordinator/child
    assert hooks.tool_calls == 6   # three task delegations plus three tools
    for name in ("coordinator", "child0", "child1", "child2"):
        assert any(kind == "model_calls" and agent_role == name for kind, agent_role, _ in hooks.controls)
    assert sum(kind == "delegation" for kind, _, _ in hooks.controls) == 3
    assert all("Probe " not in repr(entry) for entry in hooks.controls + hooks.reservations)


@pytest.mark.asyncio
async def test_nested_delegation_executes_real_two_level_graph():
    class NestedModel(LocalModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if self.scenario == "child0" and not any(isinstance(message, ToolMessage) for message in messages):
                response = AIMessage(content="", tool_calls=[{
                    "name": "task", "args": {"description": "leaf work", "subagent_type": "child1"},
                    "id": "grandchild", "type": "tool_call",
                }])
            else:
                response = AIMessage(content="nested work complete")
            return ChatResult(generations=[ChatGeneration(message=response)])

    hooks = Hooks()
    scheduler = HierarchicalDelegation(max_active=2, max_depth=2)
    graph = build_controlled_agent(
        coordinator=role("coordinator", "parent", delegates=1),
        subagents=[AgentRole(name=f"child{i}", description="nested test", model=NestedModel(scenario=f"child{i}"))
                   for i in range(2)],
        hooks=hooks, scheduler=scheduler, nested_delegation=True,
    )
    try:
        with tracing_context(enabled=False):
            result = await asyncio.wait_for(graph.ainvoke({"messages": [("user", "delegate twice")]}), timeout=15)
        assert "nested work complete" in result["messages"][-1].content
        assert sum(kind == "delegation" for kind, _, _ in hooks.controls) == 2
        assert any(role_name == "child1" and kind == "model_calls" for kind, role_name, _ in hooks.controls)
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_nested_leaf_cannot_delegate_third_level():
    class AttemptThird(LocalModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
                "name": "task", "args": {"description": "another level", "subagent_type": "child1"},
                "id": f"delegate-{self.scenario}", "type": "tool_call",
            }]))])

    hooks = Hooks()
    scheduler = HierarchicalDelegation(max_active=2, max_depth=2)
    graph = build_controlled_agent(
        coordinator=role("coordinator", "parent", delegates=1),
        subagents=[AgentRole(name=f"child{i}", description="nested test", model=AttemptThird(scenario=f"child{i}"))
                   for i in range(2)],
        hooks=hooks, scheduler=scheduler, nested_delegation=True,
    )
    try:
        with tracing_context(enabled=False), pytest.raises(AgentToolDenied):
            await asyncio.wait_for(graph.ainvoke({"messages": [("user", "try three levels")]}), timeout=15)
    finally:
        await scheduler.aclose()
    assert hooks.tool_calls == 2


@pytest.mark.asyncio
async def test_host_budget_stops_before_model_dispatch():
    hooks = Hooks(max_models=0)
    agent = build_controlled_agent(coordinator=role("coordinator", "parent"), subagents=[], hooks=hooks)
    with tracing_context(enabled=False), pytest.raises(AgentBudgetExceeded):
        await agent.ainvoke({"messages": [("user", "no call permitted")]})
    assert hooks.model_calls == 0
    assert hooks.reservations == [("model_calls", "coordinator", None)]


@pytest.mark.asyncio
async def test_control_is_checked_inside_actual_subagent_before_its_model_call():
    class ChildPaused(Hooks):
        async def check_control(self, kind, role, tool_name):
            await super().check_control(kind, role, tool_name)
            if kind == "model_calls" and role == "child0":
                raise RuntimeError("PAUSED_CHILD")

    hooks = ChildPaused()
    agent = build_controlled_agent(coordinator=role("coordinator", "parent", delegates=1),
                                   subagents=[role("child0", "child0")], hooks=hooks)
    with tracing_context(enabled=False), pytest.raises(AgentExternalCallFailed):
        await agent.ainvoke({"messages": [("user", "delegate once")]})
    assert ("model_calls", "child0", None) in hooks.controls
    assert ("model_calls", "child0", None) not in hooks.reservations


@pytest.mark.asyncio
async def test_sdk_implicit_summarization_is_replaced_for_parent_and_child(monkeypatch):
    from deepagents.middleware.summarization import _DeepAgentsSummarizationMiddleware

    async def must_not_run(*args, **kwargs):
        raise AssertionError("Uncontrolled summarization hook was invoked")

    monkeypatch.setattr(_DeepAgentsSummarizationMiddleware, "abefore_model", must_not_run)
    hooks = Hooks()

    @tool
    async def probe_work(label: str) -> str:
        """A harmless deterministic probe."""
        return label

    agent = build_controlled_agent(coordinator=role("coordinator", "parent", delegates=1),
                                   subagents=[role("child0", "child0", tools=[probe_work])], hooks=hooks)
    with tracing_context(enabled=False):
        result = await agent.ainvoke({"messages": [("user", "delegate one task")]})
    assert "child0" in result["messages"][-1].content


@pytest.mark.asyncio
async def test_unapproved_builtin_file_tool_is_rejected_without_dispatch():
    hooks = Hooks()
    agent = build_controlled_agent(coordinator=role("coordinator", "unknown"), subagents=[], hooks=hooks)
    with tracing_context(enabled=False), pytest.raises(AgentToolDenied):
        await agent.ainvoke({"messages": [("user", "attempt unapproved tool")]})
    assert hooks.tool_calls == 0


@pytest.mark.asyncio
async def test_delegation_waiting_for_slot_rechecks_pause_and_child_cannot_delegate():
    hooks = Hooks()
    semaphore = asyncio.Semaphore(1)
    middleware = SharedBoundaryMiddleware(hooks=hooks, role="coordinator", allowed_tools=frozenset(),
                                          allowed_agents=frozenset({"child"}), semaphore=semaphore, parent=True)
    dispatched = []

    async def handler(request):
        dispatched.append(request.tool_call["id"])
        return "ok"

    request = SimpleNamespace(tool_call={"name": "task", "id": "waiting",
                                         "args": {"subagent_type": "child"}})
    await semaphore.acquire()
    pending = asyncio.create_task(middleware.awrap_tool_call(request, handler))
    await asyncio.sleep(0)
    hooks.paused = True
    semaphore.release()
    with pytest.raises(AgentExternalCallFailed):
        await pending
    assert dispatched == [] and hooks.tool_calls == 0

    hooks.paused = False
    child = SharedBoundaryMiddleware(hooks=hooks, role="child", allowed_tools=frozenset(),
                                     allowed_agents=frozenset(), semaphore=semaphore, parent=False)
    with pytest.raises(AgentToolDenied):
        await child.awrap_tool_call(request, handler)
    assert dispatched == []


@pytest.mark.asyncio
async def test_shared_tool_budget_denial_stops_handler():
    hooks = Hooks(max_tools=0)
    called = []

    @tool
    async def probe_work(label: str) -> str:
        """A harmless deterministic probe."""
        called.append(label)
        return label

    agent = build_controlled_agent(coordinator=role("coordinator", "child", tools=[probe_work]),
                                   subagents=[], hooks=hooks)
    with tracing_context(enabled=False), pytest.raises(AgentBudgetExceeded):
        await agent.ainvoke({"messages": [("user", "no tool permitted")]})
    assert called == [] and hooks.tool_calls == 0


@pytest.mark.asyncio
async def test_model_credential_canary_not_in_checkpoint_values_or_hook_metadata(tmp_path):
    hooks = Hooks()
    secret = "unit-test-secret-only-4f1c"
    model = LocalModel(scenario="unknown", api_key=secret)
    coordinator = AgentRole(name="coordinator", description="test role", model=model)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "agent.db")) as saver:
        agent = build_controlled_agent(coordinator=coordinator, subagents=[], hooks=hooks,
                                       checkpointer=saver)
        config = {"configurable": {"thread_id": "test-credential-canary"}}
        with tracing_context(enabled=False), pytest.raises(AgentToolDenied):
            await agent.ainvoke({"messages": [("user", "test input without secret")]}, config)
        state = await agent.aget_state(config)
        assert secret not in repr(state.values)
    for part in tmp_path.glob("agent.db*"):
        assert secret.encode() not in part.read_bytes()
    assert secret not in repr(hooks.controls) + repr(hooks.reservations)
