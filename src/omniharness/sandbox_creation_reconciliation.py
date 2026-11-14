"""Candidate evidence-bound slot release; not wired to the production API.

The manager, journal and engine verifier are trusted service dependencies.
This is ineligible for historical operations without a CREATED journal entry.
"""
from __future__ import annotations

from copy import deepcopy

from .m4_reconciliation import _operation, _snapshot, _view
from .m4_services import reclaim_tool_slot, release_workspace_lease, tool_output
from .models import WorkspaceSnapshot, now
from .sandbox_creation_api import candidate_binding
from .sandbox_creation_reclaimer import ReclamationUnknown, reclaim_created
from .services import ServiceError, add_event


def _bound_snapshot(db, operation, run, request, config, record):
    snapshot = db.get(WorkspaceSnapshot, operation.snapshot_id) if operation.snapshot_id else None
    frozen = (run.config_snapshot or {}).get("workspace", {})
    expected_service = {"endpoint": config.endpoint, "image": config.image,
        "snapshot_root": config.snapshot_root, "instance_id": config.instance_id,
        "max_command_seconds": config.max_command_seconds}
    if (operation.tool_name != "run_command" or run.id != request.run_id
            or operation.id != request.operation_id or operation.binding_fingerprint != request.operation_fingerprint
            or record is None or record.state != "CREATED" or not record.sandbox_id
            or operation.sandbox_id not in (None, record.sandbox_id)
            or not isinstance(operation.parameters, dict)
            or operation.parameters.get("timeout_seconds") != request.timeout_seconds
            or snapshot is None or snapshot.workdir != request.snapshot_path
            or snapshot.run_id != run.id or snapshot.workspace_id != run.workspace_id
            or snapshot.task_id != operation.task_id or not isinstance(frozen, dict)
            or frozen.get("id") != run.workspace_id or frozen.get("sandbox") != expected_service):
        raise ServiceError("RECONCILIATION_BINDING_CHANGED", "创建或工作区绑定不完整，保留占位。", 409)
    return {**_snapshot(operation, run), "snapshot_id": operation.snapshot_id, "workdir": snapshot.workdir,
            "command_id": operation.command_id, "slot_claimed": operation.slot_claimed,
            "operation_status": operation.status, "run_status": run.status,
            "ledger_id": operation.operation_record_id}


async def reconcile_created(factory, config, journal, request, body, manager, verify_engine_absence):
    """Explicitly retire one completed creation after proven reclamation."""
    binding = candidate_binding(config, request)
    if body.fingerprint != request.operation_fingerprint:
        raise ServiceError("RECONCILIATION_BINDING_CHANGED", "操作指纹不匹配。", 409)
    record = journal.lookup(binding)
    with factory.begin() as db:
        operation, run, ledger = _operation(db, request.operation_id, body)
        if operation.status == "reconciled":
            existing = (operation.result or {}).get("reconciliation", {})
            if (ledger.status != "reconciled" or existing.get("client_request_id") != body.client_request_id
                    or existing.get("fingerprint") != body.fingerprint):
                raise ServiceError("RECONCILIATION_ALREADY_RECORDED", "操作已有核对记录。", 409)
            return _view(operation, release_workspace_lease(db, run.id))
        snapshot = _bound_snapshot(db, operation, run, request, config, record)
    try:
        evidence = await reclaim_created(record, manager, verify_engine_absence)
    except ReclamationUnknown:
        raise ServiceError("RECONCILIATION_PROBE_FAILED", "未确认回收，保留占位。", 409) from None
    with factory.begin() as db:
        operation, run, ledger = _operation(db, request.operation_id, body)
        if (journal.lookup(binding) != record
                or _bound_snapshot(db, operation, run, request, config, record) != snapshot):
            raise ServiceError("RECONCILIATION_BINDING_CHANGED", "核对期间绑定变化，保留占位。", 409)
        if operation.status == "reconciled":
            existing = (operation.result or {}).get("reconciliation", {})
            if existing.get("client_request_id") != body.client_request_id:
                raise ServiceError("RECONCILIATION_ALREADY_RECORDED", "操作已有核对记录。", 409)
            return _view(operation, release_workspace_lease(db, run.id))
        reconciliation = {"fingerprint": body.fingerprint, "client_request_id": body.client_request_id,
            "verification": "UNVERIFIED", "outcome": "unknown", "evidence": evidence,
            "acknowledged_at": now().isoformat(), "replayed": False}
        operation.status = ledger.status = "reconciled"
        operation.result = {**(operation.result or {}), "reconciliation": reconciliation}
        ledger.result = {**(ledger.result or {}), "reconciliation": deepcopy(reconciliation)}
        operation.error_code = ledger.error_code = "RESULT_UNVERIFIED"
        operation.finished_at = ledger.finished_at = now()
        reclaim_tool_slot(db, operation.id, sandbox_id=operation.sandbox_id)
        db.flush()
        released = release_workspace_lease(db, run.id)
        add_event(db, run, "tool.reconciled", {**tool_output(operation), "workspace_released": released})
        return _view(operation, released)
