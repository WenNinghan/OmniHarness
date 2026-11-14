"""Independent PostgreSQL integration review; all model replies are synthetic."""

import asyncio
import json
import os
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from test_postgres import postgres_environment  # noqa: F401

import omniharness.ai_routing as routing
from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema
from omniharness.models import Run
from omniharness.security import create_pairing_code
from omniharness.stage_contract import STAGE_KEYS

SECRET = "synthetic-review-secret-do-not-use"


def test_planning_to_independent_frozen_runs(request, monkeypatch):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    settings.enable_agent = True
    settings.checkpoint_schema = f"routing_e2e_{uuid4().hex}"
    raw = os.environ["OMNI_TEST_POSTGRES_URL"]
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
        runner.run(initialize_checkpoint_schema(raw, schema=settings.checkpoint_schema))
    calls = []
    model_ids = []

    async def fake_planner(config, messages):
        assert config.base_url == "http://127.0.0.1:11434/v1"
        assert config.api_key == SECRET
        assert SECRET not in str(messages)
        calls.append(len(messages))
        return AIMessage(content=json.dumps({"stages": [
            {"stage": stage, "model_config_id": model_ids[index % 2], "reason": "synthetic recommendation"}
            for index, stage in enumerate(STAGE_KEYS)
        ]}))

    monkeypatch.setattr(routing, "invoke_planner", fake_planner)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        for name in ("review-strong", "review-cheap"):
            response = client.post("/api/v1/model-configs", json={
                "name": name, "adapter_type": "openai-compatible", "model_name": name,
                "base_url": "http://127.0.0.1:11434/v1", "api_key": SECRET})
            assert response.status_code == 201 and SECRET not in response.text
            model_ids.append(response.json()["id"])
        models = {item["id"]: item for item in client.get("/api/v1/model-configs").json()}
        stage_before = client.get("/api/v1/stage-bindings").json()
        response = client.get("/api/v1/model-routing/ai-plan/preview", params={"planner_model_config_id": model_ids[0]})
        assert response.status_code == 200 and not calls
        preview = response.json()["planner"]
        assert preview["endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
        response = client.post("/api/v1/model-routing/ai-plan", json={"task": "计算 6 乘 7",
            "planner_model_config_id": model_ids[0], "planner_model_version": preview["model_version"],
            "planner_provider_version": preview["provider_version"], "candidate_model_config_ids": model_ids})
        assert response.status_code == 200 and SECRET not in response.text
        assert len(calls) == 1
        result = response.json()
        assert result["usage"]["cost"] is None
        overrides = [{key: stage[key] for key in ("stage", "model_config_id", "model_version", "provider_version")}
                     for stage in result["stages"]]
        # Simulate the user's manual adjustment before submitting the plan.
        review = next(item for item in overrides if item["stage"] == "review")
        selected = models[model_ids[1]]
        review.update(model_config_id=selected["id"], model_version=selected["version"],
                      provider_version=selected["provider_version"])
        conversations = [client.post("/api/v1/conversations", json={}).json()["id"] for _ in range(2)]
        request = {"content": "计算 6 乘 7", "model_config_id": model_ids[0],
                   "client_request_id": "stable-request", "workflow_type": "agent", "stage_overrides": overrides}
        first = client.post(f"/api/v1/conversations/{conversations[0]}/messages", json=request)
        assert first.status_code == 202 and SECRET not in first.text
        repeated = client.post(f"/api/v1/conversations/{conversations[0]}/messages", json=request)
        assert repeated.status_code == 202 and repeated.json()["run_id"] == first.json()["run_id"]
        changed = dict(request, stage_overrides=[dict(item) for item in overrides])
        changed["stage_overrides"][0]["model_config_id"] = model_ids[1]
        assert client.post(f"/api/v1/conversations/{conversations[0]}/messages", json=changed).status_code == 409
        other = client.post(f"/api/v1/conversations/{conversations[1]}/messages", json={
            "content": "另一个会话", "model_config_id": model_ids[0], "client_request_id": "other",
            "workflow_type": "agent"})
        assert other.status_code == 202
        assert len(calls) == 1  # Run submission and other conversations never replan.
        assert client.get("/api/v1/stage-bindings").json() == stage_before
        with factory() as db:
            first_run = db.get(Run, first.json()["run_id"])
            other_run = db.get(Run, other.json()["run_id"])
            first_roles = first_run.config_snapshot["agent"]["roles"]
            other_roles = other_run.config_snapshot["agent"]["roles"]
            assert first_roles["review"]["model"]["id"] == model_ids[1]
            assert all(role["source"] == "task_override" for role in first_roles.values())
            assert all(role["model"]["id"] == model_ids[0] for role in other_roles.values())
            assert all(role["source"] == "default" for role in other_roles.values())
