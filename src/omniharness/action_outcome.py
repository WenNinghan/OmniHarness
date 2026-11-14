"""V10 action-record projections; no model claim is a completion verdict."""
from __future__ import annotations

from sqlalchemy import select

from .models import OperationRecord, ToolOperation
from .review_evidence import build_review_evidence_v9


def compute_action_outcome(db, run, store, settings):
    """Project only authenticated records from the same Run, scope and task.

    A missing row is never proof of non-execution. This function does not
    authorize or initiate a correction, and does not claim semantic success.
    The caller holds the Run lock and persists the returned bounded projection.
    """
    if run.workflow_version != "stage-agent-v10":
        raise ValueError("Action outcome applies only to v10")
    state = run.outcome if isinstance(run.outcome, dict) else {}
    envelope = state.get("v10_actions")
    base = {"version": 1, "status": "NOT_ASSESSED", "reason": "no_actions",
            "actions": [], "meaning": "action_records_not_goal_success"}
    if not isinstance(envelope, dict) or envelope.get("assessed") is not True:
        if isinstance(envelope, dict) and isinstance(envelope.get("reason"), str):
            base["reason"] = envelope["reason"][:80]
        return base
    actions = envelope.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
        base["reason"] = "invalid_actions"
        return base
    if any(not isinstance(a, dict) or a.get("run_id") != run.id
           or a.get("workspace_id") != run.workspace_id
           or a.get("kind") not in {"workspace_file_write", "sandbox_command"}
           or a.get("source") not in {"planner", "user"}
           or not isinstance(a.get("id"), str) or len(a["id"]) != 64 for a in actions):
        base["reason"] = "invalid_action_scope"
        return base
    packet = build_review_evidence_v9(db, run, store, settings=settings)
    evidence = {item.get("operation_id"): item for item in packet.get("operations", [])}
    operations = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run.id)))
    projections = []
    for action in actions:
        entry = {"id": action["id"], "kind": action["kind"], "source": action["source"],
                 "status": "UNVERIFIED", "reason": "no_matching_record",
                 "operation_ids": [], "dimensions": {"claim_support": "UNVERIFIED"}}
        task = action.get("task_id")
        if not isinstance(task, str) or not task:
            entry["reason"] = "task_scope_unavailable"
            projections.append(entry)
            continue
        for op in operations:
            params = op.parameters if isinstance(op.parameters, dict) else {}
            result = op.result if isinstance(op.result, dict) else {}
            matches = (op.tool_name == "workspace_patch" and action["kind"] == "workspace_file_write"
                       and isinstance(action.get("path"), str) and bool(action["path"])
                       and params.get("path") == action["path"]) or (
                       op.tool_name == "run_command" and action["kind"] == "sandbox_command"
                       and isinstance(action.get("argv"), list) and bool(action["argv"])
                       and all(isinstance(part, str) and part for part in action["argv"])
                       and isinstance(action.get("cwd"), str) and bool(action["cwd"])
                       and params.get("command") == action["argv"]
                       and params.get("cwd", ".") == action["cwd"])
            item = evidence.get(op.id, {})
            dims = item.get("dimensions", {})
            # host_task_id is part of the MAC-bound response; its authenticity
            # requires verified response facts, not merely a completed request.
            response_ok = (item.get("payload_digest_matches") is True if op.tool_name == "workspace_patch"
                           else dims.get("exit_code") == "RECORDED")
            if not (matches and result.get("host_task_id") == task and response_ok
                    and dims.get("execution") == "RECORDED"):
                continue
            entry["operation_ids"].append(op.id)
            if op.tool_name == "workspace_patch":
                entry["dimensions"] = {"execution": "RECORDED", "workspace_write_receipt": "RECORDED",
                                       "content_digest": "MATCHED", "claim_support": "UNVERIFIED"}
            else:
                logs = [x for x in item.get("artifacts", []) if x.get("kind") == "command_log"]
                entry["dimensions"] = {"execution": "RECORDED", "exit_code": "RECORDED",
                    "initial_snapshot": dims.get("initial_snapshot", "UNVERIFIED"),
                    "logs": "VERIFIED" if logs and all(x.get("log_integrity") == "VERIFIED" for x in logs) else "UNVERIFIED",
                    "runtime_source": "UNVERIFIED", "claim_support": "UNVERIFIED"}
                entry["exit_code"] = item.get("exit_code")
                entry["snapshot_id"] = item.get("snapshot_id")
        if entry["operation_ids"]:
            entry["status"], entry["reason"] = "ACTIONS_RECORDED", "host_record_matched"
        projections.append(entry)
    unknown = db.scalar(select(OperationRecord.operation_id).where(
        OperationRecord.run_id == run.id, OperationRecord.status.in_(["running", "uncertain"])).limit(1))
    unknown_tool = any(op.status in {"dispatched", "running", "uncertain", "awaiting_approval"}
                       or op.slot_claimed for op in operations)
    all_recorded = all(a["status"] == "ACTIONS_RECORDED" for a in projections)
    status = "ACTIONS_RECORDED" if all_recorded and not unknown and not unknown_tool else "UNVERIFIED"
    reason = "host_records_only" if status == "ACTIONS_RECORDED" else (
        "pending_or_unknown_call" if unknown or unknown_tool else "evidence_incomplete")
    return {"version": 1, "status": status, "reason": reason, "actions": projections,
            "meaning": "action_records_not_goal_success"}
