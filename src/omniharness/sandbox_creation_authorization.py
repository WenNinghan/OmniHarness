"""Database-bound creation and pre-command authorization.

The database check and downstream journal/SDK call are separate transactions.
Callers must not infer that this check alone fences cancellation or revocation.
"""
from __future__ import annotations

from contextlib import nullcontext

from sqlalchemy import select

from .models import (
    Approval,
    OperationRecord,
    Run,
    ToolOperation,
    Workspace,
    WorkspaceLease,
    WorkspacePolicy,
    WorkspaceSnapshot,
)
from .sandbox import SandboxConfig


class CreationAuthorizationError(PermissionError):
    pass


def authorize_creation(factory, config: SandboxConfig, request, claim=None, *, service_binding=None,
                       _db=None, _created_id=None):
    """Check a dispatched operation against its frozen grant and exact snapshot."""
    with (factory.begin() if _db is None else nullcontext(_db)) as db:
        # Match the Worker/control lock order: Run before ToolOperation. A
        # concurrent duplicate create must not invert the pre-command locks.
        run = db.scalar(select(Run).where(Run.id == request.run_id).with_for_update())
        operation = db.scalar(select(ToolOperation).where(
            ToolOperation.id == request.operation_id).with_for_update())
        if operation is None or run is None or operation.run_id != run.id:
            raise CreationAuthorizationError("Creation authorization unavailable")
        snapshot = db.scalar(select(WorkspaceSnapshot).where(
            WorkspaceSnapshot.id == operation.snapshot_id).with_for_update()) if operation.snapshot_id else None
        workspace = db.scalar(select(Workspace).where(
            Workspace.id == run.workspace_id).with_for_update()) if run and run.workspace_id else None
        lease = db.scalar(select(WorkspaceLease).where(
            WorkspaceLease.workspace_id == run.workspace_id).with_for_update()) if run and run.workspace_id else None
        policy_row = db.scalar(select(WorkspacePolicy).where(WorkspacePolicy.workspace_id == workspace.id,
            WorkspacePolicy.version == workspace.policy_version).with_for_update()) if workspace else None
        ledger = db.scalar(select(OperationRecord).where(OperationRecord.operation_id ==
            operation.operation_record_id).with_for_update()) if operation.operation_record_id else None
        frozen = run.config_snapshot.get("workspace", {}) if run and isinstance(run.config_snapshot, dict) else {}
        if not isinstance(frozen, dict) or not isinstance(operation.parameters, dict):
            raise CreationAuthorizationError("Creation authorization unavailable")
        if service_binding is not None and frozen.get("creation_service") != service_binding:
            raise CreationAuthorizationError("Frozen creation service differs")
        sandbox = frozen.get("sandbox", {})
        expected_sandbox = {"endpoint": config.endpoint, "image": config.image,
            "snapshot_root": config.snapshot_root, "instance_id": config.instance_id,
            "max_command_seconds": config.max_command_seconds}
        if (run is None or run.id != request.run_id or run.status != "running"
                or workspace is None or not workspace.enabled or lease is None or lease.run_id != run.id
                or frozen.get("id") != workspace.id or frozen.get("policy_version") != workspace.policy_version
                or policy_row is None or frozen.get("grant") != policy_row.grant
                or frozen.get("tool_policy") != policy_row.tool_policy
                or sandbox != expected_sandbox or snapshot is None or snapshot.run_id != run.id
                or snapshot.workspace_id != workspace.id or snapshot.task_id != operation.task_id
                or snapshot.policy_version != workspace.policy_version or snapshot.workdir != request.snapshot_path
                or operation.tool_name != "run_command" or operation.status != "dispatched"
                or not operation.slot_claimed or operation.binding_fingerprint != request.operation_fingerprint
                or operation.sandbox_id != _created_id or ledger is None or ledger.run_id != run.id
                or ledger.node_id != "agent:execute:tool_calls" or ledger.status != "running"
                or operation.parameters.get("timeout_seconds") != request.timeout_seconds):
            raise CreationAuthorizationError("Creation authorization unavailable")
        policy = frozen.get("tool_policy", {})
        grant = frozen.get("grant", {})
        if (not isinstance(policy, dict) or not isinstance(grant, dict)
                or grant.get("instance_id") != config.instance_id
                or not isinstance(policy.get("tools"), list) or "run_command" not in policy["tools"]):
            raise CreationAuthorizationError("Creation authorization unavailable")
        if policy.get("command_approval", False):
            approval = db.scalar(select(Approval).where(Approval.operation_id == operation.id).with_for_update())
            if (approval is None or approval.status != "approved"
                    or approval.fingerprint != operation.binding_fingerprint):
                raise CreationAuthorizationError("Creation approval unavailable")
        return claim() if claim is not None else None


def authorize_command_dispatch(db, config, request, sandbox_id, *, service_binding):
    """Recheck after create under the Worker's fenced transaction.

    No scheduler waits or interrupts here: this tool is itself an in-flight
    operation and waiting for all operations to drain would wait on itself.
    """
    if not sandbox_id or service_binding is None:
        raise CreationAuthorizationError("Created sandbox authorization unavailable")
    return authorize_creation(None, config, request, service_binding=service_binding,
                              _db=db, _created_id=sandbox_id)
