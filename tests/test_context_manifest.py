import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from omniharness.context_manifest import input_manifest


def manifest(messages, **overrides):
    return input_manifest(messages, **({"system_message": SystemMessage(content="fixed instructions"),
        "tools": ({"name": "read", "parameters": {"type": "object"}},),
        "model": {"context_policy": {"policy": {"version": "context-v3"}}, "max_output_tokens": 100000},
        "key": b"private-key-" * 4, "run_id": "run-1"} | overrides))


def test_content_order_tools_and_scope_change_fingerprints_without_exposing_text():
    messages = [HumanMessage(content="private requirement", id="one"), AIMessage(content="private answer", id="two")]
    value = manifest(messages)
    assert value["status"] == "recorded" and value["counting"] == "estimate"
    assert value["cache_hit"] == "unknown" and value["estimated_input_tokens"] > 0
    assert value["policy_version"] == "context-v3"
    assert "private" not in json.dumps(value) and "fixed instructions" not in json.dumps(value)
    reversed_value = manifest(list(reversed(messages)))
    assert reversed_value["request_fingerprint"] != value["request_fingerprint"]
    assert reversed_value["prefix_fingerprint"] == value["prefix_fingerprint"]
    assert manifest([HumanMessage(content="different")])["request_fingerprint"] != value["request_fingerprint"]
    assert manifest(messages, tools=())["prefix_fingerprint"] != value["prefix_fingerprint"]
    assert manifest(messages, run_id="run-2")["request_fingerprint"] != value["request_fingerprint"]


def test_transient_ids_do_not_destroy_prefix_stability_and_summary_count_is_explicit():
    first = manifest([HumanMessage(content="same", id="one")])
    second = manifest([HumanMessage(content="same", id="two")])
    assert first["request_fingerprint"] == second["request_fingerprint"]
    value = manifest([HumanMessage(content="summary", id="omni-summary:artifact-1"),
                      ToolMessage(content="stored preview", tool_call_id="call-1")])
    assert value["summary_messages"] == 1 and value["message_types"] == {"human": 1, "tool": 1}


def test_manifest_never_uses_unkeyed_private_text_fingerprints():
    with pytest.raises(ValueError):
        manifest([HumanMessage(content="secret")], key=b"")
