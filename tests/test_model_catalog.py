import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from omniharness.model_catalog import create_catalog_router
from omniharness.models import ModelConfig, Provider
from omniharness.services import ServiceError


@pytest.fixture
def catalog_env(environment, monkeypatch):
    settings, _, factory, client = environment
    source = client.post("/api/v1/model-configs", json={
        "name": "Source", "adapter_type": "openai-compatible", "model_name": "alpha",
        "base_url": "https://example.invalid/v1", "api_key": "test-catalog-secret-value",
    }).json()
    app = FastAPI()

    @app.exception_handler(ServiceError)
    async def service_error(_, exc):
        return JSONResponse({"detail": {"code": exc.code, "message": exc.message}}, exc.status)

    app.include_router(create_catalog_router(factory, client.app.state.secret_store))
    from omniharness import model_catalog
    original = httpx.AsyncClient
    responses = {"status": 200, "data": {"data": [
        {"id": "alpha"}, {"id": "beta"}, {"id": "beta"}, {"id": "bad ? model"},
        {"id": "test-catalog-secret-value"}, {"id": "Gamma-3"},
    ]}}
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(responses["status"], json=responses["data"], headers=responses.get("headers"))

    def fake_client(*args, **kwargs):
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", fake_client)
    with TestClient(app) as route_client:
        yield factory, client, route_client, source, responses, requests


def test_directory_is_explicit_deduplicated_and_safe(catalog_env):
    _, _, client, source, _, requests = catalog_env
    response = client.get(f"/api/v1/model-configs/{source['id']}/catalog")
    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body["models"]] == ["alpha", "beta", "Gamma-3"]
    assert all(row["capabilities"] is None and row["price"] is None for row in body["models"])
    assert "test-catalog-secret-value" not in response.text
    assert str(requests[0].url) == "https://example.invalid/v1/models"
    assert requests[0].headers["authorization"] == "Bearer test-catalog-secret-value"


def test_import_only_selected_models_uses_independent_provider(catalog_env):
    factory, base_client, client, source, _, requests = catalog_env
    endpoint = f"/api/v1/model-configs/{source['id']}/catalog/import"
    response = client.post(endpoint, json={"model_ids": ["alpha", "beta", "beta"]})
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [(x["model_id"], x["status"]) for x in results] == [("alpha", "existing"), ("beta", "created")]
    beta_id = results[1]["model_config_id"]
    assert "test-catalog-secret-value" not in response.text
    with factory() as db:
        original = db.get(ModelConfig, source["id"])
        beta = db.get(ModelConfig, beta_id)
        assert beta.provider_id != original.provider_id
        assert db.get(Provider, beta.provider_id).encrypted_api_key == db.get(Provider, original.provider_id).encrypted_api_key
        assert beta.capabilities == {} and beta.tested_at is None
        assert len(db.scalars(select(ModelConfig)).all()) == 2
    assert client.post(endpoint, json={"model_ids": ["beta"]}).json()["results"][0]["status"] == "existing"
    assert base_client.delete(f"/api/v1/model-configs/{source['id']}").status_code == 204
    models = base_client.get("/api/v1/model-configs").json()
    assert next(m for m in models if m["id"] == beta_id)["enabled"] is True
    assert len(requests) == 2


def test_invalid_or_unlisted_import_does_not_create_partial_records(catalog_env):
    factory, _, client, source, _, _ = catalog_env
    endpoint = f"/api/v1/model-configs/{source['id']}/catalog/import"
    assert client.post(endpoint, json={"model_ids": ["beta", "unlisted"]}).status_code == 409
    assert client.post(endpoint, json={"model_ids": ["beta", "bad?name"]}).status_code == 422
    with factory() as db:
        assert len(db.scalars(select(ModelConfig)).all()) == 1


@pytest.mark.parametrize("status,data,code", [
    (302, {}, "CATALOG_REDIRECT"),
    (200, {"data": "not a list"}, "CATALOG_INVALID"),
    (401, {"error": "test-catalog-secret-value"}, "PROVIDER_AUTH"),
    (429, {"error": "test-catalog-secret-value"}, "PROVIDER_RATE_LIMIT"),
])
def test_directory_error_does_not_leak_secret(catalog_env, status, data, code):
    _, _, client, source, response_data, requests = catalog_env
    response_data.update(status=status, data=data, headers={"Location": "https://attacker.invalid/"})
    response = client.get(f"/api/v1/model-configs/{source['id']}/catalog")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    assert "test-catalog-secret-value" not in response.text
    assert len(requests) == 1


def test_recommendation_discloses_unknowns_and_never_selects_unverified_models(catalog_env):
    _, _, client, source, _, _ = catalog_env
    response = client.post("/api/v1/model-routing/recommend", json={"default_model_config_id": source["id"]})
    assert response.status_code == 200
    body = response.json()
    assert [x["stage"] for x in body["recommendations"]] == ["think", "plan", "dispatch", "execute", "review"]
    assert all(x["model_config_id"] == source["id"] and x["price"] is None for x in body["recommendations"])
    assert "最优" in json.dumps(body, ensure_ascii=False)


def test_catalog_routes_require_session_and_local_mutation_header(environment):
    _, _, _, client = environment
    client.cookies.clear()
    assert client.get("/api/v1/model-configs/no-id/catalog").status_code == 401
    assert client.post("/api/v1/model-configs/no-id/catalog/import", json={"model_ids": ["alpha"]}).status_code == 401
    assert client.post("/api/v1/model-routing/recommend", json={"default_model_config_id": "no-id"}).status_code == 401


def test_recommendation_uses_only_comparable_user_estimates_per_stage(catalog_env):
    _, base_client, client, source, _, _ = catalog_env
    second = base_client.post("/api/v1/model-configs", json={
        "name": "Second", "adapter_type": "openai-compatible", "model_name": "beta",
        "base_url": "https://example.invalid/v1", "api_key": "second-test-key",
    }).json()
    body = {
        "default_model_config_id": source["id"],
        "candidate_model_config_ids": [source["id"], second["id"]],
        "assessments": [
            {"model_config_id": source["id"], "stage_scores": {"think": 91, "execute": 90},
             "input_price_per_million": "5", "output_price_per_million": "5", "currency": "CNY", "source": "个人估计"},
            {"model_config_id": second["id"], "stage_scores": {"think": 75, "execute": 85},
             "input_price_per_million": "0.5", "output_price_per_million": "1", "currency": "CNY", "source": "个人估计"},
        ],
        "stage_requirements": {
            "think": {"minimum_score": 80, "expected_input_tokens": 1000, "expected_output_tokens": 500},
            "execute": {"minimum_score": 80, "expected_input_tokens": 1000, "expected_output_tokens": 500},
        },
    }
    response = client.post("/api/v1/model-routing/recommend", json=body)
    assert response.status_code == 200, response.text
    by_stage = {r["stage"]: r for r in response.json()["recommendations"]}
    # Only one qualified think model: no price comparison, so preserve default.
    assert by_stage["think"]["model_config_id"] == source["id"] and by_stage["think"]["price"] is None
    assert by_stage["execute"]["model_config_id"] == second["id"]
    assert by_stage["execute"]["price"]["estimated_cost"] == "0.001"
    assert by_stage["execute"]["price"]["basis"] == "user_estimate"
    assert by_stage["execute"]["capabilities"]["stage_score"] == 85
    body["assessments"][1]["currency"] = "USD"
    by_stage = {r["stage"]: r for r in client.post("/api/v1/model-routing/recommend", json=body).json()["recommendations"]}
    assert by_stage["execute"]["model_config_id"] == source["id"] and by_stage["execute"]["price"] is None


def test_recommendation_rejects_duplicate_or_invalid_evidence(catalog_env):
    _, _, client, source, _, _ = catalog_env
    assessment = {"model_config_id": source["id"], "stage_scores": {"execute": 80},
                  "input_price_per_million": "1", "output_price_per_million": "1", "currency": "CNY", "source": "estimate"}
    base = {"default_model_config_id": source["id"]}
    assert client.post("/api/v1/model-routing/recommend", json={**base, "assessments": [assessment, assessment]}).status_code == 422
    assert client.post("/api/v1/model-routing/recommend", json={**base, "assessments": [{**assessment, "input_price_per_million": "NaN"}]}).status_code == 422
    assert client.post("/api/v1/model-routing/recommend", json={**base, "stage_requirements": {
        "think": {"minimum_score": 50, "expected_input_tokens": 0, "expected_output_tokens": 0}}}).status_code == 422


def test_import_detects_source_changes_during_catalog_fetch(catalog_env, monkeypatch):
    from omniharness import model_catalog

    factory, _, client, source, _, _ = catalog_env
    async def changed(config):
        with factory.begin() as db:
            db.get(ModelConfig, source["id"]).version += 1
        return ["beta"]

    monkeypatch.setattr(model_catalog, "fetch_catalog", changed)
    response = client.post(f"/api/v1/model-configs/{source['id']}/catalog/import", json={"model_ids": ["beta"]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONFIG_CHANGED"
    with factory() as db:
        assert len(db.scalars(select(ModelConfig)).all()) == 1


def test_empty_catalog_is_not_a_fake_success(catalog_env):
    _, _, client, source, responses, _ = catalog_env
    responses["data"] = {"data": []}
    response = client.get(f"/api/v1/model-configs/{source['id']}/catalog")
    assert response.status_code == 200 and response.json()["models"] == []
    response = client.post(f"/api/v1/model-configs/{source['id']}/catalog/import", json={"model_ids": ["beta"]})
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url,trust_env", [
    ("http://localhost:18901/v1", False),
    ("http://127.0.0.1:18901/v1", False),
    ("http://[::1]:18901/v1", False),
    ("https://e-flowcode.cc/v1", True),
])
async def test_catalog_uses_environment_proxy_only_for_non_loopback(monkeypatch, base_url, trust_env):
    from omniharness import model_catalog
    from omniharness.providers import RuntimeModelConfig

    original = httpx.AsyncClient
    kwargs_seen = []
    def fake_client(*args, **kwargs):
        kwargs_seen.append(kwargs.copy())
        return original(*args, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []})), **kwargs)

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", fake_client)
    config = RuntimeModelConfig(id="source", adapter_type="openai-compatible", model_name="example", base_url=base_url)
    assert await model_catalog.fetch_catalog(config) == []
    assert kwargs_seen[0]["trust_env"] is trust_env
