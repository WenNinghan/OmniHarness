"""Native transport is frozen per tested compatible binding, never inferred by name."""
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from omniharness import context_host, providers
from omniharness.context_host import DurableSummaryExecutor
from omniharness.providers import RuntimeModelConfig, agent_streaming_enabled, summary_capability_binding
from omniharness.services import frozen_model_capabilities


def configured():
    config = RuntimeModelConfig(id="tested", adapter_type="openai-compatible", model_name="model",
                                base_url="https://example.invalid/v1", api_key="test-only-key")
    snapshot = asdict(config)
    snapshot.pop("api_key")
    probe = {"tools": {"status": "supported", "transport": providers.NATIVE_AGENT_STREAM,
                       "transport_binding": summary_capability_binding(config)}}
    snapshot["capabilities"] = frozen_model_capabilities(snapshot, probe)
    return config, snapshot, probe


@pytest.mark.parametrize("version", ["agent-v1", "stage-agent-v2", "stage-agent-v3"])
def test_old_workflows_do_not_adopt_native_streaming(version):
    config, snapshot, _ = configured()
    assert not agent_streaming_enabled(snapshot, version, config)


def test_transport_requires_exact_tested_endpoint_model_and_parameters():
    config, snapshot, probe = configured()
    assert agent_streaming_enabled(snapshot, "stage-agent-v4", config)
    assert not agent_streaming_enabled({**snapshot, "capabilities": {"tools": {"status": "supported"}}},
                                      "stage-agent-v4", config)
    for key, value in (("base_url", "https://different.invalid/v1"), ("model_name", "other"),
                       ("parameters", {"thinking": {"type": "disabled"}})):
        changed = {**snapshot, key: value}
        assert not agent_streaming_enabled(changed, "stage-agent-v4")
        assert "transport" not in frozen_model_capabilities(changed, probe)["tools"]
        assert not agent_streaming_enabled(snapshot, "stage-agent-v4", replace(config, **{key: value}))
    # Output and timeout ceilings do not change protocol identity; summary caps remain independent.
    assert agent_streaming_enabled(snapshot, "stage-agent-v4", replace(config, max_output_tokens=4000))


def test_factory_native_opt_in_keeps_original_limits_and_disables_retries(monkeypatch):
    captured = {}
    monkeypatch.setattr(providers.importlib, "import_module", lambda _: SimpleNamespace(
        CompatibleChatModel=lambda **kwargs: captured.update(kwargs)))
    config, _, _ = configured()
    providers.build_chat_model(config, native_streaming=True)
    assert captured["streaming"] is True and captured["omni_normalize_stream_usage"] is True
    assert captured["max_retries"] == 0 and captured["max_tokens"] == 100000
    assert captured["timeout"] == config.timeout_seconds
    assert captured["stream_chunk_timeout"] == config.timeout_seconds
    captured.clear()
    providers.build_chat_model(config)
    assert "streaming" not in captured and "omni_normalize_stream_usage" not in captured


def test_summary_uses_same_frozen_native_transport_but_unverified_changes_do_not(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(context_host, "build_chat_model", lambda config, **kwargs: calls.append(kwargs))
    config, snapshot, _ = configured()
    worker = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    host = SimpleNamespace(run_id="run", workflow_version="stage-agent-v4")
    summary = DurableSummaryExecutor(worker, "run", snapshot, host=host)
    summary.model_factory(replace(config, max_output_tokens=4000))
    assert calls[-1] == {"native_streaming": True}
    summary.model_factory(replace(config, parameters={"thinking": {"type": "disabled"}}))
    assert calls[-1] == {}
    legacy = DurableSummaryExecutor(worker, "run", snapshot,
                                    host=SimpleNamespace(run_id="run", workflow_version="stage-agent-v3"))
    legacy.model_factory(config)
    assert calls[-1] == {}
