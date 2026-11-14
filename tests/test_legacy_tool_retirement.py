from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from test_m4_permissions_postgres import seed

from omniharness.legacy_tool_retirement import (
    legacy_snapshot,
    retire_legacy_tool,
    scope_for_snapshot,
)
from omniharness.m4_services import acquire_workspace_lease
from omniharness.models import OperationRecord, Run, RunEvent, ToolOperation, WorkspaceLease


def setup_legacy(settings, factory):
    settings.sandbox_image = "sha256:" + "1" * 64
    run_id, operation_id = seed(factory, 1)[0]
    ledger_id = uuid4().hex * 2
    with factory.begin() as db:
        run, tool = db.get(Run, run_id), db.get(ToolOperation, operation_id)
        assert acquire_workspace_lease(db, run_id)
        run.status = "cancelled"
        run.config_snapshot = {"workspace": {"id": run.workspace_id, "sandbox": {
            "endpoint": settings.sandbox_endpoint, "image": settings.sandbox_image,
            "snapshot_root": settings.sandbox_snapshot_root, "instance_id": settings.instance_id,
            "max_command_seconds": settings.sandbox_max_command_seconds}}}
        db.add(OperationRecord(operation_id=ledger_id, run_id=run_id, node_id="agent:execute:tool_calls",
            parameters_hash="b" * 64, status="uncertain", result={"outcome": "unknown", "sentinel": "keep"}))
        db.flush()
        tool.operation_record_id, tool.status, tool.slot_claimed = ledger_id, "uncertain", True
        tool.created_at = tool.started_at = datetime.now(timezone.utc) - timedelta(days=4)
    with factory() as db:
        snapshot = legacy_snapshot(db, operation_id)
    return scope_for_snapshot(settings, snapshot), snapshot


def evidence_for(scope, snapshot):
    timestamp = datetime.now(timezone.utc)
    return {"kind": "local_closed_execution_epoch_v1", "scope": scope.manifest(),
        "snapshot_sha256": scope.snapshot_sha256, "resource_reclaimed": True, "replayed": False,
        "outcome": "unknown", "observed_at": timestamp.isoformat(), "epoch": {
            "docker_version": "28.3.3", "live_restore": False, "engine_id": "test-engine",
            "boot_id": str(uuid4()), "boot_utc": (timestamp - timedelta(days=2)).isoformat(),
            "dockerd_started_utc": (timestamp - timedelta(days=1)).isoformat(),
            "containerd_started_utc": (timestamp - timedelta(days=1)).isoformat(),
            "disk_api_agree": True, "runtime_tasks_accounted": True,
            "related_containers": [], "related_sidecars": [],
            "stopped_services": ["api", "worker", "sandbox-controller"], "broker_stopped": True,
            "exclusive_instance": True, "source_audit_sha256": "e" * 64}}


@pytest.fixture
def legacy(environment):
    settings, _, factory, _ = environment
    scope, snapshot = setup_legacy(settings, factory)
    return settings, factory, scope, snapshot


def test_retirement_releases_only_resource_claim_and_keeps_unknown_result(legacy):
    settings, factory, scope, snapshot = legacy
    evidence = evidence_for(scope, snapshot)
    with factory.begin() as db:
        result = retire_legacy_tool(db, settings, scope, snapshot, evidence, request_id="maintenance-1")
    assert result["slot_released"] and result["workspace_released"] and not result["already_retired"]
    with factory.begin() as db:
        result = retire_legacy_tool(db, settings, scope, snapshot, evidence, request_id="maintenance-1")
        assert result["already_retired"]
    with factory() as db:
        tool, ledger, run = db.get(ToolOperation, scope.operation_id), db.get(OperationRecord, scope.ledger_id), db.get(Run, scope.run_id)
        assert tool.status == ledger.status == "reconciled" and not tool.slot_claimed
        assert tool.sandbox_id is tool.command_id is None and run.status == "cancelled"
        assert ledger.result["sentinel"] == "keep" and ledger.result["outcome"] == "unknown"
        assert ledger.result["reconciliation"]["verification"] == "UNVERIFIED"
        assert ledger.result["reconciliation"]["replayed"] is False
        assert db.get(WorkspaceLease, run.workspace_id) is None
        assert db.query(RunEvent).filter_by(run_id=run.id, event_type="tool.reconciled").count() == 1
    with pytest.raises(ValueError, match="different request"), factory.begin() as db:
        retire_legacy_tool(db, settings, scope, snapshot, evidence, request_id="other-request")


@pytest.mark.parametrize("case", ["resource", "sidecar", "disk", "tasks", "broker", "actors", "instance",
    "old_boot", "daemon_before_boot", "stale", "live_restore", "version", "scope", "missing_audit"])
def test_incomplete_or_stale_closure_never_releases_legacy_claim(legacy, case):
    settings, factory, scope, snapshot = legacy
    evidence = evidence_for(scope, snapshot)
    epoch = evidence["epoch"]
    if case == "resource":
        epoch["related_containers"] = ["still-present"]
    elif case == "sidecar":
        epoch["related_sidecars"] = ["still-present"]
    elif case in {"disk", "tasks", "broker", "instance"}:
        epoch[{"disk": "disk_api_agree", "tasks": "runtime_tasks_accounted", "broker": "broker_stopped",
               "instance": "exclusive_instance"}[case]] = False
    elif case == "actors":
        epoch["stopped_services"] = ["api", "worker"]
    elif case == "old_boot":
        epoch["boot_utc"] = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    elif case == "daemon_before_boot":
        epoch["dockerd_started_utc"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    elif case == "stale":
        evidence["observed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    elif case == "live_restore":
        epoch["live_restore"] = True
    elif case == "version":
        epoch["docker_version"] = "other"
    elif case == "scope":
        evidence["scope"] = {**scope.manifest(), "operation_id": "f" * 64}
    else:
        epoch["source_audit_sha256"] = ""
    with pytest.raises(ValueError), factory.begin() as db:
        retire_legacy_tool(db, settings, scope, snapshot, evidence, request_id="maintenance-1")
    with factory() as db:
        assert db.get(ToolOperation, scope.operation_id).slot_claimed
        assert db.get(OperationRecord, scope.ledger_id).status == "uncertain"


def test_changed_operation_and_other_unknown_work_block_retirement(legacy):
    settings, factory, scope, snapshot = legacy
    evidence = evidence_for(scope, snapshot)
    with factory.begin() as db:
        db.get(ToolOperation, scope.operation_id).parameters = {"changed": True}
    with pytest.raises(ValueError, match="changed during"), factory.begin() as db:
        retire_legacy_tool(db, settings, scope, snapshot, evidence, request_id="maintenance-1")
    with factory.begin() as db:
        db.get(ToolOperation, scope.operation_id).parameters = deepcopy(snapshot["tool"]["parameters"])
        db.add(OperationRecord(operation_id="9" * 64, run_id=scope.run_id, node_id="model", parameters_hash="8" * 64,
                               status="uncertain"))
    with pytest.raises(ValueError, match="exact single"), factory.begin() as db:
        retire_legacy_tool(db, settings, scope, snapshot, evidence, request_id="maintenance-1")


@pytest.mark.parametrize("change", [{"sandbox_id": "known-id"}, {"command_id": "command"},
                                   {"slot_claimed": False}, {"status": "running"}])
def test_scope_cannot_be_used_as_generic_unknown_override(legacy, change):
    settings, factory, scope, _ = legacy
    with factory.begin() as db:
        for key, value in change.items():
            setattr(db.get(ToolOperation, scope.operation_id), key, value)
    with factory() as db, pytest.raises(ValueError, match="not the bound terminal"):
        scope_for_snapshot(settings, legacy_snapshot(db, scope.operation_id))
