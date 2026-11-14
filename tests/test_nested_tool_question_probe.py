"""Prove nested checkpoints resume without replaying an earlier side effect."""
import asyncio
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class State(TypedDict, total=False):
    effect: int
    answer: str


@pytest.mark.asyncio
async def test_nested_interrupt_does_not_repeat_prior_effect():
    effects = []
    def write(_state):
        effects.append("written")
        return {"effect": len(effects)}
    def ask(_state):
        answer = interrupt({"type": "user_clarification", "clarification_id": "q"})
        return {"answer": answer}
    child = StateGraph(State)
    child.add_node("write", write)
    child.add_node("ask", ask)
    child.add_edge(START, "write")
    child.add_edge("write", "ask")
    child.add_edge("ask", END)
    child = child.compile()
    async def call_child(state, config):
        return await child.ainvoke(state, config)
    parent = StateGraph(State)
    parent.add_node("child", call_child)
    parent.add_edge(START, "child")
    parent.add_edge("child", END)
    parent = parent.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "test"}}
    first = await parent.ainvoke({}, config, durability="sync")
    pending = first["__interrupt__"][0]
    assert effects == ["written"]
    result = await parent.ainvoke(Command(resume={pending.id: "confirmed"}), config, durability="sync")
    assert effects == ["written"]
    assert result["answer"] == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_operation', [False, True])
async def test_v7_workspace_patch_then_ask_does_not_repeat_patch(environment, tmp_path, monkeypatch, blocked_operation):
    import json

    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from sqlalchemy import select
    from test_m4_collaboration import prepare_v4
    from test_v6_handoff_smoke import V6Stage

    from omniharness import interrupt_timing
    from omniharness.artifacts import ArtifactStore
    from omniharness.m3_delegation import HierarchicalDelegation
    from omniharness.m3_stages import build_stage_graph
    from omniharness.m3_worker import _user_resume_map
    from omniharness.m4_contract import (
        EVIDENCE_STAGE_VERSIONS,
        INTERACTIVE_WORKFLOW_VERSION,
        M4_PLUGIN_VERSIONS,
    )
    from omniharness.m4_runtime import M4AgentHost, build_execution_agent
    from omniharness.models import Clarification, Run, RunBudget, RunBudgetReservation
    from omniharness.stage_contract import STAGE_KEYS, STAGE_ROLE_BINDINGS
    from omniharness.worker import WorkerLock

    started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    marker_seen = asyncio.Event()
    schedulers = []
    original_marker = interrupt_timing.user_interrupt

    def observe_marker(*args, **kwargs):
        if blocked_operation:
            assert completed.is_set(), 'marker emitted while a tool operation was still in flight'
        marker_seen.set()
        return original_marker(*args, **kwargs)

    monkeypatch.setattr(interrupt_timing, 'user_interrupt', observe_marker)

    async def blocking_handler():
        started.set()
        await release.wait()
        completed.set()

    async def release_after_check():
        await started.wait()
        await asyncio.sleep(0.05)
        assert not marker_seen.is_set()
        release.set()

    class QuestionToolModel(BaseChatModel):
        @property
        def _llm_type(self): return "v7-question-test"
        def bind_tools(self, tools, **kwargs):
            assert "ask_user" in {tool.name for tool in tools}
            return self
        def _generate(self, messages, **kwargs):
            tools = [m for m in messages if isinstance(m, ToolMessage)]
            if len(tools) == 0:
                name, args = "workspace_read", {"path": "main.py"}
            elif len(tools) == 1:
                name, args = "workspace_patch", {"path": "main.py", "content": "print(42)\n",
                    "expected_sha256": json.loads(tools[-1].content)["sha256"]}
            elif len(tools) == 2:
                name, args = "ask_user", {"question": "保留哪个配置？", "reason": "缺少选择"}
            else:
                assert json.loads(tools[-1].content)["answer"] == "选项甲"
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content='{"status":"completed","content":"已修改并收到用户选择","questions":[]}'))])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
                {"id": f"call-{len(tools)}", "name": name, "args": args}]))])
        async def _agenerate(self, messages, **kwargs):
            tools = [m for m in messages if isinstance(m, ToolMessage)]
            if blocked_operation and len(tools) == 2 and not started.is_set():
                asyncio.create_task(schedulers[-1].run_tracked(blocking_handler))
                await started.wait()
            return self._generate(messages, **kwargs)

    class InteractiveStage(V6Stage):
        async def ainvoke(self, messages):
            if getattr(messages[-1], "id", None) == "omni-interaction:preflight":
                return AIMessage(content='{"status":"continue","questions":[]}')
            if getattr(messages[-1], "id", None) == "omni-interaction:final":
                return AIMessage(content="已综合最终结果。")
            return await super().ainvoke(messages)

    worker, run_id, workspace, broker, sandbox, project = prepare_v4(environment, tmp_path)
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        agent = {**run.config_snapshot["agent"], "workflow_version": INTERACTIVE_WORKFLOW_VERSION}
        for role in STAGE_KEYS:
            agent["roles"][role] = {**agent["roles"][role], "role_version": EVIDENCE_STAGE_VERSIONS[role]}
        run.config_snapshot = {**run.config_snapshot, "agent": agent}
        run.workflow_version = INTERACTIVE_WORKFLOW_VERSION
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
    def build():
        host = M4AgentHost(worker, run_id, INTERACTIVE_WORKFLOW_VERSION, role_versions=EVIDENCE_STAGE_VERSIONS,
                           plugin_versions=M4_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
        scheduler = HierarchicalDelegation()
        schedulers.append(scheduler)
        execution = build_execution_agent(model=QuestionToolModel(), host=host, workspace=workspace, broker=broker,
            sandbox=sandbox, artifacts=ArtifactStore(worker.settings.data_dir / "artifacts"), scheduler=scheduler)
        graph = build_stage_graph(models={role: InteractiveStage(role) for role in STAGE_KEYS}, hooks=host,
            checkpointer=saver, workflow_version=INTERACTIVE_WORKFLOW_VERSION, execution_agent=execution,
            execution_strategy="compact")
        return graph, scheduler

    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = build()
        releaser = asyncio.create_task(release_after_check()) if blocked_operation else None
        try:
            first = await graph.ainvoke({"messages": [HumanMessage(content="修改项目")]}, config, durability="sync")
            if releaser:
                await releaser
        finally:
            release.set()
            if releaser and not releaser.done():
                releaser.cancel()
            await scheduler.aclose()
        assert first.get("__interrupt__") and broker.calls.count("patch") == 1
        with worker.transaction() as db:
            question = db.scalar(select(Clarification))
            assert question and question.question == "保留哪个配置？"
            assert db.get(RunBudget, run_id).tool_calls == 3
            question.status, question.answer = "answered", "选项甲"
        graph, scheduler = build()
        state = await graph.aget_state(config)
        with worker.transaction() as db:
            signals = _user_resume_map(db, run_id, state.interrupts)
        try:
            result = await graph.ainvoke(Command(resume=signals), config, durability="sync")
        finally:
            await scheduler.aclose()
    assert not result.get("__interrupt__")
    assert broker.calls.count("patch") == 1 and (project / "main.py").read_text() == "print(42)\n"
    assert result["messages"][-1].content == "已综合最终结果。"
    with worker.sessions() as db:
        assert db.get(RunBudget, run_id).tool_calls == 3
        assert len(list(db.scalars(select(RunBudgetReservation).where(
            RunBudgetReservation.run_id == run_id, RunBudgetReservation.kind == "tool_calls")))) == 3


def test_approval_reconciliation_waits_for_pending_question(environment, tmp_path):
    from test_m4_runtime import prepare

    from omniharness.clarification_service import create_clarification
    from omniharness.m4_runtime import reconcile_approval_waits
    from omniharness.models import Approval, Run, ToolOperation
    worker, run_id, _workspace, _broker, _sandbox, _project = prepare(environment, tmp_path)
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        run.workflow_version = "stage-agent-v7"
        create_clarification(db, run, "task:q", "t1", "请回答", "需要确认", [], stage="execute")
        operation = ToolOperation(id="operation", run_id=run_id, task_id="task", tool_call_id="tool",
            tool_name="run_command", tool_version="v1", parameters={}, binding_fingerprint="f" * 64,
            status="requested")
        db.add(operation)
        db.flush()
        db.add(Approval(id="approval", operation_id=operation.id, fingerprint="f" * 64, status="approved"))
        run.status = "awaiting_approval"
    reconcile_approval_waits(worker)
    with worker.sessions() as db:
        assert db.get(Run, run_id).status == "awaiting_user"
