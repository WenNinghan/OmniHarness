from dataclasses import asdict, replace
from types import SimpleNamespace

import httpx
import pytest

from omniharness import providers
from omniharness.models import Run
from omniharness.providers import ProviderError, RuntimeModelConfig, summary_capability_binding
from omniharness.request_policy import REQUEST_POLICY_KEY, RequestPolicyError, parse_request_policy
from omniharness.security import SecretStore
from omniharness.services import runtime_config


def parameters(policy=None):
    return {REQUEST_POLICY_KEY: {"version": 1, **(policy or {})}}


def config(**kwargs):
    return RuntimeModelConfig(id="local-policy", adapter_type="openai-compatible", model_name="test-model",
                              base_url="http://127.0.0.1:1/v1", api_key="test-only", **kwargs)


def test_absence_preserves_legacy_and_explicit_policy_has_bounded_defaults():
    assert parse_request_policy({"temperature": 0.2}) is None
    policy = parse_request_policy(parameters())
    assert asdict(policy.timeouts) == {
        "connect_seconds": 15, "read_seconds": 300, "write_seconds": 30, "pool_seconds": 15,
        "first_chunk_seconds": 300, "idle_seconds": 90, "total_seconds": 600,
    }
    assert asdict(policy.recovery) == {"max_attempts": 1, "backoff_seconds": 1, "max_backoff_seconds": 30}
    assert providers.request_total_seconds(config(timeout_seconds=90)) == 90
    assert providers.request_total_seconds(config(timeout_seconds=90, parameters=parameters())) == 600


@pytest.mark.parametrize("policy", [
    None, [], True, {"version": True}, {"version": 2}, {"version": 1, "unknown": "private-value"},
    {"version": 1, "timeouts": None}, {"version": 1, "timeouts": {"read": 1}},
    *({"version": 1, "timeouts": {"read_seconds": value}}
      for value in (True, 0, -1, "private-value", float("nan"), float("inf"), 3601, 10 ** 1000)),
    {"version": 1, "timeouts": {"first_chunk_seconds": 601}},
    {"version": 1, "timeouts": {"idle_seconds": 601}},
    {"version": 1, "recovery": []}, {"version": 1, "recovery": {"unknown": 1}},
    *({"version": 1, "recovery": {"max_attempts": value}} for value in (True, 1.0, 0, 3, None)),
    *({"version": 1, "recovery": {"backoff_seconds": value}}
      for value in (-1, True, float("nan"), float("inf"), 301)),
    {"version": 1, "recovery": {"backoff_seconds": 2, "max_backoff_seconds": 1}},
])
def test_policy_rejects_malformed_unbounded_and_unknown_values_without_echo(policy):
    with pytest.raises(RequestPolicyError) as error:
        parse_request_policy({REQUEST_POLICY_KEY: policy})
    assert "private-value" not in str(error.value)


def test_small_fault_injection_timeouts_and_zero_backoff_are_supported():
    value = parameters({"timeouts": {"connect_seconds": 0.01, "read_seconds": 0.02,
                                    "first_chunk_seconds": 0.01, "idle_seconds": 0.01,
                                    "total_seconds": 0.03},
                        "recovery": {"max_attempts": 2, "backoff_seconds": 0, "max_backoff_seconds": 0}})
    policy = parse_request_policy(value)
    assert policy.timeouts.connect_seconds == 0.01
    assert policy.recovery.max_attempts == 2 and policy.recovery.backoff_seconds == 0


@pytest.mark.parametrize("adapter", ["anthropic", "gemini", "mock"])
def test_unsupported_adapters_do_not_claim_split_timeout_support(adapter):
    with pytest.raises(ProviderError) as error:
        providers.validate_model_parameters(adapter, parameters())
    assert error.value.code == "INVALID_PARAMETERS"


@pytest.mark.parametrize("adapter,native", [("openai-compatible", False), ("openai-compatible", True),
                                            ("openai", False)])
def test_factory_strips_local_namespace_and_sets_explicit_network_timeouts(monkeypatch, adapter, native):
    captured = {}

    def constructor(**kw):
        return captured.update(kw)

    monkeypatch.setattr(providers.importlib, "import_module", lambda _: SimpleNamespace(
        CompatibleChatModel=constructor, RequestTimedChatOpenAI=constructor))
    cfg = replace(config(parameters={"temperature": 0.1, **parameters()}), adapter_type=adapter)
    providers.build_chat_model(cfg, native_streaming=native)
    assert REQUEST_POLICY_KEY not in captured
    assert captured["temperature"] == 0.1 and captured["max_retries"] == 0
    assert isinstance(captured["timeout"], httpx.Timeout)
    assert captured["timeout"].connect == 15 and captured["timeout"].read == 300
    assert captured["timeout"].write == 30 and captured["timeout"].pool == 15
    assert captured["omni_request_timeouts"]["total_seconds"] == 600
    assert captured["stream_chunk_timeout"] is None
    assert cfg.parameters[REQUEST_POLICY_KEY]["version"] == 1


def test_local_policy_does_not_change_tested_vendor_protocol_identity():
    original = config(parameters={"temperature": 0.1})
    explicit = replace(original, parameters={**original.parameters, **parameters()})
    assert summary_capability_binding(original) == summary_capability_binding(explicit)
    assert summary_capability_binding(original) == summary_capability_binding(asdict(explicit))
    assert summary_capability_binding(original) != summary_capability_binding(
        replace(explicit, parameters={**explicit.parameters, "temperature": 0.2}))


def test_policy_is_saved_and_frozen_per_run_without_changing_old_snapshot(environment):
    settings, _, factory, client = environment
    body = {"name": "Policy", "adapter_type": "openai-compatible", "model_name": "local-test",
            "base_url": "http://127.0.0.1:1/v1", "api_key": "test-only-policy-key",
            "parameters": parameters()}
    created = client.post("/api/v1/model-configs", json=body)
    assert created.status_code == 201, created.text
    model = created.json()
    assert model["parameters"] == body["parameters"]
    conversation = client.post("/api/v1/conversations", json={}).json()
    path = f"/api/v1/conversations/{conversation['id']}/messages"
    request = {"content": "Only freeze configuration", "model_config_id": model["id"],
               "client_request_id": "policy-before"}
    before = client.post(path, json=request)
    assert before.status_code == 202, before.text
    changed = parameters({"timeouts": {"read_seconds": 600}, "recovery": {"max_attempts": 2}})
    updated = client.put(f"/api/v1/model-configs/{model['id']}", json={**body, "parameters": changed})
    assert updated.status_code == 200, updated.text
    after = client.post(path, json={**request, "client_request_id": "policy-after"})
    assert after.status_code == 202, after.text
    with factory() as db:
        old = db.get(Run, before.json()["run_id"]).config_snapshot
        new = db.get(Run, after.json()["run_id"]).config_snapshot
        assert old["parameters"] == body["parameters"] and new["parameters"] == changed
        assert parse_request_policy(runtime_config(old, SecretStore(settings)).parameters).recovery.max_attempts == 1
        assert parse_request_policy(runtime_config(new, SecretStore(settings)).parameters).recovery.max_attempts == 2


def test_schema_rejects_invalid_local_policy_without_network(client):
    response = client.post("/api/v1/model-configs", json={
        "name": "Invalid", "adapter_type": "openai-compatible", "model_name": "test",
        "base_url": "http://127.0.0.1:1/v1", "parameters": parameters({"recovery": {"max_attempts": 3}}),
    })
    assert response.status_code == 422
