"""M3 slice D: per-role model binding configuration (no execution layer involvement)."""
from conftest import create_chat
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from omniharness.api import create_app
from omniharness.cli import migrate

ROLES = ["think", "plan", "execute", "review", "verifier"]
PATH = "/api/v1/orchestration/bindings"


def test_migration_0004_creates_role_bindings_table(environment):
    settings, engine, _, _ = environment
    assert "role_bindings" in inspect(engine).get_table_names()
    migrate(settings)
    assert "role_bindings" in inspect(engine).get_table_names()


def test_default_bindings_are_empty_and_cover_every_role(environment):
    _, _, _, client = environment
    body = client.get(PATH).json()
    assert body["version"] == 0 and body["updated_at"] is None
    assert [item["role"] for item in body["bindings"]] == ROLES
    assert all(item["model_config_id"] is None and item["enabled"] is False
               for item in body["bindings"])


def test_saving_bindings_returns_labels_and_increments_version(environment):
    _, _, _, client = environment
    model, _, _ = create_chat(client)
    response = client.put(PATH, json={"bindings": [
        {"role": "think", "model_config_id": model["id"]},
        {"role": "review", "model_config_id": None},
    ]})
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1 and body["updated_at"] is not None
    think = next(item for item in body["bindings"] if item["role"] == "think")
    assert think["model_config_id"] == model["id"] and think["enabled"] is True
    assert think["model_label"] is not None
    assert next(item for item in body["bindings"] if item["role"] == "plan")["enabled"] is False


def test_saving_replaces_the_whole_set(environment):
    _, _, _, client = environment
    model, _, _ = create_chat(client)
    client.put(PATH, json={"bindings": [{"role": "think", "model_config_id": model["id"]}]})
    second = client.put(PATH, json={"bindings": [{"role": "execute", "model_config_id": model["id"]}]})
    body = second.json()
    assert body["version"] == 2
    assert next(item for item in body["bindings"] if item["role"] == "think")["enabled"] is False
    assert next(item for item in body["bindings"] if item["role"] == "execute")["enabled"] is True


def test_duplicate_role_and_invalid_model_are_rejected(environment):
    _, _, _, client = environment
    model, _, _ = create_chat(client)
    duplicate = client.put(PATH, json={"bindings": [
        {"role": "think", "model_config_id": model["id"]},
        {"role": "think", "model_config_id": None},
    ]})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "DUPLICATE_ROLE"
    missing = client.put(PATH, json={"bindings": [{"role": "think", "model_config_id": "missing"}]})
    assert missing.status_code == 404
    assert client.put(PATH, json={"bindings": [{"role": "bogus", "model_config_id": None}]}).status_code == 422


def test_disabled_model_cannot_be_bound(environment):
    _, _, _, client = environment
    model, _, _ = create_chat(client)
    assert client.delete(f"/api/v1/model-configs/{model['id']}").status_code == 204
    response = client.put(PATH, json={"bindings": [{"role": "think", "model_config_id": model["id"]}]})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MODEL_DISABLED"


def test_bindings_endpoint_requires_session_and_request_header(environment):
    settings, engine, _, client = environment
    anonymous = TestClient(create_app(settings, engine))
    assert anonymous.get(PATH).status_code == 401
    assert anonymous.put(PATH, json={"bindings": []}, headers={"X-Omni-Request": "1"}).status_code == 401
    assert client.put(PATH, json={"bindings": []},
                      headers={"X-Omni-Request": "0"}).status_code == 403
