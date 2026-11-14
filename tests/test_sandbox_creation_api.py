import sqlite3

from fastapi.testclient import TestClient

from omniharness.sandbox import SandboxConfig
from omniharness.sandbox_creation_api import create_candidate_app
from omniharness.sandbox_creation_journal import CreationJournal


def test_candidate_api_authenticates_and_prevents_changed_binding_replay(tmp_path):
    config = SandboxConfig(endpoint="http://localhost:19080", api_key="controller-" * 5,
        image="sha256:" + "a" * 64, snapshot_root="/snapshots", instance_id="test")
    journal = CreationJournal.initialize(tmp_path / "journal.sqlite", "e" * 64)
    calls = []

    class Backend:
        async def create(self, binding, **kwargs):
            calls.append((binding, kwargs))
            return "sandbox-1"

    class Fence:
        async def reserve(self, _binding):
            pass

    app = create_candidate_app(config, journal, "service-key-" * 4, authorize=lambda _, claim: claim(),
                               fence=Fence(), backend=Backend())
    body = {"run_id": "run-1", "operation_id": "a" * 64, "operation_fingerprint": "b" * 64,
            "snapshot_path": "/snapshots/run-1/fixture", "execd_token": "x" * 32, "timeout_seconds": 120}
    headers = {"X-Creation-Service-Key": "service-key-" * 4}
    with TestClient(app) as client:
        assert client.post("/internal/create", json=body).status_code == 401
        assert calls == []
        response = client.post("/internal/create", json=body, headers=headers)
        assert response.json() == {"state": "CREATED", "sandbox_id": "sandbox-1"}
        assert client.post("/internal/status", json=body, headers=headers).json() == response.json()
        assert client.post("/internal/status", json={k: v for k, v in body.items() if k != "execd_token"},
                           headers=headers).json() == response.json()
        assert client.post("/internal/create", json=body, headers=headers).status_code == 409
        changed = {**body, "snapshot_path": "/snapshots/another"}
        assert client.post("/internal/create", json=changed, headers=headers).status_code == 409
        assert client.post("/internal/status", json=changed, headers=headers).status_code == 409
        assert client.post("/internal/create", json={**body, "snapshot_path": "/outside"},
                           headers=headers).status_code == 422
    assert len(calls) == 1


def test_candidate_api_denied_workspace_never_dispatches(tmp_path):
    config = SandboxConfig(endpoint="http://localhost:19080", api_key="controller-" * 5,
        image="sha256:" + "a" * 64, snapshot_root="/snapshots", instance_id="test")
    journal = CreationJournal.initialize(tmp_path / "journal.sqlite", "e" * 64)
    calls = []

    class Backend:
        async def create(self, *args, **kwargs):
            calls.append(True)
            return "sandbox-1"

    def reject(_, _claim):
        raise PermissionError("private authorization detail")

    class Fence:
        async def reserve(self, _binding):
            raise AssertionError("denied requests must not reserve")

    app = create_candidate_app(config, journal, "service-key-" * 4, authorize=reject,
                               fence=Fence(), backend=Backend())
    body = {"run_id": "run-1", "operation_id": "a" * 64, "operation_fingerprint": "b" * 64,
            "snapshot_path": "/snapshots/run-1/fixture", "execd_token": "x" * 32, "timeout_seconds": 120}
    with TestClient(app) as client:
        response = client.post("/internal/create", json=body, headers={"X-Creation-Service-Key": "service-key-" * 4})
        assert response.status_code == 403 and "private" not in response.text
    assert calls == []


def test_restored_journal_cannot_replay_while_independent_fence_survives(tmp_path):
    config = SandboxConfig(endpoint="http://localhost:19080", api_key="controller-" * 5,
        image="sha256:" + "a" * 64, snapshot_root="/snapshots", instance_id="test")
    path, stale = tmp_path / "journal.sqlite", tmp_path / "stale.sqlite"
    journal = CreationJournal.initialize(path, "e" * 64)
    with sqlite3.connect(path) as source, sqlite3.connect(stale) as backup:
        source.backup(backup)
    calls, markers = [], set()

    class Backend:
        async def create(self, binding, **kwargs):
            calls.append(binding.operation_id)
            return "sandbox-1"

    class Fence:
        async def reserve(self, binding):
            key = (binding.instance_id, binding.operation_id)
            if key in markers:
                raise RuntimeError("marker already exists")
            markers.add(key)

    body = {"run_id": "run-1", "operation_id": "a" * 64, "operation_fingerprint": "b" * 64,
            "snapshot_path": "/snapshots/run-1/fixture", "execd_token": "x" * 32, "timeout_seconds": 120}
    headers = {"X-Creation-Service-Key": "service-key-" * 4}
    kwargs = {"authorize": lambda _, claim: claim(), "fence": Fence(), "backend": Backend()}
    with TestClient(create_candidate_app(config, journal, "service-key-" * 4, **kwargs)) as client:
        assert client.post("/internal/create", json=body, headers=headers).status_code == 200
    restored = CreationJournal(stale, "e" * 64)
    with TestClient(create_candidate_app(config, restored, "service-key-" * 4, **kwargs)) as client:
        assert client.post("/internal/create", json=body, headers=headers).status_code == 503
        assert client.post("/internal/status", json=body, headers=headers).json()["state"] == "DISPATCHED"
    assert calls == ["a" * 64]


def test_service_identity_and_completed_creation_are_required_for_reclamation(tmp_path):
    config = SandboxConfig(endpoint="http://localhost:19080", api_key="controller-" * 5,
        image="sha256:" + "a" * 64, snapshot_root="/snapshots", instance_id="test")
    journal = CreationJournal.initialize(tmp_path / "journal.sqlite", "e" * 64)
    reclaimed = []

    class Backend:
        async def create(self, *args, **kwargs):
            return "sandbox-1"

    class Fence:
        async def reserve(self, binding):
            pass

    async def reclaim(record):
        reclaimed.append(record)
        return {"kind": "bound_resource_reclamation", "sandbox_id": record.sandbox_id,
                "resource_reclaimed": True, "outcome": "unknown", "replayed": False}

    app = create_candidate_app(config, journal, "service-key-" * 4, authorize=lambda _, claim: claim(),
        fence=Fence(), backend=Backend(), service_id="f" * 64, reclaimer=reclaim)
    body = {"run_id": "run-1", "operation_id": "a" * 64, "operation_fingerprint": "b" * 64,
            "snapshot_path": "/snapshots/fixture", "execd_token": "x" * 32, "timeout_seconds": 120}
    headers = {"X-Creation-Service-Key": "service-key-" * 4, "X-Creation-Service-ID": "f" * 64}
    with TestClient(app) as client:
        assert client.post("/internal/create", json=body,
            headers={**headers, "X-Creation-Service-ID": "wrong"}).status_code == 409
        assert client.post("/internal/reclaim", json=body, headers=headers).status_code == 409
        response = client.post("/internal/create", json=body, headers=headers)
        assert response.status_code == 200 and "execd_token" not in response.json()
        receipt = client.post("/internal/reclaim", json=body, headers=headers)
        assert receipt.status_code == 200 and receipt.json()["evidence"]["outcome"] == "unknown"
        assert receipt.json()["operation_fingerprint"] == "b" * 64
        assert receipt.json()["service_id"] == "f" * 64 and len(reclaimed) == 1
