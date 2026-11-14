import pytest
from fastapi.testclient import TestClient

from omniharness.api import create_app
from omniharness.cli import migrate
from omniharness.db import make_engine, make_session_factory
from omniharness.security import create_pairing_code, init_secrets
from omniharness.settings import Settings


@pytest.fixture
def environment(tmp_path):
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        allow_sqlite=True, data_dir=tmp_path / "data", secret_dir=tmp_path / "secrets",
        static_dir=tmp_path / "web", poll_seconds=0.01,
        allowed_origins="http://testserver,http://127.0.0.1:8787",
    )
    init_secrets(settings)
    migrate(settings)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    app = create_app(settings, engine)
    with TestClient(app, headers={"X-Omni-Request": "1"}) as client:
        code = create_pairing_code(factory)
        assert client.post("/api/v1/auth/pair", json={"code": code}).status_code == 200
        yield settings, engine, factory, client
    engine.dispose()


@pytest.fixture
def client(environment):
    return environment[3]


def create_chat(client, adapter="mock", api_key=None):
    body = {"name": "Test model", "adapter_type": adapter, "model_name": "demo"}
    if api_key is not None:
        body["api_key"] = api_key
    response = client.post("/api/v1/model-configs", json=body)
    assert response.status_code == 201, response.text
    model = response.json()
    conversation = client.post("/api/v1/conversations", json={}).json()
    run = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={
        "content": "Explain a checkpoint", "model_config_id": model["id"], "client_request_id": "request-1",
    })
    assert run.status_code == 202, run.text
    return model, conversation, run.json()
