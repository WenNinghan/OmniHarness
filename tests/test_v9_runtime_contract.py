"""V9 inherits v8 audit safeguards with a separately bound durable domain."""
import pytest
from sqlalchemy import select
from test_m3_host import prepared
from test_v8_invalid_audit import TestHost, call, error

from omniharness import m4_invalid_audit as audit
from omniharness.m3_host import AgentOperationNeedsReview, FencedAgentHost
from omniharness.m4_contract import (
    M4_V8_PLUGIN_VERSIONS,
    M4_V9_PLUGIN_VERSIONS,
    WORKSPACE_WORKFLOWS,
    workspace_prompts,
    workspace_role_versions,
)
from omniharness.models import OperationRecord, Run, ToolOperation
from omniharness.worker import Worker, WorkerLock


class V9Host(TestHost):
    workflow_version = "stage-agent-v9"

    def __init__(self, worker, run_id):
        FencedAgentHost.__init__(self, worker, run_id, self.workflow_version, role_versions={},
                                 plugin_versions=self.plugin_versions, role_bindings={"execute": "execute"})


def test_v9_registry_has_independent_frozen_maps():
    assert "stage-agent-v9" in WORKSPACE_WORKFLOWS
    assert all(M4_V9_PLUGIN_VERSIONS[k] == v for k, v in M4_V8_PLUGIN_VERSIONS.items())
    assert M4_V9_PLUGIN_VERSIONS["builtin-clarification-context"] == "1"
    assert M4_V9_PLUGIN_VERSIONS["builtin-review-evidence"] == "2"
    assert M4_V9_PLUGIN_VERSIONS is not M4_V8_PLUGIN_VERSIONS
    assert {k: v for k, v in workspace_prompts("stage-agent-v9").items() if k != "review"} == {
        k: v for k, v in workspace_prompts("stage-agent-v8").items() if k != "review"}
    assert workspace_prompts("stage-agent-v9")["review"] != workspace_prompts("stage-agent-v8")["review"]
    assert workspace_prompts("stage-agent-v9") is not workspace_prompts("stage-agent-v8")
    assert {k: v for k, v in workspace_role_versions("stage-agent-v9").items() if k != "review"} == {
        k: v for k, v in workspace_role_versions("stage-agent-v8").items() if k != "review"}
    assert workspace_role_versions("stage-agent-v9")["review"] != workspace_role_versions("stage-agent-v8")["review"]
    assert workspace_role_versions("stage-agent-v9") is not workspace_role_versions("stage-agent-v8")


@pytest.mark.asyncio
async def test_v9_audit_replay_claim_limit_and_version_binding(environment, monkeypatch):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    with factory.begin() as db:
        db.get(Run, run_id).workflow_version = "stage-agent-v9"
    worker = Worker(settings, engine)
    monkeypatch.setattr(audit, "_scope", lambda: {"id": "task-1", "write_paths": None})
    assert audit._key(settings, "stage-agent-v8") != audit._key(settings, "stage-agent-v9")
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = V9Host(worker, run_id)
        first = call(run_id, "one")
        saved = await audit.audit_invalid(host, first, "workspace_patch", error(first))
        assert await audit.audit_invalid(host, first, "workspace_patch", error(first)) == saved
        for ident in ("two", "three"):
            request = call(run_id, ident)
            if ident == "three":
                with pytest.raises(audit.InvalidToolLimitReached):
                    await audit.audit_invalid(host, request, "workspace_patch", error(request))
            else:
                await audit.audit_invalid(host, request, "workspace_patch", error(request))
        with factory() as db:
            rows = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
            ledger = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id)))
            assert len(rows) == 3 and all(row.tool_version == audit.V9_POLICY for row in rows)
            assert all(row.workflow_version == "stage-agent-v9" for row in ledger)
            assert any(row.node_id == "v9-tool-preparation" for row in ledger)
            assert any(row.node_id == "invalid-tool-audit-key-v9-v1" for row in ledger)
            with pytest.raises(audit.InvalidToolLimitReached):
                audit.assert_not_limited(db, run_id, "stage-agent-v9")
            audit.assert_not_limited(db, run_id, "stage-agent-v8")
        # A modified persisted verifier cannot be accepted on recovery.
        with factory.begin() as db:
            marker = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id,
                        OperationRecord.node_id == "invalid-tool-audit-key-v9-v1"))
            marker.workflow_version = "stage-agent-v8"
        with pytest.raises(AgentOperationNeedsReview):
            await audit.audit_invalid(host, first, "workspace_patch", error(first))

@pytest.mark.asyncio
async def test_v9_child_ask_user_replays_and_rejects_conflicting_resume(environment, monkeypatch):
    from types import SimpleNamespace

    from omniharness import m4_collaboration, m4_tools
    from omniharness.m3_agents import AgentBoundaryError
    from omniharness.m4_tools import WorkspaceToolGateway
    from omniharness.models import Clarification, RunBudget

    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    with factory.begin() as db:
        db.get(Run, run_id).workflow_version = "stage-agent-v9"
    scope = {"id": "task-one", "write_paths": None}
    monkeypatch.setattr(audit, "_scope", lambda: scope)
    monkeypatch.setattr(m4_collaboration, "current_task_scope", lambda: scope)
    seen = []
    signal_answer = ["answer"]

    def resume(payload):
        question_id = payload["clarification_id"]
        seen.append(question_id)
        with factory.begin() as db:
            row = db.get(Clarification, question_id)
            row.status, row.answer = "answered", "answer"
        return {"clarification_id": question_id, "answer": signal_answer[0]}

    monkeypatch.setattr(m4_tools, "interrupt", resume)
    async def wait_operations():
        pass
    worker = Worker(settings, engine)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        gateway = WorkspaceToolGateway.__new__(WorkspaceToolGateway)
        gateway.host = V9Host(worker, run_id)
        gateway.scheduler = SimpleNamespace(wait_operations=wait_operations)
        question = call(run_id, "same-question", args={"question": "clarify"})
        question.tool_call["name"] = "ask_user"
        first, replay = await gateway._ask_user(question), await gateway._ask_user(question)
        assert first.content == replay.content and seen[0] == seen[1]
        signal_answer[0] = "forged"
        with pytest.raises(AgentBoundaryError):
            await gateway._ask_user(question)
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == 1
            assert len(list(db.scalars(select(Clarification).where(Clarification.run_id == run_id)))) == 1

@pytest.mark.asyncio
async def test_v9_rejects_foreign_v8_audit_marker(environment, monkeypatch):
    from omniharness.services import operation_id
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    with factory.begin() as db:
        db.get(Run, run_id).workflow_version = "stage-agent-v9"
        db.add(OperationRecord(operation_id=operation_id(run_id, "invalid-tool-audit-key-v1", {}),
                               run_id=run_id, node_id="invalid-tool-audit-key-v1", parameters_hash="foreign",
                               status="completed", attempts=1, workflow_version="stage-agent-v8"))
    monkeypatch.setattr(audit, "_scope", lambda: {"id": "task-1", "write_paths": None})
    worker = Worker(settings, engine)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        request = call(run_id, "one")
        with pytest.raises(AgentOperationNeedsReview):
            await audit.audit_invalid(V9Host(worker, run_id), request, "workspace_patch", error(request))


@pytest.mark.asyncio
async def test_v9_ask_replay_rechecks_limit_after_interleaved_invalid_calls(environment, monkeypatch):
    from types import SimpleNamespace

    from omniharness import m4_collaboration, m4_tools
    from omniharness.m4_tools import WorkspaceToolGateway
    from omniharness.models import Clarification

    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    with factory.begin() as db:
        db.get(Run, run_id).workflow_version = "stage-agent-v9"
    scope = {"id": "task-one", "write_paths": None}
    monkeypatch.setattr(audit, "_scope", lambda: scope)
    monkeypatch.setattr(m4_collaboration, "current_task_scope", lambda: scope)
    seen = []

    def resume(payload):
        question_id = payload["clarification_id"]
        seen.append(question_id)
        with factory.begin() as db:
            row = db.get(Clarification, question_id)
            row.status, row.answer = "answered", "answer"
        return {"clarification_id": question_id, "answer": "answer"}

    monkeypatch.setattr(m4_tools, "interrupt", resume)
    async def wait_operations():
        pass

    worker = Worker(settings, engine)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = V9Host(worker, run_id)
        gateway = WorkspaceToolGateway.__new__(WorkspaceToolGateway)
        gateway.host = host
        gateway.scheduler = SimpleNamespace(wait_operations=wait_operations)
        question = call(run_id, "same-question", args={"question": "clarify"})
        question.tool_call["name"] = "ask_user"
        await gateway._ask_user(question)

        original_guard = audit.guard_valid_invocation

        async def interleaved_guard(*args):
            await original_guard(*args)
            for index in range(3):
                invalid = call(run_id, f"invalid-{index}")
                if index == 2:
                    with pytest.raises(audit.InvalidToolLimitReached):
                        await audit.audit_invalid(host, invalid, "workspace_patch", error(invalid))
                else:
                    await audit.audit_invalid(host, invalid, "workspace_patch", error(invalid))

        monkeypatch.setattr(audit, "guard_valid_invocation", interleaved_guard)
        with pytest.raises(audit.InvalidToolLimitReached):
            await gateway._ask_user(question)
        assert len(seen) == 1
