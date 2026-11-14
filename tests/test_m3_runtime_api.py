"""Product API boundaries while the persistent Agent runtime is not wired."""

from conftest import create_chat
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from omniharness.api import create_app
from omniharness.models import AgentRun, Message, Run, RunEvent


def test_agent_request_fails_closed_without_creating_a_run_or_messages(environment):
    settings, engine, factory, client = environment
    model, conversation, _ = create_chat(client)
    with factory() as db:
        before = tuple(db.scalar(select(func.count()).select_from(table)) for table in (Run, Message, RunEvent))
    path = f"/api/v1/conversations/{conversation['id']}/messages"
    payload = {
        "content": "Do actual agent work", "model_config_id": model["id"],
        "client_request_id": "agent-unavailable", "workflow_type": "agent",
    }
    anonymous = TestClient(create_app(settings, engine))
    assert anonymous.post(path, json=payload, headers={"X-Omni-Request": "1"}).status_code == 401
    assert client.post(path, json=payload, headers={"X-Omni-Request": "0"}).status_code == 403
    assert client.post(path, json=payload, headers={"Origin": "https://not-allowed.test"}).status_code == 403
    response = client.post(path, json=payload)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "AGENT_RUNTIME_UNAVAILABLE"
    with factory() as db:
        after = tuple(db.scalar(select(func.count()).select_from(table)) for table in (Run, Message, RunEvent))
    assert after == before


def test_explicit_chat_is_backward_compatible_and_unknown_workflow_is_rejected(environment):
    _, _, _, client = environment
    model, conversation, _ = create_chat(client)
    path = f"/api/v1/conversations/{conversation['id']}/messages"
    body = {"content": "continue", "model_config_id": model["id"], "client_request_id": "next"}
    result = client.post(path, json={**body, "workflow_type": "chat"})
    assert result.status_code == 202
    assert client.post(path, json={**body, "workflow_type": "unknown"}).status_code == 422


def test_run_snapshot_workflow_type_is_read_only_and_defaults_for_legacy(environment):
    _, _, factory, client = environment
    _, _, created = create_chat(client)
    run_id = created["run_id"]
    path = f"/api/v1/runs/{run_id}"
    with factory.begin() as db:
        run = db.get(Run, run_id)
        run.config_snapshot = {"model_name": "legacy", "secret_marker": "must-not-appear"}
    legacy = client.get(path)
    assert legacy.status_code == 200
    assert legacy.json()["workflow_type"] == "chat"
    assert "secret_marker" not in legacy.text
    with factory.begin() as db:
        db.get(Run, run_id).config_snapshot = {"workflow_type": "agent", "secret_marker": "must-not-appear"}
    agent = client.get(path)
    assert agent.json()["workflow_type"] == "agent"
    assert "secret_marker" not in agent.text


def test_agent_runs_only_return_real_nodes_for_this_run_and_require_auth(environment):
    settings, engine, factory, client = environment
    model, conversation, first = create_chat(client)
    second = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={
        "content": "another", "model_config_id": model["id"], "client_request_id": "second",
    }).json()
    path = f"/api/v1/runs/{first['run_id']}/agent-runs"
    assert client.get(path).json() == []
    with factory.begin() as db:
        parent = AgentRun(run_id=first["run_id"], role="coordinator", status="running", depth=0)
        db.add(parent)
        db.flush()
        db.add(AgentRun(run_id=first["run_id"], role="researcher", status="completed", depth=1,
                        parent_agent_run_id=parent.id, model_config_id=model["id"]))
        db.add(AgentRun(run_id=second["run_id"], role="other", status="queued", depth=0))
    response = client.get(path)
    assert response.status_code == 200
    nodes = response.json()
    assert {node["role"] for node in nodes} == {"coordinator", "researcher"}
    assert all(node["run_id"] == first["run_id"] for node in nodes)
    assert all(node["node_kind"] == "model_call" for node in nodes)
    assert next(node for node in nodes if node["role"] == "researcher")["parent_agent_run_id"] == parent.id
    assert client.get("/api/v1/runs/missing/agent-runs").status_code == 404
    anonymous = TestClient(create_app(settings, engine))
    assert anonymous.get(path).status_code == 401
    assert "api_key" not in response.text.lower()
