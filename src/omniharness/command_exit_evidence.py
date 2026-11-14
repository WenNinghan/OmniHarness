"""Bounded host-recorded command-exit bindings for one Run.

These associations establish only what an execution record reports; they do
not verify a command's semantic claims, output contents, or task completion.
The caller owns the read transaction and its consistency boundary.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select

from .models import ArtifactVersion, OperationRecord, Run, ToolOperation

MAX_ASSOCIATIONS = 200
MAX_REFERENCES_PER_RESULT = 200


def _canonical_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _references_artifact(value, artifact):
    if not isinstance(value, list) or len(value) > MAX_REFERENCES_PER_RESULT:
        return False
    if any(not isinstance(item, dict) or not _canonical_uuid(item.get("id")) for item in value):
        return False
    identities = [item["id"] for item in value]
    if len(identities) != len(set(identities)):
        return False
    matches = [item for item in value if item["id"] == artifact.id]
    return (
        len(matches) == 1
        and matches[0].get("run_id") == artifact.run_id
        and matches[0].get("operation_id") == artifact.operation_id
        and matches[0].get("kind") == "command_log"
    )


def collect_command_exit_evidence(db, run_id):
    """Return ``(artifact_id -> (operation_id, exit_code), truncated)``.

    Work is bounded by the first 201 candidate artifacts, not by the number of
    valid records found. Thus ``truncated`` conservatively signals omitted
    candidates even if some would not have passed the verification checks.
    """
    rows = list(
        db.execute(
            select(ArtifactVersion, ToolOperation, OperationRecord, Run.workspace_id)
            .join(ToolOperation, ToolOperation.id == ArtifactVersion.operation_id)
            .join(OperationRecord, OperationRecord.operation_id == ToolOperation.operation_record_id)
            .join(Run, Run.id == ToolOperation.run_id)
            .where(
                ArtifactVersion.run_id == run_id,
                ToolOperation.run_id == run_id,
                OperationRecord.run_id == run_id,
                ArtifactVersion.kind == "command_log",
                ToolOperation.tool_name == "run_command",
                ToolOperation.status.in_(("completed", "failed")),
                OperationRecord.node_id == "agent:execute:tool_calls",
                OperationRecord.status == "completed",
            )
            .order_by(ArtifactVersion.created_at, ArtifactVersion.id)
            .limit(MAX_ASSOCIATIONS + 1)
        )
    )
    truncated = len(rows) > MAX_ASSOCIATIONS
    rows = rows[:MAX_ASSOCIATIONS]
    ledger_ids = {ledger.operation_id for _, _, ledger, _ in rows}
    if not ledger_ids:
        return {}, truncated
    # A reused ledger cannot securely bind either of its tool operations.
    counts = dict(
        db.execute(
            select(ToolOperation.operation_record_id, func.count())
            .where(ToolOperation.operation_record_id.in_(ledger_ids))
            .group_by(ToolOperation.operation_record_id)
        ).all()
    )
    evidence = {}
    for artifact, operation, ledger, workspace_id in rows:
        result = operation.result if isinstance(operation.result, dict) else {}
        envelope = ledger.result if isinstance(ledger.result, dict) else {}
        response = envelope.get("response") if isinstance(envelope.get("response"), dict) else {}
        meta = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
        exit_code = result.get("exit_code")
        if (
            counts.get(ledger.operation_id) != 1
            or not _canonical_uuid(artifact.id)
            or not workspace_id
            or artifact.workspace_id != workspace_id
            or meta.get("complete") is not True
            or type(exit_code) is not int
            or type(response.get("exit_code")) is not int
            or response["exit_code"] != exit_code
            or not isinstance(operation.sandbox_id, str)
            or not operation.sandbox_id
            or not isinstance(operation.command_id, str)
            or not operation.command_id
            or result.get("sandbox_id") != operation.sandbox_id
            or response.get("sandbox_id") != operation.sandbox_id
            or result.get("command_id") != operation.command_id
            or response.get("command_id") != operation.command_id
            or not _references_artifact(result.get("artifacts"), artifact)
            or not _references_artifact(response.get("artifacts"), artifact)
        ):
            continue
        evidence[artifact.id] = (operation.id, exit_code)
    return evidence, truncated
