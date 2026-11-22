from dataclasses import asdict, replace

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from omniharness import providers
from omniharness.providers import (
    RuntimeModelConfig,
    freeze_json_mode_capability,
    probe_model,
    summary_capability_binding,
)
from omniharness.schemas import ModelConfigRequest, ProbeRequest
from omniharness.services import frozen_model_capabilities
from omniharness.structured_json import supports_stage_json_mode


def _cfg(adapter="openai-compatible", **kwargs):
    values = {
        "id": "m-json",
        "adapter_type": adapter,
        "model_name": "demo-json-model",
        "api_key": "unit-test-secret-key",
        "base_url": "http://127.0.0.1:18080/v1",
    }
    values.update(kwargs)
    return RuntimeModelConfig(**values)


def test_schemas_allow_stage_json_mode_and_json_mode_probe():
    req_true = ModelConfigRequest(
        name="M1",
        adapter_type="openai-compatible",
        model_name="demo",
        base_url="http://127.0.0.1:11434/v1",
        parameters={"omni_stage_json_mode": True},
    )
    req_false = ModelConfigRequest(
        name="M2",
        adapter_type="openai-compatible",
        model_name="demo",
        base_url="http://127.0.0.1:11434/v1",
        parameters={"omni_stage_json_mode": False},
    )
    assert req_true.parameters["omni_stage_json_mode"] is True
    assert req_false.parameters["omni_stage_json_mode"] is False

    with pytest.raises(ValidationError):
        ModelConfigRequest(
            name="Bad",
            adapter_type="openai-compatible",
            model_name="demo",
            base_url="http://127.0.0.1:11434/v1",
            parameters={"omni_stage_json_mode": "yes"},
        )

    assert ProbeRequest().probes == ["chat", "streaming", "tools", "structured"]
    assert ProbeRequest(probes=["json_mode"]).probes == ["json_mode"]
    with pytest.raises(ValidationError):
        ProbeRequest(probes=["unknown_probe"])


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter,base_url,api_key", [
    ("mock", None, None),
    ("anthropic", None, "unit-test-secret"),
    ("gemini", None, "unit-test-secret"),
])
async def test_json_mode_probe_skips_unsupported_adapters_without_dispatch(monkeypatch, adapter, base_url, api_key):
    monkeypatch.setattr(providers, "build_chat_model", lambda *_, **__: pytest.fail("must not build model"))
    result = await probe_model(_cfg(adapter, base_url=base_url, api_key=api_key), ["json_mode"])
    assert result["json_mode"]["status"] == "unsupported"
    if adapter != "mock":
        assert result["json_mode"]["model_calls"] == 0


@pytest.mark.asyncio
async def test_json_mode_probe_accepts_raw_json_and_binds_usage(monkeypatch):
    captured = {}

    class BoundModel:
        def __init__(self, bind_kwargs):
            captured["bind_kwargs"] = bind_kwargs

        async def ainvoke(self, messages):
            captured["messages"] = messages
            return AIMessage(
                content='{"ok": true}',
                response_metadata={"finish_reason": "stop"},
                usage_metadata={"input_tokens": 11, "output_tokens": 5, "total_tokens": 16},
            )

    class RawModel:
        def bind(self, **kwargs):
            return BoundModel(kwargs)

    def factory(cfg):
        captured["probe_cfg"] = cfg
        return RawModel()

    monkeypatch.setattr(providers, "build_chat_model", factory)
    cfg = _cfg(max_output_tokens=100000, timeout_seconds=120)
    res = (await probe_model(cfg, ["json_mode"]))["json_mode"]

    assert res["status"] == "supported"
    assert res["model_calls"] == 1
    assert res["usage"] == {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16}
    assert res["binding"] == summary_capability_binding(cfg)
    assert captured["bind_kwargs"] == {"response_format": {"type": "json_object"}}
    assert captured["probe_cfg"].max_output_tokens == 256
    assert captured["probe_cfg"].timeout_seconds == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    '```json\n{"ok": true}\n```',
    '{"ok": false}',
    "not json at all",
])
async def test_json_mode_probe_rejects_fenced_or_invalid_outputs(monkeypatch, content):
    class BoundModel:
        async def ainvoke(self, _messages):
            return AIMessage(content=content, response_metadata={"finish_reason": "stop"})

    class RawModel:
        def bind(self, **_kwargs):
            return BoundModel()

    monkeypatch.setattr(providers, "build_chat_model", lambda _: RawModel())
    res = (await probe_model(_cfg(), ["json_mode"]))["json_mode"]
    assert res["status"] == "unsupported"
    assert res["model_calls"] == 1


@pytest.mark.asyncio
async def test_json_mode_probe_sanitizes_provider_errors(monkeypatch):
    class BoundModel:
        async def ainvoke(self, _messages):
            raise RuntimeError("secret-token-leak-XYZ")

    class RawModel:
        def bind(self, **_kwargs):
            return BoundModel()

    monkeypatch.setattr(providers, "build_chat_model", lambda _: RawModel())
    res = (await probe_model(_cfg(), ["json_mode"]))["json_mode"]
    assert res["status"] == "error"
    assert "secret-token-leak-XYZ" not in str(res)


def test_freeze_and_supports_stage_json_mode_respect_binding_and_override():
    cfg = _cfg()
    snap = asdict(cfg)
    binding = summary_capability_binding(snap)
    cap = {
        "schema_version": 1,
        "status": "supported",
        "binding": binding,
        "checked_at": "2026-11-23T00:00:00+00:00",
        "model_calls": 1,
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        "message": "private detail",
    }
    frozen_cap = freeze_json_mode_capability(snap, cap)
    assert frozen_cap is not None
    assert frozen_cap["status"] == "supported"
    assert "message" not in frozen_cap

    frozen_caps = frozen_model_capabilities(snap, {"json_mode": cap})
    assert "json_mode" in frozen_caps
    assert supports_stage_json_mode({**snap, "capabilities": frozen_caps}) is True

    # Explicit parameter override wins in both directions.
    assert supports_stage_json_mode({
        **snap,
        "parameters": {"omni_stage_json_mode": False},
        "capabilities": frozen_caps,
    }) is False

    # Stale binding after endpoint/model/parameter change is dropped and rejected.
    for change in (
        {"model_name": "other-model"},
        {"base_url": "http://127.0.0.1:9999/v1"},
        {"parameters": {"temperature": 0.3}},
    ):
        changed = asdict(replace(cfg, **change))
        assert freeze_json_mode_capability(changed, cap) is None
        assert "json_mode" not in frozen_model_capabilities(changed, {"json_mode": cap})
        assert supports_stage_json_mode({**changed, "capabilities": {"json_mode": cap}}) is False
