"""Numeric usage and frozen model identities, never provider bodies or secrets."""


def model_binding(snapshot):
    if not isinstance(snapshot, dict):
        return None
    return {"model_config_id": snapshot.get("id"), "model_version": snapshot.get("version"),
            "provider_version": snapshot.get("provider_version"),
            **{key: snapshot.get(key) for key in ("adapter_type", "base_url", "model_name")}}


def normalize_usage(value, *, adapter_type=None):
    value = value if isinstance(value, dict) else {}

    def number(mapping, key):
        item = mapping.get(key) if isinstance(mapping, dict) else None
        return item if type(item) is int and item >= 0 else None

    result = {key: number(value, key) for key in ("input_tokens", "output_tokens", "total_tokens")}
    inputs = value.get("input_token_details", {})
    outputs = value.get("output_token_details", {})
    result.update(cache_read_input_tokens=number(inputs, "cache_read"),
                  cache_creation_input_tokens=number(inputs, "cache_creation"),
                  cache_creation_1h_input_tokens=number(inputs, "cache_creation_1h"),
                  reasoning_output_tokens=number(outputs, "reasoning"))
    # LangChain's standard input token count includes its cache detail counts.
    # Only declare that relation when the numeric details can actually reconcile.
    parts = [result[key] for key in ("cache_read_input_tokens", "cache_creation_input_tokens")]
    included = None
    if any(part is not None for part in parts) and result["input_tokens"] is not None:
        if sum(part or 0 for part in parts) <= result["input_tokens"]:
            included = True
    result["input_includes_cache"] = included
    result["complete"] = all(result[key] is not None for key in ("input_tokens", "output_tokens", "total_tokens"))
    if result["complete"] and result["input_tokens"] + result["output_tokens"] != result["total_tokens"]:
        result["complete"] = False
    return result
