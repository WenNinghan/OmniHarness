"""Preview opt-in, local host isolation and workspace boundary contracts."""
from uuid import uuid4

import pytest
from conftest import create_chat
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from omniharness.m4_contract import (
    COLLAB_WORKFLOW_VERSION,
    EVIDENCE_WORKFLOW_VERSION,
    INTERACTIVE_V9_WORKFLOW_VERSION,
    M4_PLUGIN_VERSIONS,
    M4_V9_PLUGIN_VERSIONS,
)
from omniharness.m4_services import freeze_workspace
from omniharness.models import ModelConfig, Run, Workspace
from omniharness.security import create_pairing_code
from omniharness.services import freeze_stage_run_config
from omniharness.settings import Settings
from omniharness.stage_contract import INTERACTIVE_STAGE_WORKFLOW_VERSION, STAGE_WORKFLOW_VERSION


def test_preview_is_explicit_and_requires_dedicated_instance():
    assert Settings(_env_file=None).v6_preview is False
    assert Settings(_env_file=None).interactive_preview is False
    assert Settings(_env_file=None).stage_workflow_version == STAGE_WORKFLOW_VERSION
    common = {"_env_file": None, "v6_preview": True, "enable_agent": True, "enable_tools": True}
    with pytest.raises(ValidationError):
        Settings(**common, instance_id="local")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, v6_preview=True, instance_id="omni-v6-preview",
                 enable_agent=False, enable_tools=True)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, v6_preview=True, instance_id="omni-v6-preview",
                 enable_agent=True, enable_tools=False)
    assert Settings(**common, instance_id="omni-v6-preview").v6_preview is True
    with pytest.raises(ValidationError):
        Settings(_env_file=None, interactive_preview=True, instance_id="omni-v6-preview",
                 enable_agent=True, enable_tools=True)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, interactive_preview=True, v6_preview=True,
                 instance_id="local", enable_agent=True, enable_tools=True)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, interactive_preview=True, v6_preview=True,
                 instance_id="omni-v6-preview", enable_agent=False, enable_tools=True)
    candidate = Settings(**common, instance_id="omni-v6-preview", interactive_preview=True)
    assert candidate.stage_workflow_version == INTERACTIVE_STAGE_WORKFLOW_VERSION
    assert candidate.workspace_workflow_version == INTERACTIVE_V9_WORKFLOW_VERSION


@pytest.fixture
def preview(environment):
    settings, _, factory, original = environment
    settings.v6_preview, settings.instance_id = True, "omni-v6-preview"
    settings.enable_agent = settings.enable_tools = True
    settings.allowed_origins = "http://localhost:8788"
    settings.workspace_service_key_file = settings.secret_dir / "preview-test-service.key"
    settings.workspace_service_key_file.write_text("test-service-only-" * 3)
    with TestClient(original.app, base_url="http://localhost:8788", headers={"X-Omni-Request": "1"}) as client:
        yield settings, factory, client


def test_preview_rejects_ip_alias_before_pairing_cookie(preview):
    settings, factory, client = preview
    code = create_pairing_code(factory)
    denied = client.post("/api/v1/auth/pair", json={"code": code},
                         headers={"Host": "127.0.0.1:8788", "Origin": "http://localhost:8788"})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "PREVIEW_HOST_REQUIRED"
    assert "set-cookie" not in denied.headers
    assert client.get("/health", headers={"Host": "127.0.0.1:8788"}).status_code == 200
    # The internal route remains service-authenticated, not opened by the host exception.
    internal = client.get(f"/api/internal/workspaces/{uuid4()}/grant/1", headers={"Host": "127.0.0.1:8788"})
    assert internal.status_code in {401, 503}
    assert internal.json()["detail"]["code"] != "PREVIEW_HOST_REQUIRED"
    paired = client.post("/api/v1/auth/pair", json={"code": code}, headers={"Origin": "http://localhost:8788"})
    assert paired.status_code == 200
    bootstrap = client.get("/api/v1/bootstrap").json()
    assert bootstrap["v6_preview"] is True
    assert bootstrap["stage_workflow_version"] == STAGE_WORKFLOW_VERSION
    assert bootstrap["workspace_workflow_version"] == EVIDENCE_WORKFLOW_VERSION
    assert settings.instance_id == "omni-v6-preview"


def test_fixture_registration_is_strict_and_does_not_relax_production(preview, monkeypatch):
    _, factory, client = preview
    assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200

    async def probe(self, identity, version, action, **payload):
        assert action == "probe"
        return {"workspace_id": identity, "policy_version": version,
                "root": r"G:\OmniHarness-V6-Preview-Fixture",
                "identity": {"volume_serial": 42, "file_id": 23, "filesystem": "NTFS"}}
    monkeypatch.setattr("omniharness.workspace_client.BrokerClient.request", probe)
    for forbidden in (r"G:\elsewhere", r"G:\x\..\OmniHarness-V6-Preview-Fixture",
                      r"\\?\G:\OmniHarness-V6-Preview-Fixture", r"G:\OmniHarness-V6-Preview-Fixture\nested"):
        response = client.post("/api/v1/workspaces", json={"name": "No", "host_path": forbidden})
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "PREVIEW_WORKSPACE_RESTRICTED"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Workspace)) == 0
    created = client.post("/api/v1/workspaces", json={"name": "Preview", "host_path": "g:/OmniHarness-V6-Preview-Fixture"})
    assert created.status_code == 201, created.text


def test_new_workspace_freeze_is_versioned_and_default_remains_v4(environment, preview, monkeypatch):
    settings, _, factory, _ = environment
    _, _, client = preview
    assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200

    async def probe(self, identity, version, action, **payload):
        return {"workspace_id": identity, "policy_version": version,
                "root": r"G:\OmniHarness-V6-Preview-Fixture",
                "identity": {"volume_serial": 42, "file_id": 23, "filesystem": "NTFS"}}
    monkeypatch.setattr("omniharness.workspace_client.BrokerClient.request", probe)
    monkeypatch.setattr("omniharness.workspace_client.read_service_key", lambda _: "local-key-for-tests")
    monkeypatch.setattr("omniharness.sandbox.SandboxConfig", lambda **_: None)
    workspace = client.post("/api/v1/workspaces", json={"name": "Preview", "host_path": r"G:\OmniHarness-V6-Preview-Fixture"}).json()
    ids = []
    for enabled in (False, True):
        chosen, _, queued = create_chat(client)
        with factory.begin() as db:
            db.get(ModelConfig, chosen["id"]).capabilities = {"tools": {"status": "supported"}}
            run = db.get(Run, queued["run_id"])
            run.config_snapshot = {**run.config_snapshot, "workflow_type": "agent"}
            freeze_stage_run_config(db, run, settings)
            settings.v6_preview = enabled
            freeze_workspace(db, run, settings, workspace["id"])
            ids.append(run.id)
            expected = EVIDENCE_WORKFLOW_VERSION if enabled else COLLAB_WORKFLOW_VERSION
            assert run.workflow_version == run.config_snapshot["agent"]["workflow_version"] == expected
    with factory() as db:
        assert db.get(Run, ids[0]).workflow_version == COLLAB_WORKFLOW_VERSION
        assert db.get(Run, ids[1]).workflow_version == EVIDENCE_WORKFLOW_VERSION


def test_interactive_preview_freezes_only_new_runs_and_matches_bootstrap(environment, preview, monkeypatch):
    settings, _, factory, _ = environment
    _, _, client = preview
    assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200

    async def probe(self, identity, version, action, **payload):
        return {"workspace_id": identity, "policy_version": version,
                "root": r"G:\OmniHarness-V6-Preview-Fixture",
                "identity": {"volume_serial": 42, "file_id": 23, "filesystem": "NTFS"}}
    monkeypatch.setattr("omniharness.workspace_client.BrokerClient.request", probe)
    monkeypatch.setattr("omniharness.workspace_client.read_service_key", lambda _: "local-key-for-tests")
    monkeypatch.setattr("omniharness.sandbox.SandboxConfig", lambda **_: None)
    workspace = client.post("/api/v1/workspaces", json={"name": "Preview", "host_path": r"G:\OmniHarness-V6-Preview-Fixture"}).json()
    ids = []
    for enabled in (False, True):
        settings.interactive_preview = enabled
        bootstrap = client.get("/api/v1/bootstrap").json()
        assert bootstrap["stage_workflow_version"] == (INTERACTIVE_STAGE_WORKFLOW_VERSION if enabled else STAGE_WORKFLOW_VERSION)
        assert bootstrap["workspace_workflow_version"] == (INTERACTIVE_V9_WORKFLOW_VERSION if enabled else EVIDENCE_WORKFLOW_VERSION)
        selected, _, queued = create_chat(client)
        with factory.begin() as db:
            db.get(ModelConfig, selected["id"]).capabilities = {"tools": {"status": "supported"}}
            run = db.get(Run, queued["run_id"])
            run.config_snapshot = {**run.config_snapshot, "workflow_type": "agent"}
            freeze_stage_run_config(db, run, settings)
            assert run.workflow_version == run.config_snapshot["agent"]["workflow_version"] == bootstrap["stage_workflow_version"]
            freeze_workspace(db, run, settings, workspace["id"])
            assert run.workflow_version == run.config_snapshot["agent"]["workflow_version"] == bootstrap["workspace_workflow_version"]
            assert run.config_snapshot["agent"]["plugin_versions"] == (M4_V9_PLUGIN_VERSIONS if enabled else M4_PLUGIN_VERSIONS)
            ids.append(run.id)
    with factory() as db:
        assert db.get(Run, ids[0]).workflow_version == EVIDENCE_WORKFLOW_VERSION
        assert db.get(Run, ids[1]).workflow_version == INTERACTIVE_V9_WORKFLOW_VERSION
