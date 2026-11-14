"""Local-admin retirement after a verified closed host execution epoch.

Not an HTTP proof format. Only the maintenance tool collects host evidence;
ordinary reconciliation continues to reject legacy commands without an ID.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import select

from .m4_services import reclaim_tool_slot, release_workspace_lease, tool_output
from .models import OperationRecord, Run, ToolOperation, now
from .services import TERMINAL, add_event

UNSAFE_TOOLS = {"running", "result_unknown", "uncertain", "executing", "awaiting_approval", "approved"}
UNSAFE_OPERATIONS = {"running", "uncertain", "result_unknown"}


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("Missing maintenance timestamp")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class LegacyMaintenanceScope:
    instance_id: str
    run_id: str
    operation_id: str
    ledger_id: str
    snapshot_sha256: str

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.instance_id):
            raise ValueError("Invalid maintenance instance")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.run_id):
            raise ValueError("Invalid maintenance Run")
        if any(not re.fullmatch(r"[a-f0-9]{64}", value)
               for value in (self.operation_id, self.ledger_id, self.snapshot_sha256)):
            raise ValueError("Invalid maintenance operation/snapshot binding")

    def manifest(self):
        return asdict(self)

    def validate_inventory(self, value):
        if (value.get("active_runs") != 0 or value.get("unsafe_tools") != 1
                or value.get("uncertain_operations") != 1
                or value.get("unsafe_tool_ids") != [self.operation_id]
                or value.get("unsafe_operation_ids") != [self.ledger_id]):
            raise ValueError("Maintenance permits only the exact single legacy tool and ledger")


def legacy_snapshot(db, operation_id, *, lock=False):
    initial = db.get(ToolOperation, operation_id)
    if initial is None:
        raise ValueError("Legacy tool not found")
    run_query = select(Run).where(Run.id == initial.run_id).execution_options(populate_existing=True)
    tool_query = select(ToolOperation).where(ToolOperation.id == operation_id).execution_options(populate_existing=True)
    run = db.scalar(run_query.with_for_update() if lock else run_query)
    tool = db.scalar(tool_query.with_for_update() if lock else tool_query)
    ledger_query = select(OperationRecord).where(OperationRecord.operation_id == tool.operation_record_id)
    ledger_query = ledger_query.execution_options(populate_existing=True)
    ledger = db.scalar(ledger_query.with_for_update() if lock else ledger_query)
    if run is None or ledger is None or tool.run_id != run.id:
        raise ValueError("Legacy execution binding is incomplete")
    tool_fields = ("id", "run_id", "operation_record_id", "task_id", "tool_call_id", "tool_name", "tool_version",
                   "parameters", "binding_fingerprint", "status", "result", "error_code", "snapshot_id",
                   "sandbox_id", "command_id", "slot_claimed", "created_at", "started_at", "finished_at", "reclaimed_at")
    ledger_fields = ("operation_id", "run_id", "node_id", "parameters_hash", "status", "result", "error_code",
                     "attempts", "workflow_version", "created_at", "started_at", "finished_at")

    def projection(row, fields):
        return {key: (_utc(value).isoformat() if isinstance(value := getattr(row, key), datetime)
                      else deepcopy(value)) for key in fields}

    return {"run_id": run.id, "run_status": run.status, "workspace_id": run.workspace_id,
            "workspace": deepcopy(run.config_snapshot.get("workspace", {})),
            "tool": projection(tool, tool_fields), "ledger": projection(ledger, ledger_fields)}


def scope_for_snapshot(settings, snapshot):
    tool, ledger = snapshot["tool"], snapshot["ledger"]
    expected = {"endpoint": settings.sandbox_endpoint, "image": settings.sandbox_image,
                "snapshot_root": settings.sandbox_snapshot_root, "instance_id": settings.instance_id,
                "max_command_seconds": settings.sandbox_max_command_seconds}
    if (snapshot["run_status"] not in TERMINAL or not snapshot["workspace_id"]
            or snapshot["workspace"].get("sandbox") != expected
            or snapshot["workspace"].get("creation_service") is not None
            or tool["tool_name"] != "run_command" or tool["status"] != "uncertain"
            or tool["slot_claimed"] is not True or tool["sandbox_id"] is not None or tool["command_id"] is not None
            or ledger["run_id"] != snapshot["run_id"] or ledger["status"] != "uncertain"
            or ledger["node_id"] != "agent:execute:tool_calls"):
        raise ValueError("Operation is not the bound terminal missing-ID legacy case")
    return LegacyMaintenanceScope(settings.instance_id, snapshot["run_id"], tool["id"], ledger["operation_id"], digest(snapshot))


def require_only_scope(db, scope):
    active = db.scalar(select(Run.id).where(Run.status.not_in(TERMINAL)).limit(1))
    tools = list(db.scalars(select(ToolOperation.id).where(
        ToolOperation.slot_claimed | ToolOperation.status.in_(UNSAFE_TOOLS)).order_by(ToolOperation.id)))
    operations = list(db.scalars(select(OperationRecord.operation_id).where(
        OperationRecord.status.in_(UNSAFE_OPERATIONS)).order_by(OperationRecord.operation_id)))
    scope.validate_inventory({"active_runs": int(active is not None), "unsafe_tools": len(tools),
                              "uncertain_operations": len(operations), "unsafe_tool_ids": tools,
                              "unsafe_operation_ids": operations})


def validate_closed_epoch(scope, snapshot, evidence):
    if (evidence.get("kind") != "local_closed_execution_epoch_v1" or evidence.get("scope") != scope.manifest()
            or evidence.get("resource_reclaimed") is not True or evidence.get("replayed") is not False
            or evidence.get("outcome") != "unknown" or evidence.get("snapshot_sha256") != digest(snapshot)):
        raise ValueError("Closed-epoch evidence does not match the legacy operation")
    epoch = evidence["epoch"]
    if (epoch["docker_version"] != "28.3.3" or epoch["live_restore"] is not False
            or not re.fullmatch(r"[a-f0-9-]{36}", epoch["boot_id"])
            or not epoch["engine_id"] or not epoch["disk_api_agree"] or not epoch["runtime_tasks_accounted"]
            or epoch["related_containers"] or epoch["related_sidecars"]
            or set(epoch["stopped_services"]) != {"api", "worker", "sandbox-controller"}
            or epoch["broker_stopped"] is not True or epoch["exclusive_instance"] is not True
            or not epoch["source_audit_sha256"]):
        raise ValueError("Host execution or resource closure is not verified")
    dispatched = _utc(snapshot["tool"]["started_at"] or snapshot["tool"]["created_at"])
    boot, daemon, containerd = (_utc(epoch[key]) for key in ("boot_utc", "dockerd_started_utc", "containerd_started_utc"))
    observed = _utc(evidence["observed_at"])
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    if not dispatched < boot <= min(daemon, containerd) or max(daemon, containerd) > observed or not 0 <= age <= 30:
        raise ValueError("Old request epoch is still live or closure evidence is stale")


def retire_legacy_tool(db, settings, scope, snapshot, evidence, *, request_id):
    """Atomically retire only the maintenance-scoped operation, never replay it."""
    current = legacy_snapshot(db, scope.operation_id, lock=True)
    tool = db.get(ToolOperation, scope.operation_id)
    ledger = db.get(OperationRecord, scope.ledger_id)
    if (tool.run_id != scope.run_id or tool.operation_record_id != scope.ledger_id
            or settings.instance_id != scope.instance_id or not isinstance(request_id, str) or not request_id):
        raise ValueError("Legacy retirement request belongs to another binding")
    if tool.status == "reconciled":
        prior = (tool.result or {}).get("reconciliation", {})
        if (ledger is None or ledger.status != "reconciled" or prior.get("client_request_id") != request_id
                or prior.get("maintenance_snapshot_sha256") != scope.snapshot_sha256
                or prior.get("evidence", {}).get("scope") != scope.manifest()):
            raise ValueError("Legacy operation was already retired by a different request")
        return {"operation_id": tool.id, "already_retired": True, "outcome": "unknown",
                "slot_released": not tool.slot_claimed, "workspace_released": release_workspace_lease(db, tool.run_id)}
    if current != snapshot or digest(current) != scope.snapshot_sha256 or scope_for_snapshot(settings, current) != scope:
        raise ValueError("Legacy operation changed during maintenance")
    require_only_scope(db, scope)
    validate_closed_epoch(scope, current, evidence)
    reconciliation = {"fingerprint": tool.binding_fingerprint, "client_request_id": request_id,
                      "verification": "UNVERIFIED", "outcome": "unknown", "replayed": False,
                      "maintenance_snapshot_sha256": scope.snapshot_sha256,
                      "evidence": deepcopy(evidence), "acknowledged_at": now().isoformat()}
    tool.status = ledger.status = "reconciled"
    tool.result = {**(tool.result or {}), "reconciliation": reconciliation}
    ledger.result = {**(ledger.result or {}), "reconciliation": deepcopy(reconciliation)}
    tool.error_code = ledger.error_code = "RESULT_UNVERIFIED"
    tool.finished_at = ledger.finished_at = now()
    reclaim_tool_slot(db, tool.id)
    db.flush()
    released = release_workspace_lease(db, tool.run_id)
    run = db.get(Run, tool.run_id)
    add_event(db, run, "tool.reconciled", {**tool_output(tool), "workspace_released": released,
                                         "maintenance": "closed_execution_epoch"})
    return {"operation_id": tool.id, "already_retired": False, "outcome": "unknown",
            "slot_released": not tool.slot_claimed, "workspace_released": released}
