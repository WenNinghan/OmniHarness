from pathlib import Path

import pytest
from conftest import create_chat

from omniharness.artifacts import ArtifactStore
from omniharness.m4_services import (
    acquire_workspace_lease,
    claim_tool_slot,
    ensure_approval,
    ensure_tool_request,
    finish_tool_request,
    reclaim_tool_slot,
    release_workspace_lease,
)
from omniharness.models import Approval, ArtifactVersion, Run, WorkspaceLease
from omniharness.services import ServiceError


@pytest.fixture
def workspace(environment, monkeypatch):
    settings, _, factory, client = environment
    settings.enable_tools = True
    settings.workspace_service_key_file = settings.secret_dir / "workspace.key"
    settings.workspace_service_key_file.write_text("test-service-only-" * 3)

    async def request(self, identity, version, action, **payload):
        assert action == "probe"
        return {"workspace_id": identity, "policy_version": version, "root": "G:/sample",
                "identity": {"volume_serial": 42, "file_id": 23, "filesystem": "NTFS"}}
    monkeypatch.setattr("omniharness.workspace_client.BrokerClient.request", request)
    response = client.post("/api/v1/workspaces", json={
        "name": "Sample", "host_path": "G:/sample", "write_allow": ["."], "command_approval": True,
    })
    assert response.status_code == 201, response.text
    return response.json()


def tool_run(environment, workspace, *, suffix="1", requires_approval=True):
    _, _, factory, client = environment
    _, _, result = create_chat(client)
    with factory.begin() as db:
        run = db.get(Run, result["run_id"])
        run.status, run.workspace_id = "running", workspace["id"]
        operation = ensure_tool_request(db, run_id=run.id, operation_id=suffix * 64,
            task_id="task", tool_call_id="call", tool_name="run_command", tool_version="v1",
            parameters={"command": "python -V"}, binding_fingerprint="a" * 64,
            requires_approval=requires_approval)
        approval = ensure_approval(db, operation) if requires_approval else None
        return run.id, operation.id, approval.id if approval else None


def test_workspace_registration_and_broker_auth_are_separate(environment, workspace):
    settings, _, _, client = environment
    assert workspace["ready"] and ".env" in workspace["excludes"]
    url = f"/api/internal/workspaces/{workspace['id']}/grant/1"
    assert client.get(url).status_code == 401  # A paired cookie alone is insufficient.
    headers = {"Authorization": "Bearer " + settings.workspace_service_key_file.read_text(),
               "X-Omni-Instance": settings.instance_id}
    grant = client.get(url, headers=headers).json()
    assert grant["expected_identity"]["file_id"] == 23
    client.patch(f"/api/v1/workspaces/{workspace['id']}", json={"enabled": False})
    assert not client.get(url, headers=headers).json()["enabled"]
    assert client.post("/api/v1/workspaces", json={"name": "x", "host_path": "G:/x",
                                                 "write_allow": ["../escape"]}).status_code == 422


def test_duplicate_physical_workspace_refused(environment, workspace):
    client = environment[3]
    response = client.post("/api/v1/workspaces", json={"name": "Alias", "host_path": "G:/alias"})
    assert response.status_code == 409
    entries = client.get("/api/v1/workspaces").json()
    assert len([item for item in entries if item["ready"]]) == 1


def test_approval_exact_binding_repeat_cancel_and_pause(environment, workspace):
    _, _, factory, client = environment
    run_id, operation_id, approval_id = tool_run(environment, workspace)
    route = f"/api/v1/approvals/{approval_id}/decision"
    body = {"decision": "approve", "fingerprint": "a" * 64, "client_request_id": "approve-1"}
    assert client.post(route, json={**body, "fingerprint": "b" * 64}).status_code == 409
    with factory.begin() as db:
        db.get(Run, run_id).status = "paused"
    assert client.post(route, json=body).status_code == 202
    assert client.post(route, json=body).status_code == 202
    assert client.post(route, json={**body, "decision": "reject"}).status_code == 409
    with factory() as db:
        assert db.get(Run, run_id).status == "paused"
    assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    assert client.post(route, json={**body, "client_request_id": "late"}).status_code == 409
    with factory() as db:
        assert db.get(Approval, approval_id).status == "cancelled"


def test_lease_and_tool_slots_preserve_uncertain_results(environment, workspace):
    factory = environment[2]
    run1, op1, _ = tool_run(environment, workspace, suffix="1", requires_approval=False)
    run2, op2, _ = tool_run(environment, workspace, suffix="2", requires_approval=False)
    run3, op3, _ = tool_run(environment, workspace, suffix="3", requires_approval=False)
    with factory.begin() as db:
        assert acquire_workspace_lease(db, run1)
        assert not acquire_workspace_lease(db, run2)
        assert claim_tool_slot(db, run1, op1)
        assert claim_tool_slot(db, run2, op2)
        assert not claim_tool_slot(db, run3, op3)
        finish_tool_request(db, op1, "uncertain", error_code="RESULT_UNKNOWN")
        assert not release_workspace_lease(db, run1)
        reclaim_tool_slot(db, op1)
        assert claim_tool_slot(db, run3, op3)
        assert not release_workspace_lease(db, run1)  # Reclamation is not proof of a command result.
        assert db.get(WorkspaceLease, workspace["id"]).run_id == run1


def test_immutable_artifact_scope_content_and_tampering(environment, workspace):
    settings, _, factory, client = environment
    run_id, _, _ = tool_run(environment, workspace, requires_approval=False)
    store = ArtifactStore(settings.data_dir / "artifacts")
    with factory.begin() as db:
        reference = store.put(db, run_id=run_id, workspace_id=workspace["id"],
                              name="test.log", kind="stdout", content="first\n\n中文\nlast")
        row = db.get(ArtifactVersion, reference["id"])
        with pytest.raises(ServiceError, match="其他任务"):
            store.read(row, run_id="other-run")
    route = f"/api/v1/artifacts/{reference['id']}/content"
    assert client.get(route).text == "first\n\n中文\nlast"
    assert "storage_path" not in client.get(f"/api/v1/runs/{run_id}/artifacts").json()[0]
    Path(store.root / reference["id"]).write_text("changed")
    assert client.get(route).status_code == 409


def test_context_configuration_exports_and_freezes_without_changing_output(environment):
    client = environment[3]
    response = client.post('/api/v1/model-configs', json={
        'name': 'Context model', 'adapter_type': 'mock', 'model_name': 'demo',
        'max_output_tokens': 100000, 'context_policy': {
            'schema_version': 1, 'policy': {'input_target_tokens': 12000},
            'model_limits': {'context_window_tokens': None},
        },
    })
    assert response.status_code == 201, response.text
    model = response.json()
    assert model['max_output_tokens'] == 100000
    assert model['context_policy']['policy']['input_target_tokens'] == 12000
    assert model['context_policy']['policy']['summary_output_tokens'] == 4000
    exported = client.get('/api/v1/configuration/export').json()
    assert exported['model_configs'][0]['context_policy'] == model['context_policy']
    assert 'api_key' not in exported['model_configs'][0]
    invalid = client.post('/api/v1/model-configs', json={
        'name': 'invalid', 'adapter_type': 'mock', 'model_name': 'demo',
        'context_policy': {'policy': {'summarize_at': 1.1}},
    })
    assert invalid.status_code == 422


@pytest.mark.parametrize('page_size', [1, 2, 4, 7])
def test_artifact_pages_never_split_chinese_or_emoji(environment, workspace, page_size):
    settings, _, factory, client = environment
    run_id, _, _ = tool_run(environment, workspace)
    original = '中文\n\nA🙂终点'
    with factory.begin() as db:
        artifact = ArtifactStore(settings.data_dir / 'artifacts').put(db, run_id=run_id,
            name='中文.txt', kind='test', content=original)
    offset, parts = 0, []
    while offset < artifact['size_bytes']:
        response = client.get(f"/api/v1/artifacts/{artifact['id']}/content?offset={offset}&limit={page_size}")
        assert response.status_code == 200
        parts.append(response.text)
        next_offset = int(response.headers['X-Artifact-Next-Offset'])
        assert offset < next_offset <= min(artifact['size_bytes'], offset + page_size + 3)
        offset = next_offset
    assert ''.join(parts) == original
    assert client.get(f"/api/v1/artifacts/{artifact['id']}/content?offset=1&limit=4").status_code == 422
