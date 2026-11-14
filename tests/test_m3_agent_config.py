"""Agent configuration freeze contract; it is not a product execution path."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update
from test_postgres import postgres_environment  # noqa: F401 - opt-in dedicated PG fixture

from omniharness.api import create_app
from omniharness.models import Message, ModelConfig, Run, RunBudget, RunEvent
from omniharness.schemas import SendMessageRequest
from omniharness.security import create_pairing_code
from omniharness.services import (
    ROLES,
    ServiceError,
    enqueue,
    freeze_agent_run_config,
    validate_agent_run_versions,
)

SECRET = "test-agent-secret-not-a-real-key"
ROLE_VERSIONS = {name: f"builtin-{name}-1" for name in ROLES}


def model(client, name, *, url="https://provider.example/v1", key=SECRET):
    body = {"name": name, "adapter_type": "openai-compatible", "model_name": "model-1", "base_url": url}
    if key is not None:
        body["api_key"] = key
    result = client.post("/api/v1/model-configs", json=body)
    assert result.status_code == 201
    assert SECRET not in result.text
    return result.json()


def mark_tools(factory, model_id, status="supported"):
    with factory.begin() as db:
        db.get(ModelConfig, model_id).capabilities = {"tools": {"status": status, "message": "test probe"}}


def enqueue_and_freeze(db, conversation_id, model_id, settings, *, request_id="agent-test", plugins=None):
    result = enqueue(db, conversation_id, SendMessageRequest(
        content="agent task", model_config_id=model_id, client_request_id=request_id,
    ))
    run = db.get(Run, result["run_id"])
    run.config_snapshot = {**run.config_snapshot, "workflow_type": "agent"}
    freeze_agent_run_config(db, run, settings, "agent-v1", role_versions=ROLE_VERSIONS,
                            plugin_versions=plugins if plugins is not None else {})
    return run.id


def test_single_transaction_freezes_roles_models_encrypted_credentials_and_budget(environment):
    settings, _, factory, client = environment
    default = model(client, "default")
    alternate = model(client, "alternate", url="http://localhost:11434/v1", key=None)
    for item in (default, alternate):
        mark_tools(factory, item["id"])
    with factory.begin() as db:
        db.get(ModelConfig, default["id"]).capabilities = {
            "tools": {"status": "supported", "message": SECRET},
        }
    assert client.put("/api/v1/orchestration/bindings", json={"bindings": [
        {"role": "review", "model_config_id": alternate["id"]},
    ]}).status_code == 200
    conversation = client.post("/api/v1/conversations", json={}).json()
    plugins = {"example.tool": "1.0"}
    with factory.begin() as db:
        run_id = enqueue_and_freeze(db, conversation["id"], default["id"], settings, plugins=plugins)
    plugins["example.tool"] = "2.0"
    with factory() as db:
        run = db.get(Run, run_id)
        snapshot = run.config_snapshot
        assert run.thread_id == run_id and run.workflow_version == "agent-v1"
        agent = snapshot["agent"]
        assert agent["role_binding_version"] == 1
        assert agent["roles"]["think"]["source"] == "default"
        assert agent["roles"]["think"]["model"]["id"] == default["id"]
        assert agent["roles"]["review"]["source"] == "override"
        assert agent["roles"]["review"]["model"]["id"] == alternate["id"]
        assert agent["plugin_versions"] == {"example.tool": "1.0"}
        assert db.get(RunBudget, run_id).max_model_calls == agent["budget"]["max_model_calls"] == 50
        assert SECRET not in json.dumps(snapshot)
        assert snapshot["encrypted_api_key"] and snapshot["encrypted_api_key"] != SECRET
    assert client.put("/api/v1/orchestration/bindings", json={"bindings": []}).status_code == 200
    assert client.put(f"/api/v1/model-configs/{default['id']}", json={
        "name": "changed", "adapter_type": "openai-compatible", "model_name": "model-2",
        "base_url": "https://provider.example/v1", "api_key": "new-key",
    }).status_code == 200
    settings.budget_max_model_calls = 3
    with factory() as db:
        run = db.get(Run, run_id)
        assert run.config_snapshot["model_name"] == "model-1"
        assert run.config_snapshot["agent"]["roles"]["review"]["model"]["id"] == alternate["id"]
        assert db.get(RunBudget, run_id).max_model_calls == 50
        validate_agent_run_versions(run, "agent-v1", role_versions=ROLE_VERSIONS,
                                    plugin_versions={"example.tool": "1.0"})
        for version, roles, plugins in [
            ("agent-v2", ROLE_VERSIONS, {"example.tool": "1.0"}),
            ("agent-v1", {**ROLE_VERSIONS, "review": "new"}, {"example.tool": "1.0"}),
            ("agent-v1", ROLE_VERSIONS, {"example.tool": "2.0"}),
        ]:
            with pytest.raises(ServiceError) as exc:
                validate_agent_run_versions(run, version, role_versions=roles, plugin_versions=plugins)
            assert exc.value.code == "AGENT_CONFIG_VERSION_MISMATCH" and SECRET not in str(exc.value)
    response = client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200 and SECRET not in response.text
    assert "encrypted_api_key" not in response.text


@pytest.mark.parametrize("status,expected", [
    (None, "MODEL_TOOL_CAPABILITY_UNKNOWN"), ("unsupported", "MODEL_TOOL_CALLING_UNSUPPORTED"),
])
def test_missing_or_unsupported_tool_capability_rejects_and_rolls_back(environment, status, expected):
    settings, _, factory, client = environment
    default = model(client, "no-tools")
    if status:
        mark_tools(factory, default["id"], status)
    conversation = client.post("/api/v1/conversations", json={}).json()
    with pytest.raises(ServiceError) as exc:
        with factory.begin() as db:
            enqueue_and_freeze(db, conversation["id"], default["id"], settings)
    assert exc.value.code == expected
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 0
        assert db.scalar(select(func.count()).select_from(RunBudget)) == 0


def test_missing_remote_credential_is_rejected_but_local_keyless_is_allowed(environment):
    settings, _, factory, client = environment
    remote = model(client, "remote-keyless", key=None)
    local = model(client, "local-keyless", url="http://127.0.0.1:11434/v1", key=None)
    mark_tools(factory, remote["id"])
    mark_tools(factory, local["id"])
    conversation = client.post("/api/v1/conversations", json={}).json()
    with pytest.raises(ServiceError) as exc:
        with factory.begin() as db:
            enqueue_and_freeze(db, conversation["id"], remote["id"], settings)
    assert exc.value.code == "MISSING_API_KEY"
    with factory.begin() as db:
        run_id = enqueue_and_freeze(db, conversation["id"], local["id"], settings, request_id="local")
    with factory() as db:
        assert db.get(Run, run_id).config_snapshot["encrypted_api_key"] is None


def test_disabled_override_and_double_freeze_rejected(environment):
    settings, _, factory, client = environment
    default, other = model(client, "default"), model(client, "disabled")
    mark_tools(factory, default["id"])
    mark_tools(factory, other["id"])
    assert client.put("/api/v1/orchestration/bindings", json={"bindings": [
        {"role": "review", "model_config_id": other["id"]},
    ]}).status_code == 200
    assert client.delete(f"/api/v1/model-configs/{other['id']}").status_code == 204
    conversation = client.post("/api/v1/conversations", json={}).json()
    with pytest.raises(ServiceError) as exc:
        with factory.begin() as db:
            enqueue_and_freeze(db, conversation["id"], default["id"], settings)
    assert exc.value.code == "MODEL_DISABLED"
    assert client.put("/api/v1/orchestration/bindings", json={"bindings": []}).status_code == 200
    with factory.begin() as db:
        run_id = enqueue_and_freeze(db, conversation["id"], default["id"], settings, request_id="fresh")
    with pytest.raises(ServiceError) as exc:
        with factory.begin() as db:
            freeze_agent_run_config(db, db.get(Run, run_id), settings, "agent-v1", role_versions=ROLE_VERSIONS,
                                    plugin_versions={})
    assert exc.value.code == "AGENT_SNAPSHOT_INVALID_STATE"


@pytest.mark.parametrize("unused_missing_key", [False, True], ids=["disabled", "missing-key"])
def test_unused_verifier_model_does_not_block_agent(environment, unused_missing_key):
    settings, _, factory, client = environment
    default = model(client, "default")
    unused = model(client, "unused-verifier", key=None if unused_missing_key else SECRET)
    mark_tools(factory, default["id"])
    assert client.put("/api/v1/orchestration/bindings", json={"bindings": [
        {"role": "verifier", "model_config_id": unused["id"]},
    ]}).status_code == 200
    if not unused_missing_key:
        assert client.delete(f"/api/v1/model-configs/{unused['id']}").status_code == 204
    conversation = client.post("/api/v1/conversations", json={}).json()
    with factory.begin() as db:
        run_id = enqueue_and_freeze(db, conversation["id"], default["id"], settings)
    with factory() as db:
        roles = db.get(Run, run_id).config_snapshot["agent"]["roles"]
        assert roles["verifier"] == {
            "role_version": ROLE_VERSIONS["verifier"], "source": "non_model", "model": None,
        }
        assert all(roles[role]["model"]["id"] == default["id"]
                   for role in ("think", "plan", "execute", "review"))
        # Historical pre-fix snapshots contain an unused verifier model; the
        # version check must accept them without changing persisted history.
        roles["verifier"] = {"role_version": ROLE_VERSIONS["verifier"],
                             "source": "override", "model": {"id": unused["id"]}}
        validate_agent_run_versions(db.get(Run, run_id), "agent-v1", role_versions=ROLE_VERSIONS,
                                    plugin_versions={})


def test_creation_and_freeze_rollback_together(environment):
    settings, _, factory, client = environment
    default = model(client, "rollback")
    mark_tools(factory, default["id"])
    conversation = client.post("/api/v1/conversations", json={}).json()
    with pytest.raises(RuntimeError, match="abort"):
        with factory.begin() as db:
            enqueue_and_freeze(db, conversation["id"], default["id"], settings)
            raise RuntimeError("abort")
    with factory() as db:
        assert all(db.scalar(select(func.count()).select_from(table)) == 0
                   for table in (Run, RunBudget, Message, RunEvent))


def test_normal_chat_run_cannot_be_repurposed_into_agent(environment):
    settings, _, factory, client = environment
    default = model(client, "chat")
    mark_tools(factory, default["id"])
    conversation = client.post("/api/v1/conversations", json={}).json()
    response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={
        "content": "chat", "model_config_id": default["id"], "client_request_id": "chat-run",
    })
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    with pytest.raises(ServiceError) as exc:
        with factory.begin() as db:
            freeze_agent_run_config(db, db.get(Run, run_id), settings, "agent-v1",
                                    role_versions=ROLE_VERSIONS, plugin_versions={})
    assert exc.value.code == "AGENT_SNAPSHOT_INVALID_STATE"
    with factory() as db:
        run = db.get(Run, run_id)
        assert run.thread_id is None and run.workflow_version is None
        assert db.get(RunBudget, run_id) is None


def test_postgres_single_statement_snapshot_does_not_mix_concurrent_config_versions(request):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        default = model(client, "pg-default")
        alternate = model(client, "pg-alternate", url="http://localhost:11434/v1", key=None)
        mark_tools(factory, default["id"])
        mark_tools(factory, alternate["id"])
        assert client.put("/api/v1/orchestration/bindings", json={"bindings": [
            {"role": "review", "model_config_id": alternate["id"]},
        ]}).status_code == 200
        conversation = client.post("/api/v1/conversations", json={}).json()
        changed = []

        def update_configuration():
            from omniharness.models import RoleBinding

            with factory.begin() as db:
                db.execute(update(ModelConfig).where(ModelConfig.id == default["id"]).values(model_name="new-model"))
                db.execute(update(RoleBinding).where(RoleBinding.role == "review")
                           .values(model_config_id=None, enabled=False, version=2))

        def after_execute(_conn, _cursor, statement, _parameters, _context, _executemany):
            normalized = statement.lower()
            if not changed and "join role_bindings" in normalized and "model_configs" in normalized:
                changed.append(True)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(update_configuration).result(timeout=10)

        event.listen(engine, "after_cursor_execute", after_execute)
        try:
            with factory.begin() as db:
                run_id = enqueue_and_freeze(db, conversation["id"], default["id"], settings)
        finally:
            event.remove(engine, "after_cursor_execute", after_execute)
        assert changed == [True]
        with factory() as db:
            snapshot = db.get(Run, run_id).config_snapshot
            assert snapshot["model_name"] == "model-1"
            assert snapshot["agent"]["role_binding_version"] == 1
            assert snapshot["agent"]["roles"]["review"]["model"]["id"] == alternate["id"]
            assert db.get(ModelConfig, default["id"]).model_name == "new-model"
