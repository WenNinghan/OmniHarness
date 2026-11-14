"""Depth-2 scheduling: isolated deterministic agents, never a live provider."""

import asyncio
from dataclasses import dataclass, field

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt
from langsmith import tracing_context

from omniharness.m3_agents import NoImplicitSummarization, SharedBoundaryMiddleware
from omniharness.m3_delegation import (
    DelegationDepthExceeded,
    DelegationDrainTimeout,
    DelegationError,
    HierarchicalDelegation,
)


class ObservedScheduler(HierarchicalDelegation):
    def __init__(self):
        super().__init__()
        self.maximum = 0

    async def _acquire(self):
        await super()._acquire()
        self.maximum = max(self.maximum, self.active_count)


@dataclass
class Hooks:
    paused_leaf: bool = False
    calls: list = field(default_factory=list)

    async def check_control(self, kind, role, tool_name):
        if self.paused_leaf and role == "leaf0_0" and kind == "model_calls":
            assert interrupt("pause-leaf") == "continue"

    async def reserve(self, kind, role, tool_name):
        self.calls.append((kind, role, tool_name))
        return True


class TreeModel(BaseChatModel):
    kind: str
    leaves: int = 1
    gate: object = None
    entered: object = None
    fail: bool = False
    cleanup: object = None

    @property
    def _llm_type(self):
        return "offline-delegation-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.kind.startswith("leaf") or any(isinstance(m, ToolMessage) for m in messages):
            response = AIMessage(content="done")
        else:
            names = ["child0", "child1"] if self.kind == "root" else [
                f"leaf{self.kind[-1]}_{i}" for i in range(self.leaves)
            ]
            response = AIMessage(content="", tool_calls=[{
                "name": "task", "args": {"description": "offline", "subagent_type": name},
                "id": "id-" + name, "type": "tool_call",
            } for name in names])
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.fail and self.kind == "leaf0_0":
            raise RuntimeError("offline test failure")
        if self.gate is not None and self.kind.startswith("leaf"):
            self.entered.set()
            if self.cleanup is not None:
                self.cleanup.entered_count += 1
                if self.cleanup.entered_count == 2:
                    self.cleanup.entered.set()
            try:
                await self.gate.wait()
            finally:
                if self.cleanup is not None:
                    self.cleanup.started += 1
                    if self.cleanup.started == 2:
                        self.cleanup.waiting.set()
                    try:
                        await self.cleanup.release.wait()
                    finally:
                        self.cleanup.finished += 1
                        if self.cleanup.finished == 2:
                            self.cleanup.done.set()
        return self._generate(messages, stop, run_manager, **kwargs)


class TestMiddleware(SharedBoundaryMiddleware):
    __test__ = False

    def __init__(self, *, scheduler, **kwargs):
        super().__init__(**kwargs)
        self.scheduler = scheduler

    async def awrap_model_call(self, request, handler):
        await self.scheduler.ensure_active()
        async def tracked(req):
            return await self.scheduler.run_tracked(lambda: handler(req))
        return await super().awrap_model_call(request, tracked)

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        if call.get("name") != "task":
            await self.scheduler.ensure_active()
            async def tracked(req):
                return await self.scheduler.run_tracked(lambda: handler(req))
            return await super().awrap_tool_call(request, tracked)
        await self.hooks.check_control("delegation", self.role, "task")
        target = (call.get("args") or {}).get("subagent_type")
        if not self.parent or target not in self.allowed_agents:
            raise RuntimeError("Delegation denied")
        async def delegate():
            async with self.scheduler.delegate():
                invocation = await self._reserve("tool_calls", "task", request)
                return await self._dispatch(request, handler, invocation, "tool_calls", "task")
        return await self.scheduler.run_tracked(delegate)


def build_tree(scheduler, hooks, *, leaves=1, gate=None, entered=None, fail=False, cleanup=None):
    def middleware(name, children, parent):
        return [NoImplicitSummarization(), TestMiddleware(
            hooks=hooks, role=name, allowed_tools=frozenset(), allowed_agents=frozenset(children),
            semaphore=asyncio.Semaphore(2), parent=parent, scheduler=scheduler,
        )]

    children = []
    for i in (0, 1):
        name = f"child{i}"
        names = [f"leaf{i}_{j}" for j in range(leaves)]
        leaf_specs = [{"name": leaf, "description": "offline leaf", "tools": [],
                       "model": TreeModel(kind=leaf, gate=gate, entered=entered, fail=fail, cleanup=cleanup),
                       "middleware": middleware(leaf, [], False)} for leaf in names]
        child = create_deep_agent(model=TreeModel(kind=name, leaves=leaves), tools=[],
                                  middleware=middleware(name, names, True), subagents=leaf_specs, name=name)
        children.append({"name": name, "description": "offline child", "runnable": child})
    return create_deep_agent(model=TreeModel(kind="root"), tools=[],
                             middleware=middleware("root", ["child0", "child1"], True),
                             subagents=children, checkpointer=InMemorySaver(), name="root")


@pytest.mark.asyncio
async def test_two_child_branches_parallel_grandchildren_and_interrupt_resume():
    scheduler, hooks = ObservedScheduler(), Hooks(paused_leaf=True)
    graph = build_tree(scheduler, hooks, leaves=2)
    config = {"configurable": {"thread_id": "offline-depth-two"}}
    with tracing_context(enabled=False):
        paused = await asyncio.wait_for(graph.ainvoke({"messages": [("user", "offline")]}, config,
                                                     durability="sync"), timeout=10)
        assert paused.get("__interrupt__")
        assert scheduler.active_count == 0
        complete = await asyncio.wait_for(graph.ainvoke(Command(resume="continue"), config,
                                                       durability="sync"), timeout=10)
    assert not complete.get("__interrupt__")
    assert scheduler.active_count == 0 and scheduler.maximum == 2
    await scheduler.aclose()


@pytest.mark.asyncio
async def test_siblings_suspend_parent_until_every_descendant_returns():
    scheduler, both_started, release = ObservedScheduler(), asyncio.Event(), asyncio.Event()
    started = 0
    async with scheduler.delegate() as depth:
        assert depth == 1 and scheduler.active_count == 1

        async def leaf():
            nonlocal started
            async with scheduler.delegate() as child_depth:
                assert child_depth == 2
                started += 1
                if started == 2:
                    both_started.set()
                await release.wait()

        children = [asyncio.create_task(leaf()) for _ in range(2)]
        await asyncio.wait_for(both_started.wait(), 2)
        parent_resume = asyncio.create_task(scheduler.ensure_active())
        await asyncio.sleep(0)
        assert not parent_resume.done() and scheduler.active_count == 2
        release.set()
        await asyncio.gather(*children)
        await asyncio.wait_for(parent_resume, 2)
        assert scheduler.active_count == 1
    assert scheduler.active_count == 0 and scheduler.maximum == 2
    await scheduler.aclose()


@pytest.mark.asyncio
async def test_depth_three_and_cross_run_context_fail_closed():
    first, other = HierarchicalDelegation(), HierarchicalDelegation()
    async with first.delegate() as depth:
        assert depth == 1
        with pytest.raises(DelegationError, match="another"):
            await other.ensure_active()
        async with first.delegate() as nested:
            assert nested == 2
            with pytest.raises(DelegationDepthExceeded):
                async with first.delegate():
                    pass
    assert first.active_count == 0
    await first.aclose()
    await other.aclose()


@pytest.mark.asyncio
async def test_double_cancel_waiting_for_slot_drains_nested_graph():
    scheduler, hooks = ObservedScheduler(), Hooks()
    gate, entered = asyncio.Event(), asyncio.Event()
    graph = build_tree(scheduler, hooks, leaves=2, gate=gate, entered=entered)
    config = {"configurable": {"thread_id": "offline-cancel"}}
    with tracing_context(enabled=False):
        task = asyncio.create_task(graph.ainvoke({"messages": [("user", "offline")]}, config,
                                                 durability="sync"))
        await asyncio.wait_for(entered.wait(), 3)

        async def wait_for_waiter():
            while not scheduler._semaphore._waiters:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_waiter(), 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # LangGraph may return before internal tasks stop, so the host must drain.
        await scheduler.aclose(timeout=3)
    assert scheduler.closed and scheduler.active_count == 0 and scheduler.maximum == 2
    assert not scheduler._tasks


@pytest.mark.asyncio
async def test_model_exception_drains_remaining_delegates():
    scheduler, hooks = ObservedScheduler(), Hooks()
    graph = build_tree(scheduler, hooks, leaves=2, fail=True)
    try:
        with tracing_context(enabled=False), pytest.raises(Exception):
            await asyncio.wait_for(graph.ainvoke({"messages": [("user", "offline")]},
                                                {"configurable": {"thread_id": "offline-failure"}},
                                                durability="sync"), 5)
    finally:
        await scheduler.aclose(timeout=3)
    assert scheduler.active_count == 0 and scheduler.maximum <= 2


@pytest.mark.asyncio
async def test_drain_timeout_is_fail_closed_and_refuses_new_work():
    scheduler = HierarchicalDelegation()
    entered, release = asyncio.Event(), asyncio.Event()

    async def stubborn():
        async with scheduler.delegate():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

    task = asyncio.create_task(stubborn())
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(DelegationDrainTimeout):
        await scheduler.aclose(timeout=0.05)
    with pytest.raises(DelegationError, match="closed"):
        async with scheduler.delegate():
            pass
    release.set()
    await task
    assert scheduler.active_count == 0


@pytest.mark.asyncio
async def test_drain_survives_caller_cancel_until_child_closes():
    scheduler = HierarchicalDelegation()
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def child():
        async with scheduler.delegate():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

    task = asyncio.create_task(child())
    await asyncio.wait_for(entered.wait(), 2)
    closer = asyncio.create_task(scheduler.aclose(timeout=2))
    await asyncio.wait_for(cancelled.wait(), 2)
    closer.cancel()
    await asyncio.sleep(0)
    closer.cancel()
    await asyncio.sleep(0)
    assert not closer.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closer
    await task
    assert scheduler.active_count == 0 and not scheduler._tasks


@pytest.mark.asyncio
async def test_repeated_graph_cancel_waits_for_two_provider_cleanup_handlers():
    scheduler, hooks = ObservedScheduler(), Hooks()
    gate, entered = asyncio.Event(), asyncio.Event()

    class Cleanup:
        def __init__(self):
            self.entered_count = self.started = self.finished = 0
            self.entered = asyncio.Event()
            self.waiting, self.release, self.done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    cleanup = Cleanup()
    graph = build_tree(scheduler, hooks, gate=gate, entered=entered, cleanup=cleanup)
    with tracing_context(enabled=False):
        execution = asyncio.create_task(graph.ainvoke({"messages": [("user", "offline")]},
                                      {"configurable": {"thread_id": "provider-cleanup"}}, durability="sync"))
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(cleanup.entered.wait(), 3)
        execution.cancel()
        await asyncio.wait_for(cleanup.waiting.wait(), 3)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        drain = asyncio.create_task(scheduler.aclose(timeout=3))
        await asyncio.sleep(0.05)
        assert not drain.done() and not cleanup.done.is_set()
        cleanup.release.set()
        await asyncio.wait_for(drain, 3)
        await asyncio.wait_for(cleanup.done.wait(), 3)
    assert cleanup.finished == 2 and scheduler.active_count == 0
    assert not scheduler._tasks and not scheduler._operations


@pytest.mark.asyncio
async def test_abandoned_operation_exception_is_retrieved():
    scheduler = HierarchicalDelegation()
    entered, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    reports = []
    loop.set_exception_handler(lambda _loop, context: reports.append(context))

    async def provider():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise RuntimeError("sensitive-provider-canary")

    try:
        caller = asyncio.create_task(scheduler.run_tracked(provider))
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        await scheduler.aclose(timeout=2)
        await asyncio.sleep(0)
        assert not reports
    finally:
        loop.set_exception_handler(previous_handler)
