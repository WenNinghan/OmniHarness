"""Isolated PostgreSQL creation and legacy idempotency for action contracts."""
# ruff: noqa: F811
from copy import deepcopy
from uuid import uuid4

import pytest
from test_m4_collaboration import prepare_v4
from test_postgres import postgres_environment  # noqa: F401

from omniharness import m4_services
from omniharness.action_contract import validate_explicit_actions
from omniharness.models import Run, Workspace, WorkspacePolicy
from omniharness.schemas import SendMessageRequest
from omniharness.services import ServiceError, enqueue


def test_new_dependency_contract_and_existing_v1_idempotency(postgres_environment, tmp_path, monkeypatch):
    settings, engine, factory, _ = postgres_environment
    from fastapi.testclient import TestClient

    from omniharness.api import create_app
    from omniharness.security import create_pairing_code

    with TestClient(create_app(settings, engine), base_url="http://localhost:8788",
                    headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        _, template_id, workspace, *_ = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            template = db.get(Run, template_id)
            model_id, workspace_id = template.model_config_id, template.workspace_id
            template.status = "completed"
            assert m4_services.release_workspace_lease(db, template_id)
            row = db.get(Workspace, workspace_id)
            row.physical_key = uuid4().hex
            row.probe = {"identity": workspace["grant"]["expected_identity"]}
            policy = db.get(WorkspacePolicy, (workspace_id, row.policy_version))
            policy.grant = {**policy.grant, "instance_id": "omni-v6-preview"}
        settings.enable_agent = settings.enable_tools = True
        settings.v6_preview = settings.interactive_preview = settings.v10_preview = True
        settings.instance_id = "omni-v6-preview"
        settings.sandbox_key_file = tmp_path / "sandbox.key"
        settings.workspace_service_key_file = tmp_path / "workspace.key"
        settings.sandbox_key_file.write_text("s" * 64, encoding="ascii")
        settings.workspace_service_key_file.write_text("w" * 64, encoding="ascii")
        monkeypatch.setattr(m4_services, "_require_preview_fixture", lambda *_: None)
        conversation_id = client.post("/api/v1/conversations", json={}).json()["id"]
        base = {"content": "write then test", "model_config_id": model_id, "client_request_id": "request-1",
                "workflow_type": "agent", "workspace_id": workspace_id}
        old = [{"kind": "workspace_file_write", "path": "a.py"},
               {"kind": "sandbox_command", "argv": ["pytest"], "cwd": "."}]
        with pytest.raises(ServiceError) as error, factory.begin() as db:
            enqueue(db, conversation_id, SendMessageRequest(**base, action_requirements=old), settings)
        assert error.value.code == "ACTION_REQUIREMENTS_INVALID"
        linked = [{**old[0], "client_id": "write", "depends_on": []},
                  {**old[1], "client_id": "test", "depends_on": ["write"]}]
        with factory.begin() as db:
            result = enqueue(db, conversation_id, SendMessageRequest(**base, action_requirements=linked), settings)
            run = db.get(Run, result["run_id"])
            assert run.config_snapshot["action_contract"]["explicit"]["version"] == 2
            snapshot = deepcopy(run.config_snapshot)
            snapshot["action_contract"]["request"]["requirements"] = old
            snapshot["action_contract"]["explicit"] = validate_explicit_actions(
                old, run_id=run.id, workspace_id=workspace_id)
            run.config_snapshot = snapshot
        # A request retry for an already-existing legacy Run must not be
        # rejected or silently rewritten under the new-Run validation rule.
        with factory.begin() as db:
            retry = enqueue(db, conversation_id, SendMessageRequest(**base, action_requirements=old), settings)
            assert retry["run_id"] == result["run_id"]
            assert db.get(Run, result["run_id"]).config_snapshot["action_contract"]["explicit"]["version"] == 1
