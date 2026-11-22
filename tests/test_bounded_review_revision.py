"""Offline regressions for bounded Review -> Revise loop and RunBudget.revisions."""
from __future__ import annotations

import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from omniharness.m3_host import FencedAgentHost
from omniharness.m3_stages import _extract_actionable_review_failures, build_stage_graph
from omniharness.models import Conversation, ModelConfig, Provider, Run, RunBudget, RunEvent, ToolOperation
from omniharness.schemas import SendMessageRequest
from omniharness.services import enqueue, freeze_stage_run_config, run_output
from omniharness.stage_contract import STAGE_KEYS, STAGE_ROLE_BINDINGS, STAGE_VERSIONS, STAGE_WORKFLOW_VERSION
from omniharness.worker import Worker, WorkerLock


def test_extract_actionable_review_failures_variants():
    assert _extract_actionable_review_failures(None, None) == []
    assert _extract_actionable_review_failures({"status": "UNAVAILABLE", "reason": "INVALID_OUTPUT"}, "INVALID_OUTPUT") == []
    assert _extract_actionable_review_failures(
        {"summary": "degraded", "findings": [{"status": "FAIL", "location": "a.py"}]},
        "INVALID_OUTPUT",
    ) == []
    assert _extract_actionable_review_failures(
        {"summary": "ok", "findings": [{"status": "PASS", "location": "a.py", "severity": "INFO", "description": "ok", "recommendation": ""}]},
        "COMPLETED",
    ) == []

    v1_fail = _extract_actionable_review_failures(
        {
            "summary": "has bug",
            "findings": [
                {"location": "a.py", "severity": "HIGH", "description": "bug", "status": "FAIL", "recommendation": "fix bug"},
                {"location": "b.py", "severity": "INFO", "description": "ok", "status": "PASS", "recommendation": ""},
            ],
        },
        None,
    )
    assert len(v1_fail) == 1
    assert v1_fail[0]["location"] == "a.py"
    assert v1_fail[0]["recommendation"] == "fix bug"

    v9_fail = _extract_actionable_review_failures(
        {
            "summary": "v9 fail",
            "findings": [
                {"location": "c.py", "severity": "MEDIUM", "description": "missing test", "model_status": "FAIL", "recommendation": "add test"},
            ],
        },
        "COMPLETED",
    )
    assert len(v9_fail) == 1
    assert v9_fail[0]["location"] == "c.py"


class _MemoryHooks:
    def __init__(self, *, max_revisions: int = 2, has_uncertain_tools: bool = False):
        self.max_revisions = max_revisions
        self.has_uncertain_tools = has_uncertain_tools
        self.invocations: list[tuple[str, str | None]] = []

    async def check_control(self, kind, role, tool_name):
        del kind, role, tool_name

    async def stage_requirements(self, stage, messages):
        del stage, messages
        return []

    async def replay_invocation(self, kind, role, tool_name, identity):
        del kind, role, tool_name, identity
        return None

    async def reserve_invocation(self, kind, role, tool_name, identity, **kwargs):
        del kind, tool_name, kwargs
        self.invocations.append((role, identity.invocation_id))
        return True

    async def finish_invocation(self, kind, role, tool_name, identity, **kwargs):
        del kind, role, tool_name, identity, kwargs

    async def mark_invocation_uncertain(self, *args, **kwargs):
        del args, kwargs


class _ScriptedStageModel(BaseChatModel):
    stage: str

    def __init__(self, stage: str, state_box: dict):
        super().__init__(stage=stage)
        object.__setattr__(self, "_box", state_box)

    @property
    def _llm_type(self):
        return "scripted-revision-model"

    def _generate(self, messages, **kwargs):
        raise NotImplementedError

    async def ainvoke(self, messages):
        self._box["calls"].append((self.stage, len(messages)))
        if self.stage == "think":
            return AIMessage(content="分析完成。")
        if self.stage == "plan":
            return AIMessage(content='{"steps":[{"id":"s1","task":"实现功能","acceptance":"通过测试","depends_on":[]}]}')
        if self.stage == "dispatch":
            return AIMessage(content='{"tasks":[{"id":"t1","step_id":"s1","instruction":"实现并验证","depends_on":[]}]}')
        if self.stage == "execute":
            exec_n = self._box["exec_count"]
            self._box["exec_count"] += 1
            return AIMessage(content=f"执行输出第 {exec_n + 1} 版" + ("（已按审查意见修复）" if exec_n > 0 else ""))
        rev_n = self._box["review_count"]
        self._box["review_count"] += 1
        if rev_n < self._box["fail_times"]:
            return AIMessage(content=json.dumps({
                "summary": f"第 {rev_n + 1} 次审查发现缺陷",
                "findings": [{
                    "location": "main.py:10",
                    "severity": "HIGH",
                    "description": "缺少边界检查",
                    "status": "FAIL",
                    "recommendation": "补充空输入校验并重测",
                }],
                "unverified": [],
            }, ensure_ascii=False))
        return AIMessage(content=json.dumps({
            "summary": "复核通过",
            "findings": [{
                "location": "main.py:10",
                "severity": "INFO",
                "description": "边界检查已补齐",
                "status": "PASS",
                "recommendation": "",
            }],
            "unverified": [],
        }, ensure_ascii=False))


@pytest.mark.asyncio
async def test_bounded_revision_fixes_fail_and_completes_while_off_stops():
    box = {"calls": [], "exec_count": 0, "review_count": 0, "fail_times": 1}
    hooks = _MemoryHooks(max_revisions=2)
    graph = build_stage_graph(
        models={stage: _ScriptedStageModel(stage, box) for stage in STAGE_KEYS},
        hooks=hooks,
        checkpointer=InMemorySaver(),
        workflow_version=STAGE_WORKFLOW_VERSION,
        review_revision_policy="bounded",
    )
    final = await graph.ainvoke(
        {"messages": [("user", "写一个函数")]},
        {"configurable": {"thread_id": "thread-rev-1"}},
    )
    assert final["revision_round"] == 1
    assert "第 1 轮修订" in final["execution"]
    assert "复核通过" in final["review"]
    assert box["exec_count"] == 2
    assert box["review_count"] == 2
    review_ids = [inv_id for role, inv_id in hooks.invocations if role == "review"]
    assert len(review_ids) == 2 and len(set(review_ids)) == 2

    # Default policy="off" does not revise even on FAIL.
    off_box = {"calls": [], "exec_count": 0, "review_count": 0, "fail_times": 2}
    off_hooks = _MemoryHooks(max_revisions=2)
    off_graph = build_stage_graph(
        models={stage: _ScriptedStageModel(stage, off_box) for stage in STAGE_KEYS},
        hooks=off_hooks,
        checkpointer=InMemorySaver(),
        workflow_version=STAGE_WORKFLOW_VERSION,
    )
    off_final = await off_graph.ainvoke(
        {"messages": [("user", "写一个函数")]},
        {"configurable": {"thread_id": "thread-rev-off"}},
    )
    assert off_final.get("revision_round") is None
    assert off_box["exec_count"] == 1
    assert off_box["review_count"] == 1


def _seed_durable_run(environment, *, max_revisions: int = 1, review_revision_policy: str = "bounded"):
    settings, engine, factory, _ = environment
    custom_settings = settings.model_copy(update={
        "budget_max_revisions": max_revisions,
        "review_revision_policy": review_revision_policy,
    })
    with factory.begin() as db:
        provider = Provider(adapter_type="openai-compatible", base_url="http://127.0.0.1:18080/v1")
        db.add(provider)
        db.flush()
        model_cfg = ModelConfig(provider_id=provider.id, name="M", model_name="demo", version=1)
        db.add(model_cfg)
        db.flush()
        conv = Conversation(title="RevTest")
        db.add(conv)
        db.flush()
        run_id = enqueue(
            db,
            conv.id,
            SendMessageRequest(content="修复边界", model_config_id=model_cfg.id, client_request_id=f"req-{max_revisions}-{id(db)}"),
        )["run_id"]
        run = db.get(Run, run_id)
        run.config_snapshot = {**run.config_snapshot, "workflow_type": "agent"}
        freeze_stage_run_config(db, run, custom_settings)
        run.status = "running"
        projected = run_output(run)
    worker = Worker(custom_settings, engine, factory)
    return worker, run_id, factory, projected


@pytest.mark.asyncio
async def test_durable_budget_exhaustion_unsafe_guard_and_replay_idempotency(environment):
    worker, run_id, factory, projected = _seed_durable_run(environment, max_revisions=1)
    assert projected["execution_config"]["review_revision_policy"] == "bounded"

    box = {"calls": [], "exec_count": 0, "review_count": 0, "fail_times": 5}
    with WorkerLock(worker.engine, worker.settings) as lock:
        worker.lock = lock
        host = FencedAgentHost(
            worker,
            run_id,
            STAGE_WORKFLOW_VERSION,
            role_versions=STAGE_VERSIONS,
            plugin_versions={},
            role_bindings=STAGE_ROLE_BINDINGS,
        )
        graph = build_stage_graph(
            models={stage: _ScriptedStageModel(stage, box) for stage in STAGE_KEYS},
            hooks=host,
            checkpointer=InMemorySaver(),
            workflow_version=STAGE_WORKFLOW_VERSION,
            review_revision_policy="bounded",
        )
        final = await graph.ainvoke(
            {"messages": [("user", "修复边界")]},
            {"configurable": {"thread_id": run_id}},
        )
        assert final["revision_round"] == 1
        assert box["exec_count"] == 2
        assert box["review_count"] == 2

    with factory() as db:
        budget = db.get(RunBudget, run_id)
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence_number)))
        triggered = [e for e in events if e.event_type == "agent.revision_triggered"]
        skipped = [e for e in events if e.event_type == "agent.revision_skipped"]
        assert budget.revisions == 1
        assert len(triggered) == 1 and triggered[0].payload["revision_round"] == 1
        assert len(skipped) == 1 and skipped[0].payload["reason"] == "REVISION_BUDGET_EXHAUSTED"

    # Unsafe uncertain ToolOperation blocks revision completely and preserves revisions=0.
    worker2, run_id2, factory2, _ = _seed_durable_run(environment, max_revisions=2)
    with factory2.begin() as db:
        db.add(ToolOperation(
            id="uncertain-op-rev",
            run_id=run_id2,
            task_id="t1",
            tool_call_id="call-1",
            tool_name="run_command",
            tool_version="builtin-workspace-v1",
            parameters={"command": "pytest"},
            binding_fingerprint="e" * 64,
            status="uncertain",
            slot_claimed=True,
        ))
    box2 = {"calls": [], "exec_count": 0, "review_count": 0, "fail_times": 5}
    with WorkerLock(worker2.engine, worker2.settings) as lock2:
        worker2.lock = lock2
        host2 = FencedAgentHost(
            worker2,
            run_id2,
            STAGE_WORKFLOW_VERSION,
            role_versions=STAGE_VERSIONS,
            plugin_versions={},
            role_bindings=STAGE_ROLE_BINDINGS,
        )
        graph2 = build_stage_graph(
            models={stage: _ScriptedStageModel(stage, box2) for stage in STAGE_KEYS},
            hooks=host2,
            checkpointer=InMemorySaver(),
            workflow_version=STAGE_WORKFLOW_VERSION,
            review_revision_policy="bounded",
        )
        final2 = await graph2.ainvoke(
            {"messages": [("user", "修复边界")]},
            {"configurable": {"thread_id": run_id2}},
        )
        assert final2.get("revision_round") is None
        assert box2["exec_count"] == 1
        assert box2["review_count"] == 1

    with factory2() as db:
        budget2 = db.get(RunBudget, run_id2)
        skipped2 = list(db.scalars(select(RunEvent).where(
            RunEvent.run_id == run_id2, RunEvent.event_type == "agent.revision_skipped",
        )))
        assert budget2.revisions == 0
        assert len(skipped2) == 1 and skipped2[0].payload["reason"] == "UNSAFE_STATE"

    # Crash replay: pre-existing agent.revision_triggered(round=1) avoids double-charging RunBudget.revisions.
    worker3, run_id3, factory3, _ = _seed_durable_run(environment, max_revisions=2)
    with factory3.begin() as db:
        budget3 = db.get(RunBudget, run_id3)
        budget3.revisions = 1
        db.add(RunEvent(
            run_id=run_id3,
            sequence_number=99,
            event_type="agent.revision_triggered",
            payload={"revision_round": 1, "finding_count": 1},
        ))
    box3 = {"calls": [], "exec_count": 0, "review_count": 0, "fail_times": 1}
    with WorkerLock(worker3.engine, worker3.settings) as lock3:
        worker3.lock = lock3
        host3 = FencedAgentHost(
            worker3,
            run_id3,
            STAGE_WORKFLOW_VERSION,
            role_versions=STAGE_VERSIONS,
            plugin_versions={},
            role_bindings=STAGE_ROLE_BINDINGS,
        )
        graph3 = build_stage_graph(
            models={stage: _ScriptedStageModel(stage, box3) for stage in STAGE_KEYS},
            hooks=host3,
            checkpointer=InMemorySaver(),
            workflow_version=STAGE_WORKFLOW_VERSION,
            review_revision_policy="bounded",
        )
        final3 = await graph3.ainvoke(
            {"messages": [("user", "修复边界")]},
            {"configurable": {"thread_id": run_id3}},
        )
        assert final3["revision_round"] == 1
        assert box3["exec_count"] == 2
    with factory3() as db:
        assert db.get(RunBudget, run_id3).revisions == 1
