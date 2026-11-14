from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from omniharness.reference_cost import calculate_reference_cost, load_reference_prices

AT = datetime(2026, 11, 9, 4, tzinfo=timezone.utc)


def operation(identity="one", model="deepseek-v4-pro", **usage):
    return {"operation_id": identity, "node_id": "agent:execute:model_calls", "purpose": "task",
            "model_binding": {"model_config_id": model, "model_version": 1, "provider_version": 1,
                "adapter_type": "openai-compatible", "base_url": "https://e-flowcode.cc/v1", "model_name": model},
            "normalized_usage": [{"input_tokens": 1000, "output_tokens": 2000, "total_tokens": 3000,
                                   "complete": True, **usage}]}


def test_mixed_exact_decimal_includes_failed_summary_and_recovery():
    a, b = operation(), operation("two", "deepseek-flash")
    b.update(purpose="context_summary", status="failed", recovery_of="previous")
    result = calculate_reference_cost([a, b], expected_calls=2, at=AT)
    assert result["reference_cost"] == "0.084" and result["status"] == "COMPLETE"
    assert result["billed_cost"] is None and result["group_multiplier"] is None
    assert result["entries"][1]["recovery_of"] == "previous"


def test_cache_and_reasoning_not_double_counted_duplicate_event_deduplicated():
    a = operation(cache_read_input_tokens=900, reasoning_output_tokens=1500)
    result = calculate_reference_cost([a, deepcopy(a)], expected_calls=1, at=AT)
    assert result["reference_cost"] == "0.063" and len(result["entries"]) == 1


def test_unknown_or_missing_usage_never_zero_or_complete():
    a = operation()
    b = operation("two")
    b["normalized_usage"] = [None]
    r = calculate_reference_cost([a, b], expected_calls=2, at=AT)
    assert r["reference_cost"] is None and r["observed_reference_cost"] == "0.063"
    assert r["status"] == "PARTIAL"
    assert calculate_reference_cost([], expected_calls=1, at=AT)["reference_cost"] is None
    assert calculate_reference_cost([operation(complete=False)], at=AT)["status"] == "UNKNOWN"


def test_expired_wrong_endpoint_or_conflicting_duplicate_are_unknown():
    a = operation()
    assert calculate_reference_cost([a], at=datetime(2026, 12, 1, tzinfo=timezone.utc))["status"] == "UNKNOWN"
    for endpoint in ("https://other.example/v1", "https://e-flowcode.cc/v1?group=other"):
        b = deepcopy(a)
        b["model_binding"]["base_url"] = endpoint
        assert calculate_reference_cost([b], at=AT)["status"] == "UNKNOWN"
    b = deepcopy(a)
    b["normalized_usage"][0]["input_tokens"] = 1
    assert calculate_reference_cost([a, b], at=AT)["reference_cost"] is None


def test_shared_snapshot_has_all_25_original_highest_quotes():
    snapshot = load_reference_prices()
    assert len(snapshot["quotes"]) == 25
    assert Decimal(snapshot["quotes"]["deepseek-flash"]["input"]) == 3
    assert Decimal(snapshot["quotes"]["deepseek-v4-pro"]["output"]) == 27
    assert Decimal(snapshot["quotes"]["glm-5.1"]["input"]) == 8


def test_tool_record_with_role_binding_is_not_a_paid_model_request():
    model, tool = operation(), operation("tool")
    tool.update(node_id="agent:execute:tool_calls", normalized_usage=[])
    result = calculate_reference_cost([model, tool], expected_calls=1, at=AT)
    assert result["status"] == "COMPLETE" and result["reference_cost"] == "0.063"
