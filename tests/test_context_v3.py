"""context-v3: deterministic tool-result masking, reasoning-aware counting and
bounded degradation. Offline only; the durable host adapter is a test double."""
import json
from dataclasses import asdict, replace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_context_runtime import HostExecutor, prepare, request

from omniharness.context_policy import (
    ContextPolicy,
    ContextPolicyError,
    ContextPolicyV2,
    ContextPolicyV3,
    UnicodeContextCounter,
    context_configuration_dict,
    counter_for_policy,
    parse_context_configuration,
)
from omniharness.context_runtime import (
    ContextPreparationError,
    StoredSummary,
    interaction_groups,
    mask_older_tool_results,
    masked_tool_message,
)

RUN = "run-1"  # request() scope run_id


def tool_exchange(index, *, size=3000, run_id=RUN, artifacts=True, name="workspace_read"):
    call = AIMessage(id=f"ai-{index}", content="", tool_calls=[
        {"id": f"call-{index}", "name": name, "args": {"path": f"f{index}.py"}}])
    body = {"path": f"f{index}.py", "content": "源码" * size, "sha256": f"{index:064x}"}
    if artifacts:
        body["artifacts"] = [{"id": f"art-{index}", "run_id": run_id, "kind": "tool_result"}]
    result = ToolMessage(id=f"tool-{index}", name=name, tool_call_id=f"call-{index}",
                         content=json.dumps(body, ensure_ascii=False))
    return call, result


def history(count, **kwargs):
    return tuple(message for index in range(count) for message in tool_exchange(index, **kwargs))


def v3_request(messages, **policy):
    return replace(request(count=0, policy=ContextPolicyV3(**policy)), history_messages=tuple(messages))


# ---- policy -----------------------------------------------------------------

def test_v3_is_explicit_and_older_versions_are_unchanged():
    assert type(parse_context_configuration({})[0]) is ContextPolicy
    assert type(parse_context_configuration({"policy": {"version": "context-v2"}})[0]) is ContextPolicyV2
    v3, limits = parse_context_configuration({"policy": {"version": "context-v3"}})
    assert type(v3) is ContextPolicyV3 and isinstance(v3, ContextPolicyV2)
    assert (v3.input_target_tokens, v3.keep_recent_tokens) == (32000, 8000)
    assert (v3.replayed_reasoning, v3.mask_tool_results, v3.mask_keep_recent_exchanges,
            v3.summary_failure) == ("count", True, 4, "degrade")
    assert parse_context_configuration(context_configuration_dict(v3, limits))[0] == v3
    # v2 serialization did not gain v3 fields.
    assert "mask_tool_results" not in asdict(ContextPolicyV2())


@pytest.mark.parametrize("change", [
    {"replayed_reasoning": "auto"}, {"mask_tool_results": 1}, {"mask_keep_recent_exchanges": 0},
    {"mask_keep_recent_exchanges": 21}, {"mask_keep_recent_exchanges": True}, {"summary_failure": "retry"},
    {"summary_recovery_attempts": 2},  # inherited v2 bound still applies
])
def test_v3_rejects_unbounded_or_unknown_values(change):
    with pytest.raises(ContextPolicyError):
        replace(ContextPolicyV3(), **change)


def test_v2_payload_with_v3_fields_is_rejected():
    with pytest.raises(ContextPolicyError):
        parse_context_configuration({"policy": {"version": "context-v2", "summary_failure": "degrade"}})


def test_replayed_reasoning_ignore_changes_only_the_explicit_v3_counter():
    native = AIMessage(content="", tool_calls=[{"id": "c", "name": "read", "args": {}}],
                       additional_kwargs={"reasoning_content": "私有" * 600})
    public = native.model_copy(update={"additional_kwargs": {}})
    counting = counter_for_policy(ContextPolicyV3())
    ignoring = counter_for_policy(ContextPolicyV3(replayed_reasoning="ignore"))
    assert counting.count([native]) - counting.count([public]) == 1200
    assert ignoring.count([native]) == ignoring.count([public])
    assert ignoring.method != UnicodeContextCounter().method and ignoring.estimated
    assert counter_for_policy(ContextPolicyV2()).count([native]) == counting.count([native])


# ---- masking helpers ---------------------------------------------------------

def test_mask_stub_copies_host_fields_only_and_keeps_pair_identity():
    _, result = tool_exchange(1)
    stub = masked_tool_message(result, RUN)
    data = json.loads(stub.content)
    assert (stub.id, stub.tool_call_id, stub.name, stub.status) == (result.id, result.tool_call_id,
                                                                     result.name, result.status)
    assert data["masked"] is True and data["artifact_ids"] == ["art-1"]
    assert data["path"] == "f1.py" and data["sha256"] == f"{1:064x}"
    assert "content" not in data and "源码" not in stub.content and "artifact_read" in data["hint"]
    assert masked_tool_message(stub, RUN) is None  # never masked twice


@pytest.mark.parametrize("kwargs", [{"artifacts": False}, {"run_id": "another-run"}])
def test_results_without_own_run_artifacts_are_never_masked(kwargs):
    _, result = tool_exchange(1, **kwargs)
    assert masked_tool_message(result, RUN) is None


def test_non_json_short_and_non_tool_messages_are_never_masked():
    assert masked_tool_message(HumanMessage(id="h", content="{}"), RUN) is None
    assert masked_tool_message(ToolMessage(id="t", tool_call_id="c", content="plain text"), RUN) is None
    short = ToolMessage(id="t", tool_call_id="c", name="workspace_list", content=json.dumps(
        {"artifacts": [{"id": "a", "run_id": RUN}]}))
    assert masked_tool_message(short, RUN) is None  # a stub would not be shorter


def test_artifact_read_results_can_be_masked_back_to_their_reference():
    call = AIMessage(id="ai-r", content="", tool_calls=[{"id": "r", "name": "artifact_read", "args": {}}])
    result = ToolMessage(id="tool-r", name="artifact_read", tool_call_id="r", content=json.dumps(
        {"artifact_id": "art-9", "sha256": "x", "content": "日志" * 2000, "offset": 0, "next_offset": 4000}))
    stub = json.loads(masked_tool_message(result, RUN).content)
    assert stub["artifact_ids"] == ["art-9"] and stub["next_offset"] == 4000
    assert mask_older_tool_results([call, result], RUN, 1) == [call, result]


def test_newest_exchanges_stay_verbatim_and_pairs_remain_valid():
    messages = list(history(6))
    masked = mask_older_tool_results(messages, RUN, 4)
    interaction_groups(masked)
    changed = [old.id for old, new in zip(messages, masked) if old is not new]
    assert changed == ["tool-0", "tool-1"]
    assert masked[-8:] == messages[-8:]
    assert all(old is new for old, new in zip(messages, masked) if isinstance(old, AIMessage))


# ---- prepare_context ---------------------------------------------------------

@pytest.mark.asyncio
async def test_masking_avoids_a_paid_summary_and_leaves_originals_unchanged():
    messages = history(10, size=1200)
    before = [message.model_dump() for message in messages]
    host = HostExecutor()
    prepared = await prepare(v3_request(messages, input_target_tokens=16000), host)
    assert not host.requests and not prepared.compacted
    assert "tool_results_masked" in prepared.decisions
    assert prepared.masked_message_ids == tuple(f"tool-{index}" for index in range(6))
    assert prepared.masked_tokens_saved > 0 and prepared.input_tokens < prepared.input_limit
    assert [message.model_dump() for message in messages] == before
    assert prepared.messages[0].id == "goal" and prepared.messages[-1].id == "omni-command:latest"
    interaction_groups(list(prepared.messages))


@pytest.mark.asyncio
async def test_below_trigger_nothing_is_masked():
    host = HostExecutor()
    prepared = await prepare(v3_request(history(2, size=100)), host)
    assert not prepared.masked_message_ids and not host.requests and not prepared.decisions


@pytest.mark.asyncio
async def test_masking_disabled_keeps_v2_behaviour():
    host = HostExecutor()
    prepared = await prepare(v3_request(history(10, size=1200), input_target_tokens=16000,
                                        mask_tool_results=False), host)
    assert not prepared.masked_message_ids and host.requests and prepared.compacted


@pytest.mark.asyncio
async def test_unmaskable_history_still_uses_the_v2_summary_path():
    host = HostExecutor()
    value = replace(request(count=20, policy=ContextPolicyV3(input_target_tokens=16000)))
    prepared = await prepare(value, host)
    assert host.requests and prepared.compacted and not prepared.masked_message_ids


def incomplete():
    # DurableSummaryExecutor raises this after a completed but truncated call.
    return HostExecutor(failure=ContextPreparationError("SUMMARY_INCOMPLETE", "summary truncated"))


def soft_only_history():
    # ~15.7K estimated: above the 12.8K soft trigger, below the 16K hard limit.
    return (*history(5, size=300), *request(count=10).history_messages)


@pytest.mark.asyncio
async def test_failed_summary_degrades_when_masked_history_fits():
    host = incomplete()
    prepared = await prepare(v3_request(soft_only_history(), input_target_tokens=16000), host)
    assert len(host.requests) == 1 and not prepared.compacted and not prepared.summaries
    assert "summary_degraded:SUMMARY_INCOMPLETE" in prepared.decisions
    assert prepared.input_tokens <= prepared.input_limit
    interaction_groups(list(prepared.messages))


@pytest.mark.asyncio
async def test_summary_failure_fail_option_keeps_v2_stop():
    with pytest.raises(ContextPreparationError) as captured:
        await prepare(v3_request(soft_only_history(), input_target_tokens=16000, summary_failure="fail"),
                      incomplete())
    assert captured.value.code == "SUMMARY_INCOMPLETE"


@pytest.mark.asyncio
async def test_degradation_never_bypasses_the_hard_input_limit():
    value = replace(request(count=30, policy=ContextPolicyV3(input_target_tokens=16000)))
    with pytest.raises(ContextPreparationError) as captured:
        await prepare(value, incomplete())
    assert captured.value.code == "SUMMARY_INCOMPLETE"


@pytest.mark.asyncio
async def test_unknown_or_failed_summary_call_is_not_degraded():
    with pytest.raises(RuntimeError):
        await prepare(v3_request(soft_only_history(), input_target_tokens=16000),
                      HostExecutor(failure=RuntimeError("result unknown")))


@pytest.mark.asyncio
async def test_summary_integrity_failure_is_not_degraded():
    def forged(stored: StoredSummary):
        return replace(stored, invocation_id="another-call")
    with pytest.raises(ContextPreparationError) as captured:
        await prepare(v3_request(soft_only_history(), input_target_tokens=16000), HostExecutor(mutate=forged))
    assert captured.value.code == "SUMMARY_EVIDENCE"


@pytest.mark.asyncio
async def test_after_degradation_soft_trigger_does_not_pay_again():
    host = HostExecutor()
    prepared = await prepare(replace(v3_request(soft_only_history(), input_target_tokens=16000),
                                     soft_summary=False), host)
    assert not host.requests and not prepared.compacted


@pytest.mark.asyncio
async def test_protected_goal_and_requirements_are_never_masked():
    call, result = tool_exchange(99, size=4000)
    value = replace(v3_request(history(6, size=1500), input_target_tokens=16000),
                    prefix_messages=(HumanMessage(id="goal", content="Original objective"), call, result))
    prepared = await prepare(value, HostExecutor())
    assert "tool-99" not in prepared.masked_message_ids
    assert next(m for m in prepared.messages if m.id == "tool-99").content == result.content
