"""Unknown operations retire only on exact evidence and an explicit user ack."""
from copy import deepcopy
from hashlib import sha256

import pytest
from opensandbox.exceptions import SandboxApiException
from test_m4_api import tool_run, workspace  # noqa: F401

from omniharness.m4_services import acquire_workspace_lease
from omniharness.models import OperationRecord, Run, RunEvent, ToolOperation, WorkspaceLease


@pytest.fixture
def unknown(environment, workspace):  # noqa: F811
    settings, _, factory, client = environment
    settings.sandbox_image = "sha256:" + "1" * 64
    settings.sandbox_key_file = settings.secret_dir / "sandbox.key"
    settings.sandbox_key_file.write_text("isolated-controller-key-" * 3)
    run_id, operation_id, _ = tool_run(environment, workspace, requires_approval=False)
    with factory.begin() as db:
        run, operation = db.get(Run, run_id), db.get(ToolOperation, operation_id)
        assert acquire_workspace_lease(db, run_id)
        run.status = "cancelled"
        frozen = {"id": workspace["id"], "policy_version": 1,
            "broker_endpoint": settings.workspace_broker_url,
            "grant": {"instance_id": settings.instance_id},
            "sandbox": {"endpoint": settings.sandbox_endpoint, "image": settings.sandbox_image,
                "snapshot_root": settings.sandbox_snapshot_root, "instance_id": settings.instance_id,
                "max_command_seconds": settings.sandbox_max_command_seconds}}
        run.config_snapshot = {**run.config_snapshot, "workspace": frozen}
        ledger = OperationRecord(operation_id="ledger", run_id=run_id, node_id="agent:execute:tool_calls",
                                 parameters_hash="b" * 64, status="uncertain", result={"outcome": "unknown"})
        db.add(ledger)
        db.flush()
        operation.operation_record_id = ledger.operation_id
        operation.status, operation.slot_claimed, operation.sandbox_id = "uncertain", True, "sandbox-isolated"
    return run_id, operation_id, f"/api/v1/tool-operations/{operation_id}/reconcile", {
        "fingerprint": "a" * 64, "acknowledge_unverified": True, "client_request_id": "ack-1"}


class Manager:
    def __init__(self, status=404):
        self.status, self.calls, self.closed = status, [], False

    async def get_sandbox_info(self, identity):
        self.calls.append(identity)
        if self.status != 200:
            raise SandboxApiException(message="test response", status_code=self.status)
        return {"id": identity}

    async def close(self):
        self.closed = True


def manager_factory(monkeypatch, manager):
    async def create(**kwargs):
        return manager
    monkeypatch.setattr("omniharness.m4_reconciliation.SandboxManager.create", create)


def assert_locked(factory, run_id):
    with factory() as db:
        run = db.get(Run, run_id)
        assert db.get(WorkspaceLease, run.workspace_id).run_id == run_id
        operation = db.query(ToolOperation).filter_by(run_id=run_id).one()
        assert operation.status == "uncertain" and operation.slot_claimed
        assert db.get(OperationRecord, "ledger").status == "uncertain"


def test_command_404_is_unverified_reclamation_not_success(environment, unknown, monkeypatch):
    _, _, factory, client = environment
    run_id, _, route, body = unknown
    manager = Manager()
    manager_factory(monkeypatch, manager)
    response = client.post(route, json=body)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["workspace_released"] and value["operation"]["status"] == "reconciled"
    assert value["reconciliation"]["verification"] == "UNVERIFIED"
    assert value["reconciliation"]["outcome"] == "unknown"
    assert value["reconciliation"]["evidence"]["resource_reclaimed"] and manager.closed
    assert not value["reconciliation"]["replayed"]
    assert client.post(route, json=body).json() == value
    assert manager.calls == ["sandbox-isolated"]
    with factory() as db:
        run = db.get(Run, run_id)
        assert run.status == "cancelled" and db.get(WorkspaceLease, run.workspace_id) is None
        ledger = db.get(OperationRecord, "ledger")
        assert ledger.status == "reconciled" and ledger.result["outcome"] == "unknown"
        assert db.query(RunEvent).filter_by(run_id=run_id, event_type="tool.reconciled").count() == 1


@pytest.mark.parametrize("case", ["live", "auth", "missing_id", "endpoint", "binding", "active_run", "no_ack", "mcp"])
def test_unproven_command_never_releases(environment, unknown, monkeypatch, case):
    settings, _, factory, client = environment
    run_id, operation_id, route, body = unknown
    manager = Manager(200 if case == "live" else 401 if case == "auth" else 404)
    manager_factory(monkeypatch, manager)
    if case == "endpoint":
        settings.sandbox_endpoint = "http://127.0.0.1:19999"
    elif case == "binding":
        body["fingerprint"] = "b" * 64
    elif case == "no_ack":
        body["acknowledge_unverified"] = False
    else:
        with factory.begin() as db:
            if case == "missing_id":
                db.get(ToolOperation, operation_id).sandbox_id = None
            if case == "active_run":
                db.get(Run, run_id).status = "pending_review"
            if case == "mcp":
                db.get(ToolOperation, operation_id).tool_name = "mcp_call"
    assert client.post(route, json=body).status_code in (409, 422)
    assert_locked(factory, run_id)
    if case not in ("live", "auth"):
        assert manager.calls == []


@pytest.mark.parametrize("mismatch", [None, "path", "before_sha256", "policy_version", "run_id",
                                     "current_sha256", "sha256", "recorded_completed", "state"])
def test_patch_exact_receipt_required_and_never_replayed(environment, unknown, monkeypatch, mismatch):
    settings, _, factory, client = environment
    run_id, operation_id, route, body = unknown
    with factory.begin() as db:
        operation = db.get(ToolOperation, operation_id)
        operation.tool_name, operation.sandbox_id = "workspace_patch", None
        operation.parameters = {"path": "main.py", "content": "print(42)\n", "expected_sha256": "b" * 64}
        workspace_id = db.get(Run, run_id).workspace_id
    digest = sha256(b"print(42)\n").hexdigest()
    receipt = {"workspace_id": workspace_id, "instance_id": settings.instance_id, "run_id": run_id,
        "policy_version": 1, "recovery_id": operation_id, "path": "main.py", "before_sha256": "b" * 64,
        "sha256": digest, "current_sha256": digest, "size": 10, "recorded_completed": True,
        "matches_expected_result": True, "state": "completed"}
    if mismatch:
        receipt[mismatch] = False if mismatch == "recorded_completed" else "wrong"
    calls = []
    async def request(self, workspace_id, policy_version, action, **payload):
        calls.append(action)
        assert action == "operation-result"
        assert payload == {"operation_id": operation_id, "run_id": run_id}
        return deepcopy(receipt)
    monkeypatch.setattr("omniharness.workspace_client.BrokerClient.request", request)
    response = client.post(route, json=body)
    assert calls == ["operation-result"]
    if mismatch:
        assert response.status_code == 409, response.text
        assert_locked(factory, run_id)
    else:
        assert response.status_code == 200, response.text
        assert response.json()["reconciliation"]["outcome"] == "write_receipt_matched"
        assert response.json()["reconciliation"]["verification"] == "UNVERIFIED"
        assert response.json()["workspace_released"]


def test_changed_binding_during_read_only_probe_rejects_commit(environment, unknown, monkeypatch):
    _, _, factory, client = environment
    run_id, operation_id, route, body = unknown
    class ChangedManager(Manager):
        async def get_sandbox_info(self, identity):
            with factory.begin() as db:
                db.get(ToolOperation, operation_id).parameters = {"command": "changed"}
            return await super().get_sandbox_info(identity)
    manager_factory(monkeypatch, ChangedManager())
    assert client.post(route, json=body).status_code == 409
    assert_locked(factory, run_id)
