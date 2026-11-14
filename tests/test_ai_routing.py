"""Explicit routing requests are bounded, versioned and never reveal credentials."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from omniharness.models import ModelConfig, Run
from omniharness.schemas import SendMessageRequest, StageOverride
from omniharness.services import ServiceError, enqueue, freeze_stage_run_config
from omniharness.stage_contract import STAGE_KEYS

SECRET = "fake-planner-secret-not-a-real-credential"


def add_model(client, model_name="model-a", key=SECRET):
    result = client.post("/api/v1/model-configs", json={
        "name": model_name, "model_name": model_name, "adapter_type": "openai-compatible",
        "base_url": "https://example.test/v1", "api_key": key,
    })
    assert result.status_code == 201
    return result.json()


def payload(model):
    return {"task": "处理我的问题", "planner_model_config_id": model["id"],
            "planner_model_version": model["version"], "planner_provider_version": model["provider_version"],
            "candidate_model_config_ids": [model["id"]]}


def answer(model_id, *, reason="基于现有信息的建议"):
    return AIMessage(content=json.dumps({"stages": [{"stage": stage, "model_config_id": model_id,
                                                    "reason": reason} for stage in STAGE_KEYS]}, ensure_ascii=False))


def test_preview_then_explicit_one_call_returns_verified_candidate_and_unknown_cost(environment, monkeypatch):
    _, _, _, client = environment
    model = add_model(client)
    preview = client.get("/api/v1/model-routing/ai-plan/preview", params={"planner_model_config_id": model["id"]})
    assert preview.status_code == 200
    assert preview.json()["planner"]["endpoint"] == "https://example.test/v1/chat/completions"
    assert SECRET not in preview.text
    calls = []

    async def fake(config, messages):
        calls.append((config.model_name, messages))
        assert SECRET not in " ".join(str(message.content) for message in messages)
        return answer(model["id"])

    monkeypatch.setattr("omniharness.ai_routing.invoke_planner", fake)
    result = client.post("/api/v1/model-routing/ai-plan", json=payload(model))
    assert result.status_code == 200, result.text
    assert len(calls) == 1
    assert [item["stage"] for item in result.json()["stages"]] == list(STAGE_KEYS)
    assert result.json()["usage"]["cost"] is None
    assert SECRET not in result.text


@pytest.mark.parametrize("kind", ["foreign_model", "duplicate_stage", "secret", "invalid_json"])
def test_invalid_or_secret_containing_response_is_rejected(environment, monkeypatch, kind):
    _, _, _, client = environment
    model = add_model(client)

    async def fake(config, messages):
        if kind == "foreign_model":
            return answer("not-an-authorized-model")
        if kind == "secret":
            return answer(model["id"], reason=SECRET)
        if kind == "duplicate_stage":
            return AIMessage(content=json.dumps({"stages": [{"stage": "think", "model_config_id": model["id"],
                                                             "reason": "r"}] * 5}))
        return AIMessage(content="not json")

    monkeypatch.setattr("omniharness.ai_routing.invoke_planner", fake)
    result = client.post("/api/v1/model-routing/ai-plan", json=payload(model))
    assert result.status_code == 422 and result.json()["detail"]["code"] == "PLANNER_OUTPUT_INVALID"
    assert SECRET not in result.text


@pytest.mark.parametrize("valid_content", [True, False])
def test_truncated_planner_result_is_not_accepted_or_retried(environment, monkeypatch, valid_content):
    _, _, _, client = environment
    model = add_model(client)
    calls = []

    async def fake(config, messages):
        calls.append(True)
        content = answer(model["id"]).content if valid_content else '{"stages":['
        return AIMessage(content=content, response_metadata={"finish_reason": "length"})

    monkeypatch.setattr("omniharness.ai_routing.invoke_planner", fake)
    result = client.post("/api/v1/model-routing/ai-plan", json=payload(model))
    assert result.status_code == 422 and result.json()["detail"]["code"] == "PLANNER_OUTPUT_TRUNCATED"
    assert "调整输出上限" in result.json()["detail"]["message"]
    assert "2048" not in result.json()["detail"]["message"]
    assert len(calls) == 1


def test_version_change_before_call_is_rejected_and_no_model_is_invoked(environment, monkeypatch):
    _, _, factory, client = environment
    model = add_model(client)
    with factory.begin() as db:
        db.get(ModelConfig, model["id"]).version += 1

    async def never(*args):
        pytest.fail("request went to an unconfirmed endpoint")

    monkeypatch.setattr("omniharness.ai_routing.invoke_planner", never)
    response = client.post("/api/v1/model-routing/ai-plan", json=payload(model))
    assert response.status_code == 409 and response.json()["detail"]["code"] == "MODEL_CONFIG_CHANGED"


def test_config_change_during_call_invalidates_suggestion(environment, monkeypatch):
    _, _, factory, client = environment
    model = add_model(client)

    async def fake(config, messages):
        with factory.begin() as db:
            db.get(ModelConfig, model["id"]).version += 1
        return answer(model["id"])

    monkeypatch.setattr("omniharness.ai_routing.invoke_planner", fake)
    response = client.post("/api/v1/model-routing/ai-plan", json=payload(model))
    assert response.status_code == 409 and response.json()["detail"]["code"] == "MODEL_CONFIG_CHANGED"


def test_capability_change_during_call_invalidates_suggestion(environment, monkeypatch):
    _, _, factory, client = environment
    model = add_model(client)

    async def fake(config, messages):
        with factory.begin() as db:
            db.get(ModelConfig, model["id"]).capabilities = {"tools": {"status": "supported"}}
        return answer(model["id"])

    monkeypatch.setattr("omniharness.ai_routing.invoke_planner", fake)
    response = client.post("/api/v1/model-routing/ai-plan", json=payload(model))
    assert response.status_code == 409 and response.json()["detail"]["code"] == "MODEL_CONFIG_CHANGED"


def test_provider_error_and_user_input_with_key_are_sanitized(environment, monkeypatch):
    _, _, _, client = environment
    model = add_model(client)
    request = payload(model)
    request["task"] = "key " + SECRET
    assert client.post("/api/v1/model-routing/ai-plan", json=request).json()["detail"]["code"] == "PLANNER_INPUT_CONTAINS_KEY"

    async def fake(config, messages):
        raise RuntimeError(SECRET)

    monkeypatch.setattr("omniharness.ai_routing.invoke_planner", fake)
    response = client.post("/api/v1/model-routing/ai-plan", json=payload(model))
    assert response.status_code == 422 and SECRET not in response.text


def test_other_candidate_credential_is_never_forwarded_or_returned(environment, monkeypatch):
    _, _, _, client = environment
    planner = add_model(client)
    other_secret = "different-candidate-fake-secret"
    other = add_model(client, "other-model", key=other_secret)
    request = payload(planner)
    request["candidate_model_config_ids"].append(other["id"])
    request["task"] = other_secret
    response = client.post("/api/v1/model-routing/ai-plan", json=request)
    assert response.status_code == 422 and other_secret not in response.text
    request["task"] = "safe task"

    async def fake(config, messages):
        return answer(planner["id"], reason=other_secret)

    monkeypatch.setattr("omniharness.ai_routing.invoke_planner", fake)
    response = client.post("/api/v1/model-routing/ai-plan", json=request)
    assert response.status_code == 422 and other_secret not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("output_limit", [100_000, 4096])
async def test_planner_uses_configured_limit_without_hidden_cap(monkeypatch, output_limit):
    from omniharness.ai_routing import invoke_planner
    from omniharness.providers import RuntimeModelConfig

    config = RuntimeModelConfig(id="limit-test", adapter_type="openai-compatible", model_name="test",
                                api_key=SECRET, base_url="http://127.0.0.1:12345/v1",
                                max_output_tokens=output_limit)
    messages = [HumanMessage(content="test")]
    expected = answer("limit-test")
    built_limits, invoked = [], []

    class Planner:
        async def ainvoke(self, actual_messages):
            invoked.append(actual_messages)
            return expected

    def build(actual_config, **kwargs):
        built_limits.append(actual_config.max_output_tokens)
        assert kwargs["http_async_client"].follow_redirects is False
        return Planner()

    monkeypatch.setattr("omniharness.ai_routing.build_chat_model", build)
    assert await invoke_planner(config, messages) is expected
    assert built_limits == [output_limit]
    assert invoked == [messages]


@pytest.mark.asyncio
async def test_planner_http_client_does_not_follow_redirects_or_retry():
    from omniharness.ai_routing import invoke_planner
    from omniharness.providers import RuntimeModelConfig

    calls = []

    class RedirectServer(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/capture")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), RedirectServer)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = RuntimeModelConfig(id="test", adapter_type="openai-compatible", model_name="test",
                                    api_key=SECRET, base_url=f"http://127.0.0.1:{server.server_port}/v1")
        with pytest.raises(Exception):
            await invoke_planner(config, [HumanMessage(content="test")])
        assert calls == ["/v1/chat/completions"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_run_stage_overrides_freeze_model_versions_without_changing_global(environment):
    settings, _, factory, client = environment
    default, second = add_model(client), add_model(client, "model-b")
    choices = [StageOverride(stage=stage, model_config_id=second["id"] if stage == "review" else default["id"],
                             model_version=1, provider_version=1) for stage in STAGE_KEYS]
    conversation = client.post("/api/v1/conversations", json={}).json()
    with factory.begin() as db:
        result = enqueue(db, conversation["id"], SendMessageRequest(
            content="task", model_config_id=default["id"], client_request_id="unit"))
        run = db.get(Run, result["run_id"])
        run.config_snapshot = {**run.config_snapshot, "workflow_type": "agent"}
        freeze_stage_run_config(db, run, settings, stage_overrides=choices)
    with factory.begin() as db:
        db.get(ModelConfig, second["id"]).version += 1
    with factory() as db:
        run = db.get(Run, result["run_id"])
        assert run.config_snapshot["agent"]["roles"]["review"]["model"]["id"] == second["id"]
        assert run.config_snapshot["agent"]["roles"]["review"]["model"]["version"] == 1
        assert run.config_snapshot["agent"]["roles"]["review"]["source"] == "task_override"
    assert all(item["model_config_id"] is None for item in client.get("/api/v1/stage-bindings").json()["bindings"])
    projection = client.get(f"/api/v1/runs/{result['run_id']}").json()["stage_plan"]
    assert next(item for item in projection if item["stage"] == "review")["model_name"] == "model-b"
    assert SECRET not in json.dumps(projection)


def test_run_rejects_stale_override_and_chat_override(environment):
    settings, _, factory, client = environment
    model = add_model(client)
    choices = [StageOverride(stage=stage, model_config_id=model["id"], model_version=999, provider_version=1)
               for stage in STAGE_KEYS]
    with pytest.raises(ValueError):
        SendMessageRequest(content="task", model_config_id=model["id"], client_request_id="unit",
                           workflow_type="chat", stage_overrides=choices)
    conversation = client.post("/api/v1/conversations", json={}).json()
    with factory.begin() as db:
        result = enqueue(db, conversation["id"], SendMessageRequest(
            content="task", model_config_id=model["id"], client_request_id="unit"))
        run = db.get(Run, result["run_id"])
        run.config_snapshot = {**run.config_snapshot, "workflow_type": "agent"}
        with pytest.raises(ServiceError) as error:
            freeze_stage_run_config(db, run, settings, stage_overrides=choices)
        assert error.value.code == "MODEL_CONFIG_CHANGED"
