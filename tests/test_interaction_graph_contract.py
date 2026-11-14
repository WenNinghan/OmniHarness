from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from omniharness.interaction_contract import TaskDecision, UserDecision
from omniharness.m3_agents import AgentBoundaryError
from omniharness.m3_worker import _user_resume_map


def test_interaction_decision_requires_structured_question():
    assert UserDecision.model_validate({"status": "continue", "questions": []}).status == "continue"
    with pytest.raises(ValidationError):
        UserDecision.model_validate({"status": "needs_user", "questions": []})
    with pytest.raises(ValidationError):
        TaskDecision.model_validate({"status": "completed", "content": "问题？", "questions": [{"question": "问题？"}]})


def test_resume_map_requires_same_run_and_answered_record():
    item = SimpleNamespace(id="interrupt", value={"type": "user_clarification", "run_id": "run", "clarification_id": "q"})
    class DB:
        def __init__(self, row): self.row = row
        def get(self, _model, _key): return self.row
    row = SimpleNamespace(id="q", run_id="run", status="pending", answer=None)
    assert _user_resume_map(DB(row), "run", [item]) is None
    row.status, row.answer = "answered", "回答"
    assert _user_resume_map(DB(row), "run", [item]) == {"interrupt": {"clarification_id": "q", "answer": "回答"}}
    row.run_id = "other"
    with pytest.raises(AgentBoundaryError):
        _user_resume_map(DB(row), "run", [item])


def test_v7_handoff_keeps_separate_binding_domain_and_v6_unchanged():
    import hashlib

    from omniharness.evidence_view import _project_handoff
    from omniharness.handoff_contract import build_handoff_v6, build_handoff_v7, handoff_event_payload
    report = "真实记录摘要"
    args = dict(run_id="run", workspace_id="workspace", source_task_id="first", target_task_id="next",
                goal_message_id="goal", artifact_id="7cfbdff1-0c04-4f09-bd4e-333c8664b2b0",
                artifact_sha256=hashlib.sha256(report.encode()).hexdigest(), report_text=report,
                operations=[], task_fingerprint="b" * 64)
    v6 = build_handoff_v6(workflow_version="stage-agent-v6", **args)
    v7 = build_handoff_v7(workflow_version="stage-agent-v7", **args)
    assert v6.workflow_version == "stage-agent-v6" and v7.workflow_version == "stage-agent-v7"
    assert v6.binding_fingerprint != v7.binding_fingerprint
    assert _project_handoff(handoff_event_payload(v6))["validation"]["claim_support"] == "UNVERIFIED"
    assert _project_handoff(handoff_event_payload(v7))["validation"]["claim_support"] == "UNVERIFIED"


@pytest.mark.asyncio
async def test_preflight_questions_and_midtask_resume_keep_dependencies_blocked(monkeypatch):
    import asyncio
    from contextlib import contextmanager

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from test_m3_stages import Hooks

    import omniharness.clarification_service as service
    from omniharness.m3_stages import STAGE_KEYS, build_stage_graph
    from omniharness.stage_contract import INTERACTIVE_STAGE_WORKFLOW_VERSION

    rows = {}
    def create(_db, _run, key, task_id, question, reason, options, stage):
        row = rows.setdefault(key, SimpleNamespace(id=f"q{len(rows)+1}", question=question))
        return row
    monkeypatch.setattr(service, "create_clarification", create)
    class DB:
        def get(self, _model, identity): return SimpleNamespace(id=identity)
    class Worker:
        @contextmanager
        def transaction(self): yield DB()
    class InteractiveHooks(Hooks):
        def __init__(self):
            super().__init__()
            self.worker, self.run_id, self.serial = Worker(), "run", asyncio.Lock()
    observed = []
    class Model:
        def __init__(self, stage): self.stage, self.calls = stage, 0
        async def ainvoke(self, messages):
            self.calls += 1
            observed.append((self.stage, list(messages)))
            last = messages[-1]
            if getattr(last, "id", "") == "omni-interaction:preflight":
                return AIMessage(content='{"status":"needs_user","questions":[{"question":"甲？"},{"question":"乙？"}]}')
            if getattr(last, "id", "") == "omni-interaction:final":
                return AIMessage(content="综合后的最终答复")
            if self.stage == "think":
                return AIMessage(content="理解目标")
            if self.stage == "plan":
                return AIMessage(content='{"steps":[{"id":"s1","task":"完成","acceptance":"准确"}]}')
            if self.stage == "dispatch":
                return AIMessage(content='{"tasks":[{"id":"t1","step_id":"s1","instruction":"执行"}]}')
            if self.stage == "execute":
                if self.calls == 1:
                    return AIMessage(content='{"status":"needs_user","content":"","questions":[{"question":"丙？"}]}')
                assert any("第三答复" in str(m.content) for m in messages)
                return AIMessage(content='{"status":"completed","content":"实际成果","questions":[]}')
            return AIMessage(content='{"summary":"审查结果","findings":[],"unverified":["未验证"]}')
    hooks = InteractiveHooks()
    graph = build_stage_graph(models={stage: Model(stage) for stage in STAGE_KEYS}, hooks=hooks,
                              checkpointer=InMemorySaver(), workflow_version=INTERACTIVE_STAGE_WORKFLOW_VERSION)
    config = {"configurable": {"thread_id": "run"}}
    result = await graph.ainvoke({"messages": [HumanMessage(content="原始目标")]}, config, durability="sync")
    assert len(result["__interrupt__"]) == 1 and len(rows) == 2
    first = result["__interrupt__"][0]
    result = await graph.ainvoke(Command(resume={first.id: {"clarification_id": first.value["clarification_id"], "answer": "第一答复"}}), config, durability="sync")
    second = result["__interrupt__"][0]
    assert second.value["clarification_id"] != first.value["clarification_id"] and len(rows) == 2
    result = await graph.ainvoke(Command(resume={second.id: {"clarification_id": second.value["clarification_id"], "answer": "第二答复"}}), config, durability="sync")
    third = result["__interrupt__"][0]
    assert len(rows) == 3
    result = await graph.ainvoke(Command(resume={third.id: {"clarification_id": third.value["clarification_id"], "answer": "第三答复"}}), config, durability="sync")
    assert result["messages"][-1].content == "综合后的最终答复"
    final_messages = observed[-1][1]
    assert any("原始目标" in str(m.content) for m in final_messages)
    assert any("审查结果" in str(m.content) for m in final_messages)
