from dataclasses import replace

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from omniharness.context_policy import (
    ApproximateContextCounter,
    ContextPolicy,
    ContextPolicyError,
    ModelContextLimits,
    context_budget,
    context_configuration_dict,
    parse_context_configuration,
)


def test_local_defaults_leave_main_100k_output_unchanged():
    policy = ContextPolicy()
    budget = context_budget(policy, ModelContextLimits(), output_tokens=100_000)
    assert (policy.input_target_tokens, policy.summarize_at, policy.summary_output_tokens) == (16000, 0.8, 4000)
    assert budget.input_limit == 16000 and budget.summarize_at_tokens == 12800
    assert budget.output_reservation == 100000 and not budget.provider_window_known


def test_total_window_reserves_output_but_input_only_limit_does_not():
    policy = ContextPolicy()
    total = context_budget(policy, ModelContextLimits(context_window_tokens=110_000), output_tokens=100_000)
    input_only = context_budget(policy, ModelContextLimits(max_input_tokens=9000), output_tokens=100_000)
    both = context_budget(policy, ModelContextLimits(context_window_tokens=110000, max_input_tokens=8000),
                          output_tokens=100000)
    assert total.input_limit == 8976
    assert input_only.input_limit == 9000 and not input_only.provider_window_known
    assert both.input_limit == 8000


def test_summary_has_separate_reservation_without_mutating_policy():
    policy = ContextPolicy()
    limits = ModelContextLimits(context_window_tokens=110000)
    assert context_budget(policy, limits, output_tokens=policy.summary_output_tokens).input_limit == 16000
    assert context_budget(policy, limits, output_tokens=100000).output_reservation == 100000


@pytest.mark.parametrize("window", [50000, 100000, 101024])
def test_known_window_conflict_is_explicit_not_silent_clamping(window):
    with pytest.raises(ContextPolicyError, match=f"window {window}.*output 100000") as exc:
        context_budget(ContextPolicy(), ModelContextLimits(context_window_tokens=window), output_tokens=100000)
    assert exc.value.code == "CONTEXT_WINDOW_CONFLICT"


@pytest.mark.parametrize("change", [
    {"input_target_tokens": 0}, {"summary_output_tokens": True}, {"max_messages": -1},
    {"summarize_at": 1}, {"summarize_at": float("nan")}, {"summarize_at": False}, {"version": ""},
    {"version": "context-v2"},
])
def test_invalid_policy_is_rejected(change):
    with pytest.raises(ContextPolicyError):
        replace(ContextPolicy(), **change)


def test_counter_includes_system_tools_unicode_and_structured_json():
    counter = ApproximateContextCounter()
    messages = [SystemMessage(content="必须保留要求"), HumanMessage(content='中文🧪 {"数量": 3}')]
    tool = {"type": "function", "function": {"name": "read", "description": "读取工件",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
    assert counter.count(messages) > counter.count(messages[1:])
    assert counter.count(messages, tools=(tool,)) > counter.count(messages)
    assert counter.estimated and "approximate" in counter.method


@pytest.mark.parametrize("limit", [0, -1, True])
def test_unknown_provider_limits_are_null_not_invalid_numbers(limit):
    with pytest.raises(ContextPolicyError):
        ModelContextLimits(max_input_tokens=limit)


def test_versioned_configuration_roundtrip_and_legacy_empty_defaults():
    expected = (ContextPolicy(), ModelContextLimits())
    assert parse_context_configuration(None) == parse_context_configuration({}) == expected
    exported = context_configuration_dict(*expected)
    assert parse_context_configuration(exported) == expected
    assert exported["model_limits"]["context_window_tokens"] is None
    assert "max_output_tokens" not in exported["policy"]


@pytest.mark.parametrize("value", [[], {"schema_version": True}, {"schema_version": 2},
                                   {"policy": {"max_output_tokens": 1}}, {"policy": []},
                                   {"model_limits": {"source": {}}}, {"hidden": 1}])
def test_context_configuration_rejects_unknown_schema_and_fields(value):
    with pytest.raises(ContextPolicyError):
        parse_context_configuration(value)
