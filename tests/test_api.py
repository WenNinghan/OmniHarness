import json
from datetime import timedelta

import pytest
from conftest import create_chat
from sqlalchemy import func, select

from omniharness.models import LocalSession, ModelConfig, PairingCode, Provider, Run, now
from omniharness.security import SecretStore, create_pairing_code, digest
from omniharness.services import runtime_config


def sse_events(response):
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_m3_preview_stage_does_not_override_agent_readiness(environment):
    settings, _, _, client = environment
    settings.enable_agent = True  # SQLite remains unsupported for persistent Agent runs.
    assert client.get("/health").json()["stage"] == "M4-preview"
    bootstrap = client.get("/api/v1/bootstrap").json()
    assert bootstrap["stage"] == "M4-preview"
    assert bootstrap["agent_available"] is False
    assert client.get("/").json()["stage"] == "M4-preview"
    client.cookies.clear()
    unpaired = client.get("/api/v1/bootstrap").json()
    assert unpaired["stage"] == "M4-preview"
    assert unpaired["agent_available"] is False


def test_unpaired_browser_cannot_access_private_api(client):
    client.cookies.clear()
    bootstrap = client.get("/api/v1/bootstrap").json()
    assert bootstrap["pairing_required"] is True
    assert bootstrap["authenticated"] is False
    for endpoint in ("model-configs", "conversations", "runs", "plugins", "configuration/export"):
        response = client.get(f"/api/v1/{endpoint}")
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "PAIRING_REQUIRED"


def test_cross_origin_and_missing_request_header_cannot_mutate(client):
    response = client.post(
        "/api/v1/conversations", json={"title": "must not exist"}, headers={"Origin": "https://evil.invalid"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ORIGIN_DENIED"
    client.headers.pop("X-Omni-Request")
    response = client.post("/api/v1/conversations", json={"title": "must not exist"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "REQUEST_HEADER_REQUIRED"
    assert client.get("/api/v1/conversations").json() == []


def test_pairing_code_is_one_use_and_logout_revokes_session(environment):
    _, _, factory, client = environment
    code = create_pairing_code(factory)
    client.cookies.clear()
    response = client.post("/api/v1/auth/pair", json={"code": code})
    assert response.status_code == 200
    cookie_header = response.headers["set-cookie"].lower()
    assert "httponly" in cookie_header and "samesite=strict" in cookie_header
    token = client.cookies.get("omniharness_session")
    with factory() as db:
        stored_code = db.get(PairingCode, "local")
        assert stored_code.code_hash == digest(code) and stored_code.code_hash != code
        assert stored_code.consumed is True
        assert db.get(LocalSession, digest(token)) is not None
    replay = client.post("/api/v1/auth/pair", json={"code": code})
    assert replay.status_code == 401
    assert code not in replay.text
    assert client.post("/api/v1/auth/logout").status_code == 204
    client.cookies.set("omniharness_session", token)
    assert client.get("/api/v1/model-configs").status_code == 401


def test_expired_pairing_code_is_rejected(environment):
    _, _, factory, client = environment
    code = create_pairing_code(factory)
    with factory.begin() as db:
        db.get(PairingCode, "local").expires_at = now() - timedelta(seconds=1)
    response = client.post("/api/v1/auth/pair", json={"code": code})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INVALID_PAIRING_CODE"


def test_credential_stays_encrypted_and_update_can_preserve_or_clear_it(environment):
    settings, _, factory, client = environment
    secret = "test-only-provider-secret-never-serialize"
    model, _, queued = create_chat(client, adapter="openai", api_key=secret)
    assert model["key_configured"] is True
    assert secret not in json.dumps(model)
    with factory() as db:
        stored_model = db.get(ModelConfig, model["id"])
        ciphertext = db.get(Provider, stored_model.provider_id).encrypted_api_key
        assert ciphertext != secret
        assert SecretStore(settings).decrypt(ciphertext) == secret
    body = {"name": "Renamed", "adapter_type": "openai", "model_name": "replacement-model"}
    response = client.put(f"/api/v1/model-configs/{model['id']}", json=body)
    assert response.status_code == 200
    assert response.json()["key_configured"] is True
    assert response.json()["version"] == model["version"] + 1
    with factory() as db:
        updated_model = db.get(ModelConfig, model["id"])
        updated_key = db.get(Provider, updated_model.provider_id).encrypted_api_key
        assert SecretStore(settings).decrypt(updated_key) == secret
    for path in (
        "/api/v1/model-configs", "/api/v1/configuration/export", "/api/v1/runs",
        f"/api/v1/runs/{queued['run_id']}",
    ):
        public = client.get(path)
        assert public.status_code == 200
        assert secret not in public.text and ciphertext not in public.text
        assert "encrypted_api_key" not in public.text
    cleared = client.put(f"/api/v1/model-configs/{model['id']}", json={**body, "api_key": ""})
    assert cleared.status_code == 200
    assert cleared.json()["key_configured"] is False


@pytest.mark.parametrize("initial_limit,updated_limit", [(None, 4096), (2048, 100_000)])
def test_output_limits_freeze_per_run_and_updates_preserve_credentials(environment, initial_limit, updated_limit):
    settings, _, factory, client = environment
    secret = "test-only-output-limit-key"
    body = {"name": "Limit test", "adapter_type": "openai", "model_name": "limit-model"}
    if initial_limit is not None:
        body["max_output_tokens"] = initial_limit
    created = client.post("/api/v1/model-configs", json={**body, "api_key": secret})
    assert created.status_code == 201
    model = created.json()
    original_limit = 100_000 if initial_limit is None else initial_limit
    assert model["max_output_tokens"] == original_limit
    conversation = client.post("/api/v1/conversations", json={}).json()
    path = f"/api/v1/conversations/{conversation['id']}/messages"
    request = {"content": "A test task", "model_config_id": model["id"], "client_request_id": "before-limit"}
    before = client.post(path, json=request)
    assert before.status_code == 202
    with factory() as db:
        stored_model = db.get(ModelConfig, model["id"])
        ciphertext = db.get(Provider, stored_model.provider_id).encrypted_api_key
    updated = client.put(f"/api/v1/model-configs/{model['id']}", json={**body, "max_output_tokens": updated_limit})
    assert updated.status_code == 200
    assert updated.json()["max_output_tokens"] == updated_limit
    assert updated.json()["version"] == model["version"] + 1
    assert updated.json()["key_configured"] is True
    after = client.post(path, json={**request, "client_request_id": "after-limit"})
    assert after.status_code == 202
    with factory() as db:
        old_snapshot = db.get(Run, before.json()["run_id"]).config_snapshot
        new_snapshot = db.get(Run, after.json()["run_id"]).config_snapshot
        secrets = SecretStore(settings)
        assert runtime_config(old_snapshot, secrets).max_output_tokens == original_limit
        assert runtime_config(new_snapshot, secrets).max_output_tokens == updated_limit
        # Legacy checkpoints without the field retain their historical default.
        legacy = {key: value for key, value in old_snapshot.items() if key != "max_output_tokens"}
        assert runtime_config(legacy, secrets).max_output_tokens == 2048
        stored_model = db.get(ModelConfig, model["id"])
        assert db.get(Provider, stored_model.provider_id).encrypted_api_key == ciphertext
        assert secrets.decrypt(ciphertext) == secret


def test_model_insert_without_output_limit_uses_new_default(environment):
    _, _, factory, _ = environment
    with factory.begin() as db:
        provider = Provider(adapter_type="mock")
        db.add(provider)
        db.flush()
        model = ModelConfig(provider_id=provider.id, name="Direct insert", model_name="mock")
        db.add(model)
        db.flush()
        assert model.max_output_tokens == 100_000


def test_export_import_creates_separate_configs_without_credentials(environment):
    _, _, factory, client = environment
    model, _, _ = create_chat(client, adapter="anthropic", api_key="test-only-original-key")
    exported = client.get("/api/v1/configuration/export").json()
    assert "api_key" not in exported["model_configs"][0]
    response = client.post("/api/v1/configuration/import", json=exported)
    assert response.status_code == 200 and response.json() == {"imported": 1}
    configs = client.get("/api/v1/model-configs").json()
    assert len(configs) == 2
    original = next(item for item in configs if item["id"] == model["id"])
    imported = next(item for item in configs if item["id"] != model["id"])
    assert original["key_configured"] is True
    assert imported["key_configured"] is False
    with factory() as db:
        original_provider_id = db.get(ModelConfig, original["id"]).provider_id
        imported_provider_id = db.get(ModelConfig, imported["id"]).provider_id
        assert original_provider_id != imported_provider_id
        assert db.get(Provider, imported_provider_id).encrypted_api_key is None
    exported["model_configs"][0]["api_key"] = "test-only-import-secret"
    rejected = client.post("/api/v1/configuration/import", json=exported)
    assert rejected.status_code == 422
    assert "test-only-import-secret" not in rejected.text
    assert len(client.get("/api/v1/model-configs").json()) == 2


def test_validation_errors_do_not_echo_submitted_credentials(client):
    secret = "test-only-key-inside-invalid-url"
    response = client.post("/api/v1/model-configs", json={
        "name": "Invalid", "adapter_type": "openai-compatible", "model_name": "test",
        "base_url": f"https://user:{secret}@example.invalid/v1", "api_key": secret,
    })
    assert response.status_code == 422
    assert secret not in response.text
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_duplicate_request_reuses_run_but_conflicting_content_is_rejected(environment):
    _, _, factory, client = environment
    model, conversation, queued = create_chat(client)
    path = f"/api/v1/conversations/{conversation['id']}/messages"
    body = {"content": "Explain a checkpoint", "model_config_id": model["id"], "client_request_id": "request-1"}
    duplicate = client.post(path, json=body)
    assert duplicate.status_code == 202 and duplicate.json() == queued
    conflict = client.post(path, json={**body, "content": "Different question"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "REQUEST_ID_CONFLICT"
    assert len(client.get(path).json()) == 2
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1


def test_missing_live_key_does_not_enqueue_mock_fallback(client):
    model = client.post("/api/v1/model-configs", json={
        "name": "No key", "adapter_type": "openai", "model_name": "test",
    }).json()
    conversation = client.post("/api/v1/conversations", json={}).json()
    path = f"/api/v1/conversations/{conversation['id']}/messages"
    response = client.post(path, json={
        "content": "Run for real", "model_config_id": model["id"], "client_request_id": "no-key",
    })
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MISSING_API_KEY"
    assert client.get("/api/v1/runs").json() == []
    assert client.get(path).json() == []


def test_queued_cancel_is_idempotent_and_sse_replays_from_cursor(client):
    _, _, queued = create_chat(client)
    path = f"/api/v1/runs/{queued['run_id']}"
    assert client.post(f"{path}/cancel").json()["status"] == "cancelled"
    assert client.post(f"{path}/cancel").json()["status"] == "cancelled"
    all_events = sse_events(client.get(f"{path}/events"))
    assert [event["event_type"] for event in all_events] == ["run.created", "run.cancelled"]
    assert [event["sequence_number"] for event in all_events] == [1, 2]
    assert all(event["payload"]["mode"] == "mock" for event in all_events)
    resumed = sse_events(client.get(f"{path}/events?after=0", headers={"Last-Event-ID": "1"}))
    assert resumed == all_events[1:]
    assert sse_events(client.get(f"{path}/events?after=2")) == []


@pytest.mark.parametrize("suffix,headers,status", [
    ("?after=999", {}, 409),
    ("", {"Last-Event-ID": "not-an-integer"}, 400),
    ("?after=-1", {}, 409),
    ("", {"Last-Event-ID": "-1"}, 409),
    ("?after=-1", {"Last-Event-ID": "1"}, 409),
])
def test_invalid_sse_cursor_fails_before_streaming(client, suffix, headers, status):
    _, _, queued = create_chat(client)
    path = f"/api/v1/runs/{queued['run_id']}"
    client.post(f"{path}/cancel")
    response = client.get(f"{path}/events{suffix}", headers=headers)
    assert response.status_code == status
    assert response.json()["detail"]["code"] == "INVALID_CURSOR"
