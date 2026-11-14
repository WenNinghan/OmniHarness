from omniharness.model_usage import model_binding, normalize_usage


def test_usage_never_invents_missing_or_double_counts_reasoning():
    usage = normalize_usage({"input_tokens": 100, "output_tokens": 40, "total_tokens": 140,
                             "input_token_details": {"cache_read": 60},
                             "output_token_details": {"reasoning": 30}, "api_key": "private"})
    assert usage["complete"] and usage["output_tokens"] == 40
    assert usage["reasoning_output_tokens"] == 30
    assert usage["cache_read_input_tokens"] == 60 and usage["input_includes_cache"] is True
    assert usage["cache_creation_input_tokens"] is None and "api_key" not in usage


def test_usage_rejects_invalid_counts_and_inconsistent_totals():
    assert normalize_usage({"input_tokens": True, "output_tokens": -1})["input_tokens"] is None
    assert normalize_usage({"input_tokens": 1, "output_tokens": 2, "total_tokens": 4})["complete"] is False
    assert normalize_usage(None)["complete"] is False
    assert normalize_usage({"input_tokens": 1, "input_token_details": {"cache_read": 2}})["input_includes_cache"] is None


def test_model_binding_contains_only_frozen_public_identity():
    binding = model_binding({"id": "m", "model_name": "test", "version": 3,
                             "encrypted_api_key": "private", "parameters": {"secret": "private"}})
    assert binding["model_config_id"] == "m" and binding["model_version"] == 3
    assert "encrypted_api_key" not in binding and "parameters" not in binding
