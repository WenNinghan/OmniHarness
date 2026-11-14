"""Private, run-scoped fingerprints of host-prepared model input.

Fingerprints are keyed to avoid exposing guesses about short private messages.
They describe the host representation, never provider wire bytes or cache hits.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from .context_policy import context_budget, counter_for_policy, parse_context_configuration


def input_manifest(messages, *, system_message, tools, model, key: bytes, run_id: str):
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("A private fingerprint key is required")
    scoped_key = hmac.digest(key, ("omni-input-v1:" + run_id).encode(), "sha256")

    def digest(value):
        data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode()
        return hmac.new(scoped_key, data, hashlib.sha256).hexdigest()

    def prepared(message):
        return message.model_dump(mode="json", exclude={"response_metadata", "usage_metadata", "id"})

    rows = [prepared(message) for message in messages]
    system = prepared(system_message) if system_message is not None else None
    policy, limits = parse_context_configuration(model.get("context_policy"))
    actual = ([system_message] if system_message is not None else []) + list(messages)
    counter = counter_for_policy(policy)
    estimates = counter.count(actual, tools=tuple(tools))
    budget = context_budget(policy, limits, output_tokens=model.get("max_output_tokens", 100_000))
    kinds = {}
    for message in messages:
        kind = message.type if message.type in {"human", "ai", "tool", "system"} else "other"
        kinds[kind] = kinds.get(kind, 0) + 1
    summaries = sum(isinstance(message.id, str) and message.id.startswith("omni-summary:") for message in messages)
    prefix = {"system": system, "tools": list(tools)}
    return {"schema_version": 1, "status": "recorded", "representation": "host_prepared_input",
        "fingerprint_scope": "run", "request_fingerprint": digest({**prefix, "messages": rows}),
        "prefix_fingerprint": digest(prefix), "message_count": len(rows), "message_types": kinds,
        "summary_messages": summaries, "tool_count": len(tools), "policy_version": policy.version,
        "estimated_input_tokens": estimates, "input_limit": budget.input_limit,
        "counting": "estimate", "counting_method": counter.method,
        "cache_hit": "unknown", "provider_receipt": "unknown"}
