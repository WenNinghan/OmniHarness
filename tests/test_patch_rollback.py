"""Tests for manual SHA-checked workspace patch rollback (WorkspaceIO -> Broker -> API)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from omniharness import m4_rollback
from omniharness.models import (
    Conversation,
    ModelConfig,
    Provider,
    Run,
    RunEvent,
    ToolOperation,
    Workspace,
    WorkspaceLease,
)
from omniharness.services import ServiceError
from omniharness.workspace_broker import create_broker_app
from omniharness.workspace_client import BrokerClient
from omniharness.workspace_io import DictWorkspaceRegistry, WorkspaceError, WorkspaceGrant, WorkspaceIO


def _make_io(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8", newline="")
    framework = tmp_path / "framework"
    framework.mkdir()
    registry = DictWorkspaceRegistry([
        WorkspaceGrant("ws-1", "local", str(root), write_allow=("src",)),
    ])
    io = WorkspaceIO(
        registry,
        tmp_path / "storage",
        protected_roots=(framework,),
        require_write_lease=lambda *_: None,
    )
    return io, root


def test_workspace_io_rollback_restores_edited_file_and_prevents_double_rollback(tmp_path):
    io, root = _make_io(tmp_path)
    original = io.read("ws-1", "local", 1, "src/app.py")
    patched = io.patch(
        "ws-1", "local", 1,
        path="src/app.py", content="print('v2')\n",
        expected_sha256=original["sha256"], operation_id="op-edit", run_id="run-1",
    )
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "print('v2')\n"

    receipt = io.rollback("ws-1", "local", 1, "op-edit", "run-1", patched["sha256"])
    assert receipt["action"] == "restored"
    assert receipt["state"] == "rolled_back"
    assert receipt["restored_sha256"] == original["sha256"]
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert (io.storage_root / "recovery/op-edit/rollback.json").is_file()

    with pytest.raises(WorkspaceError) as err:
        io.rollback("ws-1", "local", 1, "op-edit", "run-1", patched["sha256"])
    assert err.value.code == "already_rolled_back"


def test_workspace_io_rollback_deletes_newly_created_file_cleanly(tmp_path):
    io, root = _make_io(tmp_path)
    created = io.patch(
        "ws-1", "local", 1,
        path="src/created.py", content="VALUE = 42\n",
        expected_sha256=None, operation_id="op-create", run_id="run-1",
    )
    target = root / "src" / "created.py"
    assert target.is_file()

    receipt = io.rollback("ws-1", "local", 1, "op-create", "run-1", created["sha256"])
    assert receipt["action"] == "deleted_created_file"
    assert receipt["restored_sha256"] is None
    assert not target.exists()


def test_workspace_io_rollback_refuses_external_edit_or_incomplete_recovery(tmp_path):
    io, root = _make_io(tmp_path)
    original = io.read("ws-1", "local", 1, "src/app.py")
    patched = io.patch(
        "ws-1", "local", 1,
        path="src/app.py", content="print('v2')\n",
        expected_sha256=original["sha256"], operation_id="op-ext", run_id="run-1",
    )

    with pytest.raises(WorkspaceError) as mismatch:
        io.rollback("ws-1", "local", 1, "op-ext", "run-1", "0" * 64)
    assert mismatch.value.code == "version_conflict"

    (root / "src" / "app.py").write_text("print('manual-edit')\n", encoding="utf-8", newline="")
    with pytest.raises(WorkspaceError) as conflict:
        io.rollback("ws-1", "local", 1, "op-ext", "run-1", patched["sha256"])
    assert conflict.value.code == "version_conflict"
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "print('manual-edit')\n"

    # Missing or incomplete result.json fails closed.
    (io.storage_root / "recovery/op-ext/result.json").write_text('{"state":"prepared"}', encoding="utf-8")
    with pytest.raises(WorkspaceError) as incomplete:
        io.rollback("ws-1", "local", 1, "op-ext", "run-1", patched["sha256"])
    assert incomplete.value.code == "recovery_incomplete"


def test_api_rollback_patch_end_to_end_with_broker_and_lease_guards(environment, tmp_path, monkeypatch):
    settings, _engine, factory, client = environment
    io, root = _make_io(tmp_path)
    original = io.read("ws-1", "local", 1, "src/app.py")
    patched = io.patch(
        "ws-1", "local", 1,
        path="src/app.py", content="print('v2')\n",
        expected_sha256=original["sha256"], operation_id="op-api-1", run_id="run-api-1",
    )
    service_key = "k" * 40
    broker_app = create_broker_app(io, instance_id=settings.instance_id, service_key=service_key)
    broker_client = TestClient(broker_app)

    class _FakeBrokerClient:
        def __init__(self, _settings):
            pass

        async def request(self, workspace_id, policy_version, action, **payload):
            resp = broker_client.post(
                f"/workspaces/{workspace_id}/{action}",
                headers={"Authorization": f"Bearer {service_key}", "X-Omni-Instance": settings.instance_id},
                json={"policy_version": policy_version, **payload},
            )
            if resp.status_code != 200:
                code = resp.json()["error"]["code"]
                raise ServiceError("WORKSPACE_" + code.upper(), resp.json()["error"]["message"], 409)
            return resp.json()

    monkeypatch.setattr(m4_rollback, "BrokerClient", _FakeBrokerClient)
    monkeypatch.setattr(BrokerClient, "request", lambda self, ws, ver, act, **kw: _FakeBrokerClient(settings).request(ws, ver, act, **kw))

    with factory.begin() as db:
        provider = Provider(adapter_type="mock")
        db.add(provider)
        db.flush()
        model = ModelConfig(provider_id=provider.id, name="M", model_name="mock")
        db.add(model)
        db.flush()
        conv = Conversation(title="Rollback")
        db.add(conv)
        db.flush()
        ws = Workspace(
            id="ws-1", name="Demo", host_path=str(root), physical_key="phys-1",
            policy_version=1, enabled=True, probe={"status": "ok"},
        )
        db.add(ws)
        db.flush()
        run = Run(
            id="run-api-1", conversation_id=conv.id, model_config_id=model.id,
            model_label="M · mock", ordinal=1,
            status="running", mode="mock",
            workspace_id="ws-1", client_request_id="req-rb-1",
            config_snapshot={"workflow_type": "agent", "workspace": {
                "id": "ws-1",
                "policy_version": 1,
                "broker_endpoint": settings.workspace_broker_url,
                "grant": {"instance_id": settings.instance_id},
            }},
        )
        db.add(run)
        db.flush()
        op = ToolOperation(
            id="op-api-1", run_id="run-api-1", task_id="t1", tool_call_id="call-1",
            tool_name="workspace_patch", tool_version="builtin-workspace-v1",
            parameters={"path": "src/app.py", "expected_sha256": original["sha256"]},
            binding_fingerprint="f" * 64,
            status="completed",
            result={
                "path": "src/app.py",
                "before_sha256": original["sha256"],
                "sha256": patched["sha256"],
                "recovery_id": "op-api-1",
            },
        )
        db.add(op)
        db.add(WorkspaceLease(workspace_id="ws-1", run_id="run-api-1", owner_epoch=1))

    body = {
        "fingerprint": "f" * 64,
        "expected_sha256": patched["sha256"],
        "acknowledge_manual_rollback": True,
        "client_request_id": "rb-req-1",
    }

    # 1. Reject while Run is still active.
    r_active = client.post("/api/v1/tool-operations/op-api-1/rollback", json=body)
    assert r_active.status_code == 409
    assert r_active.json()["detail"]["code"] == "ROLLBACK_RUN_ACTIVE"

    # 2. Reject while WorkspaceLease is still held even if Run is terminal.
    with factory.begin() as db:
        db.get(Run, "run-api-1").status = "completed"
    r_leased = client.post("/api/v1/tool-operations/op-api-1/rollback", json=body)
    assert r_leased.status_code == 409
    assert r_leased.json()["detail"]["code"] == "WORKSPACE_LEASE_ACTIVE"

    # 3. Release lease -> succeeds, restores file, emits event.
    with factory.begin() as db:
        lease = db.get(WorkspaceLease, "ws-1")
        if lease is not None:
            db.delete(lease)
    r_ok = client.post("/api/v1/tool-operations/op-api-1/rollback", json=body)
    assert r_ok.status_code == 200, r_ok.text
    payload = r_ok.json()
    assert payload["rollback"]["action"] == "restored"
    assert payload["rollback"]["restored_sha256"] == original["sha256"]
    assert payload["operation"]["result"]["rollback"]["action"] == "restored"
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "print('v1')\n"

    # 4. Same client_request_id replays idempotently; different client_request_id returns 409.
    r_replay = client.post("/api/v1/tool-operations/op-api-1/rollback", json=body)
    assert r_replay.status_code == 200
    assert r_replay.json()["rollback"] == payload["rollback"]

    r_dup = client.post("/api/v1/tool-operations/op-api-1/rollback", json={**body, "client_request_id": "rb-req-2"})
    assert r_dup.status_code == 409
    assert r_dup.json()["detail"]["code"] == "PATCH_ALREADY_ROLLED_BACK"

    with factory() as db:
        events = list(db.scalars(select(RunEvent).where(
            RunEvent.run_id == "run-api-1", RunEvent.event_type == "workspace.patch_rolled_back",
        )))
        assert len(events) == 1
        assert events[0].payload["operation_id"] == "op-api-1"
        assert json.loads((io.storage_root / "recovery/op-api-1/rollback.json").read_text())["state"] == "rolled_back"
