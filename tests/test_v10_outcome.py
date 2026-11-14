import hashlib

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_m4_collaboration import graph_v4, prepare_v4

from omniharness.action_outcome import compute_action_outcome
from omniharness.artifacts import ArtifactStore
from omniharness.evidence_view import _project_handoff
from omniharness.handoff_contract import build_handoff_v9, build_handoff_v10, handoff_event_payload
from omniharness.m4_contract import M4_V10_PLUGIN_VERSIONS
from omniharness.models import OperationRecord, Run, RunBudgetReservation, ToolOperation, WorkspaceSnapshot
from omniharness.review_evidence import _v9_tool_binding, v9_request_mac, v9_response_mac, v9_snapshot_mac
from omniharness.worker import WorkerLock


@pytest.mark.asyncio
async def test_v10_action_evidence_is_scoped_and_does_not_prove_success(environment, tmp_path):
    worker, run_id, workspace, broker, sandbox, _ = prepare_v4(environment, tmp_path)
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_v4(worker, run_id, workspace, broker, sandbox, InMemorySaver(), strategy="single")
        try:
            await graph.ainvoke({"messages": [("user", "fix and test")]},
                                {"configurable": {"thread_id": run_id}, "recursion_limit": 100})
        finally:
            await scheduler.aclose()
    store = ArtifactStore(worker.settings.data_dir / "artifacts")
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        run.workflow_version = "stage-agent-v10"
        # v10 bindings only accept the frozen v10 plugin set; rebind the v4 fixture snapshot too.
        run.config_snapshot = {**run.config_snapshot, "agent": {**run.config_snapshot["agent"],
                                                              "plugin_versions": M4_V10_PLUGIN_VERSIONS}}
        operations = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
        for op in operations:
            ledger = db.get(OperationRecord, op.operation_record_id)
            ledger.workflow_version = run.workflow_version
            binding = _v9_tool_binding(run, op, ledger.result["purpose"])
            reservation = next(r for r in db.scalars(select(RunBudgetReservation).where(
                RunBudgetReservation.run_id == run_id, RunBudgetReservation.kind == "tool_calls"))
                if r.binding_fingerprint == ledger.result["binding_fingerprint"])
            reservation.binding_fingerprint = binding
            snap = db.get(WorkspaceSnapshot, op.snapshot_id) if op.snapshot_id else None
            req = v9_request_mac(worker.settings, run, op)
            ledger.result = {**ledger.result, "binding_fingerprint": binding, "v9_request_mac": req,
                "v9_snapshot_mac": v9_snapshot_mac(worker.settings, run, op, snap),
                "v9_response_mac": v9_response_mac(worker.settings, run, op.id, req, op.result)}
        db.flush()
        patch = next(x for x in operations if x.tool_name == "workspace_patch")
        action = {"id": "a" * 64, "run_id": run.id, "workspace_id": run.workspace_id,
                  "source": "planner", "kind": "workspace_file_write", "path": patch.parameters["path"],
                  "task_id": patch.result["host_task_id"]}
        run.outcome = {"v10_actions": {"assessed": True, "actions": [action]}}
        result = compute_action_outcome(db, run, store, worker.settings)
        assert result["status"] == "ACTIONS_RECORDED"
        assert result["meaning"] == "action_records_not_goal_success"
        assert result["actions"][0]["operation_ids"] == [patch.id]
        command = next(x for x in operations if x.tool_name == "run_command")
        # The inherited fixture uses a shell string; v10 requires exact argv.
        # Rebind this isolated ledger to model an argv-based host execution.
        if isinstance(command.parameters["command"], str):
            command.parameters = {**command.parameters, "command": ["python", "fixture.py"]}
            ledger = db.get(OperationRecord, command.operation_record_id)
            req = v9_request_mac(worker.settings, run, command)
            ledger.result = {**ledger.result, "v9_request_mac": req,
                "v9_response_mac": v9_response_mac(worker.settings, run, command.id, req, command.result)}
            db.flush()
        command_action = {"id": "b" * 64, "run_id": run.id, "workspace_id": run.workspace_id,
            "source": "planner", "kind": "sandbox_command", "argv": command.parameters["command"],
            "cwd": command.parameters.get("cwd", "."), "task_id": command.result["host_task_id"]}
        run.outcome = {"v10_actions": {"assessed": True, "actions": [action, command_action]}}
        two = compute_action_outcome(db, run, store, worker.settings)
        assert two["status"] == "ACTIONS_RECORDED"
        assert two["actions"][1]["exit_code"] == 0
        assert two["actions"][1]["dimensions"]["runtime_source"] == "UNVERIFIED"
        mismatch = {**command_action, "argv": ["different"]}
        run.outcome = {"v10_actions": {"assessed": True, "actions": [action, mismatch]}}
        assert compute_action_outcome(db, run, store, worker.settings)["status"] == "UNVERIFIED"
        run.outcome = {"v10_actions": {"assessed": True, "actions": [action, command_action]}}
        command.status = "uncertain"
        assert compute_action_outcome(db, run, store, worker.settings)["status"] == "UNVERIFIED"
        command.status = "completed"
        run.outcome = {"v10_actions": {"assessed": True, "actions": [{**action, "task_id": "wrong"}]}}
        assert compute_action_outcome(db, run, store, worker.settings)["status"] == "UNVERIFIED"
        run.outcome = {"v10_actions": {"assessed": True, "actions": [{**action, "workspace_id": "other"}]}}
        assert compute_action_outcome(db, run, store, worker.settings)["status"] == "NOT_ASSESSED"
        run.outcome = {"v10_actions": {"assessed": True, "actions": [action]}}
        patch.result = {**patch.result, "host_task_id": "forged"}
        assert compute_action_outcome(db, run, store, worker.settings)["status"] == "UNVERIFIED"
        run.outcome = {}
        assert compute_action_outcome(db, run, store, worker.settings)["status"] == "NOT_ASSESSED"


def test_v10_handoff_domain_is_separate():
    args = dict(run_id="run", workspace_id="workspace", source_task_id="s", target_task_id="t",
                goal_message_id="goal", task_fingerprint="a" * 64, artifact_id="12345678-1234-1234-1234-123456789abc",
                artifact_sha256=hashlib.sha256(b"report").hexdigest(), report_text="report", operations=[])
    v9 = build_handoff_v9(workflow_version="stage-agent-v9", **args)
    v10 = build_handoff_v10(workflow_version="stage-agent-v10", **args)
    assert v9.handoff_id != v10.handoff_id
    assert _project_handoff(handoff_event_payload(v10))["workflow_version"] == "stage-agent-v10"
