from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_providers import config

from omniharness import providers
from omniharness.providers import ProviderError, build_chat_model, freeze_summary_capability, probe_model


def test_only_narrow_compatible_thinking_parameter_maps_to_extra_body(monkeypatch):
    captured = {}
    monkeypatch.setattr(providers.importlib, "import_module", lambda name: SimpleNamespace(
        CompatibleChatModel=lambda **kwargs: captured.update(kwargs)))
    original = config(parameters={"thinking": {"type": "disabled"}, "temperature": .2})
    build_chat_model(original)
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert captured["max_retries"] == 0 and captured["max_tokens"] == 100000
    assert original.parameters["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("parameters", [
    {"thinking": True}, {"thinking": {"type": "disabled", "api_key": "private"}},
    {"thinking": {"type": "auto"}}, {"thinking": {"budget_tokens": 1}},
])
def test_unknown_thinking_parameters_rejected(parameters):
    with pytest.raises(ProviderError):
        build_chat_model(config(parameters=parameters))


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning,raw,status", [
    (0, {}, "supported"), (None, {"reasoning_content": ""}, "supported"),
    (None, {}, "unknown"), (1, {}, "unsupported"),
    (None, {"reasoning_content": "PRIVATE"}, "unsupported"),
])
async def test_probe_observes_effect_not_just_parameter_acceptance(monkeypatch, reasoning, raw, status):
    calls, configs = [], []
    usage = {"input_tokens": 12, "output_tokens": 10, "total_tokens": 22}
    if reasoning is not None:
        usage["output_token_details"] = {"reasoning": reasoning}

    class Model:
        async def ainvoke(self, messages):
            calls.append(messages)
            return AIMessage(content="PUBLIC must not persist", additional_kwargs=raw,
                             response_metadata={"finish_reason": "stop"}, usage_metadata=usage)

    def factory(value):
        configs.append(value)
        return Model()

    monkeypatch.setattr(providers, "build_chat_model", factory)
    original = config(max_output_tokens=100000, timeout_seconds=120)
    result = (await probe_model(original, ["summary_non_thinking"]))["summary_non_thinking"]
    assert result["status"] == status and result["parameter_accepted"] is True
    assert len(calls) == 1 and result["model_calls"] == 1
    assert configs[0].max_output_tokens == 512 and configs[0].timeout_seconds == 60
    assert configs[0].parameters == {"thinking": {"type": "disabled"}} and original.parameters == {}
    assert "PRIVATE" not in str(result) and "PUBLIC" not in str(result)
    frozen = freeze_summary_capability(asdict(original), result)
    assert frozen["status"] == status
    for change in ({"model_name": "other"}, {"base_url": "http://127.0.0.1:222/v1"},
                   {"parameters": {"temperature": .1}}):
        assert freeze_summary_capability(asdict(replace(original, **change)), result)["status"] == "unknown"
    assert freeze_summary_capability({**asdict(original), "version": 99, "context_policy": {}}, result)["status"] == status


@pytest.mark.asyncio
async def test_probe_error_is_one_call_without_response_or_secret(monkeypatch):
    calls = []

    class Model:
        async def ainvoke(self, messages):
            calls.append(messages)
            raise TimeoutError("private-upstream-message")

    monkeypatch.setattr(providers, "build_chat_model", lambda _: Model())
    result = await probe_model(config(), ["summary_non_thinking", "summary_non_thinking"])
    assert len(calls) == 1 and result["summary_non_thinking"]["status"] == "error"
    assert "private-upstream-message" not in str(result)


@pytest.mark.asyncio
async def test_truncated_probe_retains_usage_but_never_marks_supported(monkeypatch):
    class Model:
        async def ainvoke(self, messages):
            return AIMessage(content="partial private fixture", response_metadata={"finish_reason": "length"},
                usage_metadata={"input_tokens": 20, "output_tokens": 512, "total_tokens": 532,
                                "output_token_details": {"reasoning": 0}})
    monkeypatch.setattr(providers, "build_chat_model", lambda _: Model())
    result = (await probe_model(config(), ["summary_non_thinking"]))["summary_non_thinking"]
    assert result["status"] == "error" and result["usage"]["output_tokens"] == 512
    assert result["model_calls"] == 1 and "partial private fixture" not in str(result)


@pytest.mark.asyncio
async def test_probe_does_not_guess_other_adapter_support(monkeypatch):
    monkeypatch.setattr(providers, "build_chat_model", lambda _: pytest.fail("must not dispatch"))
    result = await probe_model(config("anthropic"), ["summary_non_thinking"])
    assert result["summary_non_thinking"]["model_calls"] == 0
    assert result["summary_non_thinking"]["status"] == "unsupported"


def test_freeze_drops_unproven_flags_and_nested_private_metadata():
    snapshot = asdict(config())
    cap = {"schema_version": 1, "binding": providers.summary_capability_binding(snapshot),
           "status": "supported", "parameter_accepted": True, "non_thinking_observed": True,
           "usage": {"input_tokens": 1, "output_tokens": {"key": "private"}},
           "message": "private", "response": "private"}
    assert freeze_summary_capability(snapshot, cap)["status"] == "unknown"
    cap["observation_basis"] = "reported_zero_reasoning"
    frozen = freeze_summary_capability(snapshot, cap)
    assert frozen["status"] == "supported" and "private" not in str(frozen)
