"""Read-only projection of durable task, execution, resource and input facts.

No heartbeat guesses, replay, cancellation, or acceptance inferred from prose.
The caller must provide a consistent read transaction.
"""
from __future__ import annotations

from datetime import timezone

from sqlalchemy import func, select

from .models import (
    AgentRun,
    ArtifactVersion,
    OperationRecord,
    Run,
    RunCommand,
    RunEvent,
    ToolOperation,
    WorkspaceLease,
    now,
)
from .services import TERMINAL, require


def _time(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat() if value is not None else None


def observe_run(db, run_id):
    run = require(db, Run, run_id)
    observed_at = now()
    latest = db.scalar(select(RunEvent).where(RunEvent.run_id == run_id)
                       .order_by(RunEvent.sequence_number.desc()).limit(1))

    def count(model, *conditions):
        return db.scalar(select(func.count()).select_from(model).where(model.run_id == run_id, *conditions)) or 0

    tool_counts = dict(db.execute(select(ToolOperation.status, func.count()).where(
        ToolOperation.run_id == run_id).group_by(ToolOperation.status)).all())
    ledger_unknown = count(OperationRecord, OperationRecord.status == "uncertain")
    pending_ledger = count(OperationRecord, OperationRecord.status == "running")
    slots = count(ToolOperation, ToolOperation.slot_claimed.is_(True))
    lease = db.scalar(select(WorkspaceLease).where(WorkspaceLease.run_id == run_id).limit(1))
    active_models = count(AgentRun, AgentRun.status == "running")
    inflight_tools = sum(tool_counts.get(name, 0) for name in ("dispatched", "running"))
    # A process can die before it updates the request row. The owner-fenced
    # ledger/Run, rather than that row's last saved label, determines whether
    # this is an ordinary in-flight call or an unknown operation.
    unconfirmed = OperationRecord.operation_id.is_(None) | (OperationRecord.status != "running")
    if run.status in TERMINAL or run.status == "pending_review":
        unconfirmed = True
    unknown_inflight = db.scalar(select(func.count()).select_from(ToolOperation).outerjoin(
        OperationRecord, ToolOperation.operation_record_id == OperationRecord.operation_id).where(
            ToolOperation.run_id == run_id, ToolOperation.status.in_(["dispatched", "running"]), unconfirmed)) or 0
    active_tools = inflight_tools - unknown_inflight
    uncertain_tools = tool_counts.get("uncertain", 0) + unknown_inflight
    terminal_unresolved = run.status in TERMINAL and (slots or active_tools or pending_ledger or lease is not None)
    if uncertain_tools or ledger_unknown or terminal_unresolved or run.status == "pending_review":
        phase = "needs_reconciliation"
    elif run.status in {"awaiting_user", "awaiting_approval"}:
        phase = "needs_input"
    elif run.status == "completed":
        phase = "done"
    elif run.status in TERMINAL:
        phase = "incomplete"
    elif run.status in {"cancel_requested", "pause_requested", "paused"}:
        phase = "stopping" if run.status != "paused" else "paused"
    elif run.status == "queued":
        phase = "accepted"
    elif active_tools:
        phase = "executing"
    else:
        latest_node = db.scalar(select(AgentRun).where(AgentRun.run_id == run_id, AgentRun.status == "running")
                                .order_by(AgentRun.created_at.desc()).limit(1))
        phase = "verifying" if latest_node is not None and latest_node.role == "review" else "executing"
    commands = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id,
        ToolOperation.tool_name == "run_command").order_by(ToolOperation.created_at.desc(), ToolOperation.id).limit(20)))
    evidence = []
    for operation in commands:
        result = operation.result if isinstance(operation.result, dict) else {}
        exit_code = result.get("exit_code")
        confirmed = (operation.status in {"completed", "failed"} and type(exit_code) is int
            and bool(operation.command_id) and bool(operation.sandbox_id)
            and result.get("command_id") == operation.command_id and result.get("sandbox_id") == operation.sandbox_id)
        artifacts = list(db.scalars(select(ArtifactVersion.id).where(ArtifactVersion.run_id == run_id,
            ArtifactVersion.operation_id == operation.id, ArtifactVersion.kind == "command_log")
            .order_by(ArtifactVersion.id).limit(4)))
        evidence.append({"operation_id": operation.id, "status": operation.status,
            "command_id": operation.command_id, "sandbox_id": operation.sandbox_id,
            "exit_code": exit_code if confirmed else None, "outcome": "exit_recorded" if confirmed else "unknown",
            "resource_reclaimed": bool(operation.reclaimed_at and not operation.slot_claimed),
            "log_artifact_ids": artifacts, "source": "host_tool_record"})
    events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type.in_(
        ["agent.model_completed", "agent.model_uncertain"]))
        .order_by(RunEvent.sequence_number.desc()).limit(1000)))
    total_calls = count(RunEvent, RunEvent.event_type.in_(["agent.model_completed", "agent.model_uncertain"]))
    cache = {}
    for field in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        values = [(event.payload.get("usage_details") or {}).get(field) for event in events]
        reported = [value for value in values if type(value) is int and value >= 0]
        complete = bool(events) and len(events) == total_calls and len(reported) == len(events) and not active_models
        cache[field] = {"value": sum(reported) if complete else None,
                       "observed": sum(reported) if reported else None,
                       "reported_calls": len(reported), "total_finished_calls": total_calls,
                       "source": "provider_reported" if complete else "partial" if reported else "unknown"}
    prepared_inputs = count(RunCommand, RunCommand.command_type == "append_requirement", RunCommand.status == "applied")
    return {"schema_version": 1, "run_id": run.id, "run_status": run.status, "phase": phase,
        "observed_at": _time(observed_at), "event_watermark": latest.sequence_number if latest else 0,
        "last_event_at": _time(latest.created_at) if latest else None,
        "last_event_type": latest.event_type if latest else None,
        "execution": {"active_model_calls": active_models, "active_tools": active_tools,
                      "uncertain_tools": uncertain_tools, "uncertain_ledger_entries": ledger_unknown},
        "resources": {"tool_slots_held": slots, "workspace_lease_held": lease is not None},
        "inputs": {"pending": count(RunCommand, RunCommand.status == "pending"),
                   "applied_requirements": prepared_inputs, "model_read": "unknown"},
        "evidence": {"command_results": evidence, "command_total": count(ToolOperation,
            ToolOperation.tool_name == "run_command"), "truncated": count(ToolOperation,
            ToolOperation.tool_name == "run_command") > len(evidence),
            "independent_acceptance": "not_recorded_in_this_view", "completion_claim": "unverified"},
        "context": {"input_records": count(RunEvent, RunEvent.event_type == "context.model_input"),
            "summary_artifacts": count(ArtifactVersion, ArtifactVersion.kind == "context_summary"),
            "mask_events": count(RunEvent, RunEvent.event_type == "context.tool_results_masked"),
            "degradation_events": count(RunEvent, RunEvent.event_type == "context.summary_degraded")},
        "cache": cache}
