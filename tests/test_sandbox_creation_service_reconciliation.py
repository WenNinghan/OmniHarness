import pytest
from test_m4_api import workspace  # noqa: F401
from test_m4_reconciliation import unknown  # noqa: F401

from omniharness.models import OperationRecord, Run, ToolOperation, WorkspaceLease, WorkspaceSnapshot
from omniharness.sandbox import SandboxResultUnknown
from omniharness.sandbox_creation_client import creation_service_binding


@pytest.mark.parametrize("case", ["ok", "lost_reply", "changed_snapshot", "wrong_id", "legacy_missing_id"])
def test_public_reconciliation_uses_only_frozen_service_evidence(environment, unknown, monkeypatch, case):  # noqa: F811
    settings, _, factory, client = environment
    run_id, operation_id, route, body = unknown
    settings.sandbox_creation_url = "http://creation:19083"
    settings.sandbox_creation_service_id = "a" * 64
    settings.sandbox_creation_key_file = settings.secret_dir / "creation.key"
    settings.sandbox_creation_key_file.write_text("private-test-key-" * 4)
    with factory.begin() as db:
        op, run = db.get(ToolOperation, operation_id), db.get(Run, run_id)
        snapshot = WorkspaceSnapshot(id="creation-snapshot", run_id=run_id, workspace_id=run.workspace_id,
            task_id=op.task_id, policy_version=1, manifest={}, workdir=settings.sandbox_snapshot_root + "/fixture")
        db.add(snapshot)
        db.flush()
        op.snapshot_id = snapshot.id
        op.sandbox_id = "existing-id" if case == "wrong_id" else None
        op.parameters = {"timeout_seconds": 120, "command": "python -V"}
        if case != "legacy_missing_id":
            run.config_snapshot = {**run.config_snapshot, "workspace": {**run.config_snapshot["workspace"],
                "creation_service": creation_service_binding(settings)}}
    requests = []

    async def reclaim(self, request):
        requests.append(request)
        assert request.execd_token is None
        if case == "lost_reply":
            raise SandboxResultUnknown("remote reclamation result unknown")
        if case == "changed_snapshot":
            with factory.begin() as db:
                db.get(WorkspaceSnapshot, "creation-snapshot").workdir += "/changed"
        return {"service_id": "a" * 64, "sandbox_id": "receipt-sandbox", "evidence": {
            "kind": "bound_resource_reclamation", "sandbox_id": "receipt-sandbox",
            "resource_reclaimed": True, "outcome": "unknown", "replayed": False}}

    monkeypatch.setattr("omniharness.sandbox_creation_client.CreationServiceClient.reclaim", reclaim)
    response = client.post(route, json=body)
    assert response.status_code == (200 if case == "ok" else 409), response.text
    with factory() as db:
        run, op = db.get(Run, run_id), db.get(ToolOperation, operation_id)
        assert op.slot_claimed == (case != "ok")
        assert (db.get(WorkspaceLease, run.workspace_id) is None) == (case == "ok")
        assert db.get(OperationRecord, op.operation_record_id).status == ("reconciled" if case == "ok" else "uncertain")
    if case == "ok":
        assert response.json()["reconciliation"]["outcome"] == "unknown"
        assert response.json()["operation"]["sandbox_id"] == "receipt-sandbox"
        assert client.post(route, json=body).json() == response.json()
    assert len(requests) == (0 if case == "legacy_missing_id" else 1)
