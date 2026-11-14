from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_m4_api import tool_run, workspace  # noqa: F401

from omniharness.m4_services import acquire_workspace_lease
from omniharness.models import (
    OperationRecord,
    Run,
    ToolOperation,
    Workspace,
    WorkspaceLease,
    WorkspacePolicy,
    WorkspaceSnapshot,
)
from omniharness.sandbox import SandboxConfig
from omniharness.sandbox_creation_api import create_candidate_app
from omniharness.sandbox_creation_authorization import (
    CreationAuthorizationError,
    authorize_command_dispatch,
    authorize_creation,
)
from omniharness.sandbox_creation_journal import CreationJournal


@pytest.fixture
def candidate(environment, workspace):  # noqa: F811
    _, _, factory, _ = environment
    run_id, op_id, _ = tool_run(environment, workspace, requires_approval=False)
    config = SandboxConfig(endpoint="http://localhost:19080", api_key="controller-" * 5,
        image="sha256:" + "a" * 64, snapshot_root="/snapshots", instance_id="test")
    request = SimpleNamespace(run_id=run_id, operation_id=op_id, operation_fingerprint="a" * 64,
                              snapshot_path="/snapshots/run-1/fixture", timeout_seconds=120)
    with factory.begin() as db:
        run, op = db.get(Run, run_id), db.get(ToolOperation, op_id)
        assert acquire_workspace_lease(db, run_id)
        policy = db.get(WorkspacePolicy, (workspace["id"], 1))
        policy.grant = {"instance_id": "test"}
        policy.tool_policy = {"command_approval": False, "tools": ["run_command"]}
        run.config_snapshot = {"workspace": {"id": workspace["id"], "policy_version": 1,
            "grant": policy.grant, "tool_policy": policy.tool_policy, "sandbox": {
                "endpoint": config.endpoint, "image": config.image, "snapshot_root": config.snapshot_root,
                "instance_id": config.instance_id, "max_command_seconds": config.max_command_seconds}}}
        snapshot = WorkspaceSnapshot(id="snapshot-1", workspace_id=workspace["id"], run_id=run_id,
            task_id=op.task_id, policy_version=1, manifest={}, workdir=request.snapshot_path)
        ledger = OperationRecord(operation_id="ledger", run_id=run_id, node_id="agent:execute:tool_calls",
                                 parameters_hash="b" * 64, status="running")
        db.add_all([snapshot, ledger])
        db.flush()
        op.operation_record_id, op.snapshot_id = ledger.operation_id, snapshot.id
        op.status, op.slot_claimed, op.parameters = "dispatched", True, {"timeout_seconds": 120}
    return factory, config, request, workspace["id"]


def test_authorization_matches_exact_run_workspace_snapshot_and_ledger(candidate):
    factory, config, request, _ = candidate
    authorize_creation(factory, config, request)


def test_service_cannot_claim_for_a_legacy_or_differently_frozen_run(candidate):
    factory, config, request, _ = candidate
    called = []
    with pytest.raises(CreationAuthorizationError):
        authorize_creation(factory, config, request, lambda: called.append(True),
                           service_binding={"url": "http://creation:19083", "service_id": "f" * 64})
    assert called == []


@pytest.mark.parametrize("change", [None, "cancel", "policy", "sandbox", "slot"])
def test_precommand_check_rechecks_grant_and_created_identity_without_scheduler_wait(candidate, change):
    factory, config, request, workspace_id = candidate
    service = {"url": "http://creation:19083", "service_id": "f" * 64}
    with factory.begin() as db:
        run, op = db.get(Run, request.run_id), db.get(ToolOperation, request.operation_id)
        run.config_snapshot = {**run.config_snapshot, "workspace": {
            **run.config_snapshot["workspace"], "creation_service": service}}
        op.sandbox_id = "created-sandbox"
        if change == "cancel":
            run.status = "cancel_requested"
        if change == "policy":
            db.get(Workspace, workspace_id).policy_version = 2
        if change == "sandbox":
            op.sandbox_id = "other-sandbox"
        if change == "slot":
            op.slot_claimed = False
    with factory.begin() as db:
        if change is None:
            authorize_command_dispatch(db, config, request, "created-sandbox", service_binding=service)
        else:
            with pytest.raises(CreationAuthorizationError):
                authorize_command_dispatch(db, config, request, "created-sandbox", service_binding=service)


def test_candidate_api_checks_live_database_grant_before_dispatch(candidate, tmp_path):
    factory, config, request, _ = candidate
    calls = []

    class Backend:
        async def create(self, binding, **kwargs):
            calls.append(binding.operation_id)
            return "sandbox-1"

    class Fence:
        async def reserve(self, binding):
            pass

    journal = CreationJournal.initialize(tmp_path / "creation.sqlite", "e" * 64)
    app = create_candidate_app(config, journal, "service-key-" * 4,
        authorize=lambda value, claim: authorize_creation(factory, config, value, claim),
        fence=Fence(), backend=Backend())
    body = {**vars(request), "execd_token": "x" * 32}
    headers = {"X-Creation-Service-Key": "service-key-" * 4}
    with TestClient(app) as client:
        assert client.post("/internal/create", json=body, headers=headers).status_code == 200
        with factory.begin() as db:
            db.get(Run, request.run_id).status = "cancelled"
        assert client.post("/internal/create", json=body, headers=headers).status_code == 403
    assert calls == [request.operation_id]


@pytest.mark.parametrize("case", ["run", "lease", "workspace", "policy", "snapshot", "fingerprint",
                                  "slot", "ledger", "sandbox", "approval", "timeout"])
def test_authorization_rejects_changed_or_unapproved_binding(candidate, case):
    factory, config, request, workspace_id = candidate
    with factory.begin() as db:
        run, op = db.get(Run, request.run_id), db.get(ToolOperation, request.operation_id)
        if case == "run":
            run.status = "cancelled"
        elif case == "lease":
            db.delete(db.get(WorkspaceLease, workspace_id))
        elif case == "workspace":
            db.get(Workspace, workspace_id).enabled = False
        elif case == "policy":
            db.get(Workspace, workspace_id).policy_version = 2
        elif case == "snapshot":
            db.get(WorkspaceSnapshot, op.snapshot_id).workdir = "/snapshots/other"
        elif case == "fingerprint":
            op.binding_fingerprint = "c" * 64
        elif case == "slot":
            op.slot_claimed = False
        elif case == "ledger":
            db.get(OperationRecord, op.operation_record_id).status = "uncertain"
        elif case == "sandbox":
            op.sandbox_id = "another"
        elif case == "approval":
            run.config_snapshot = {**run.config_snapshot, "workspace": {
                **run.config_snapshot["workspace"], "tool_policy": {
                    "command_approval": True, "tools": ["run_command"]}}}
            db.get(WorkspacePolicy, (workspace_id, 1)).tool_policy = {
                "command_approval": True, "tools": ["run_command"]}
        elif case == "timeout":
            op.parameters = {"timeout_seconds": 5}
    with pytest.raises(CreationAuthorizationError):
        authorize_creation(factory, config, request)
