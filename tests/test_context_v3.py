"""context-v3: deterministic tool-result masking, reasoning-aware counting and
bounded degradation. Offline only; the durable host adapter is a test double."""
import json
from dataclasses import asdict, replace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_context_runtime import HostExecutor, prepare, request

from omniharness.artifacts import ArtifactStore
from omniharness.context_admission import context_admission_diagnostic, safe_admission_diagnostic
from omniharness.context_policy import (
    ContextPolicy,
    ContextPolicyError,
    ContextPolicyV2,
    ContextPolicyV3,
    ModelContextLimits,
    UnicodeContextCounter,
    context_configuration_dict,
    context_policy_dict,
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
from omniharness.models import ArtifactVersion

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
    assert v3.pressure_mask_keep_recent_exchanges is None
    assert (v3.max_messages, v3.max_characters) == (100, 200_000)
    assert parse_context_configuration(context_configuration_dict(v3, limits))[0] == v3
    # v2 serialization did not gain v3 fields.
    assert "mask_tool_results" not in asdict(ContextPolicyV2())


@pytest.mark.parametrize("change", [
    {"replayed_reasoning": "auto"}, {"mask_tool_results": 1}, {"mask_keep_recent_exchanges": 0},
    {"mask_keep_recent_exchanges": 21}, {"mask_keep_recent_exchanges": True}, {"summary_failure": "retry"},
    {"summary_recovery_attempts": 2},  # inherited v2 bound still applies
    {"pressure_mask_keep_recent_exchanges": True}, {"pressure_mask_keep_recent_exchanges": -1},
    {"pressure_mask_keep_recent_exchanges": 4}, {"pressure_mask_keep_recent_exchanges": 1.5},
])
def test_v3_rejects_unbounded_or_unknown_values(change):
    with pytest.raises(ContextPolicyError):
        replace(ContextPolicyV3(), **change)


def test_v2_payload_with_v3_fields_is_rejected():
    with pytest.raises(ContextPolicyError):
        parse_context_configuration({"policy": {"version": "context-v2", "summary_failure": "degrade"}})


@pytest.mark.parametrize("keep", [0, 1, 3])
def test_pressure_mask_is_an_explicit_frozen_v3_setting(keep):
    value = {"policy": {"version": "context-v3", "pressure_mask_keep_recent_exchanges": keep}}
    policy, limits = parse_context_configuration(value)
    assert policy.pressure_mask_keep_recent_exchanges == keep
    assert parse_context_configuration(context_configuration_dict(policy, limits)) == (policy, limits)
    with pytest.raises(ContextPolicyError):
        parse_context_configuration({"policy": {**value["policy"], "version": "context-v2"}})


def test_disabled_pressure_mask_keeps_the_pre_extension_canonical_policy():
    legacy = asdict(ContextPolicyV3())
    legacy.pop("pressure_mask_keep_recent_exchanges")
    assert context_policy_dict(ContextPolicyV3()) == legacy
    assert context_policy_dict(ContextPolicyV3(pressure_mask_keep_recent_exchanges=0)) == {
        **legacy, "pressure_mask_keep_recent_exchanges": 0,
    }


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


@pytest.mark.asyncio
async def test_explicit_long_history_envelope_admits_150_short_natural_messages_without_summary():
    messages = tuple((HumanMessage if index % 2 == 0 else AIMessage)(
        id=f"step-{index}", content=f"Step {index}: inspect the module, then verify the stated requirement.",
    ) for index in range(150))
    host = HostExecutor()
    prepared = await prepare(v3_request(messages, max_messages=200, max_characters=400_000), host)
    assert not host.requests and not prepared.compacted and not prepared.masked_message_ids
    assert [message.id for message in prepared.messages[1:-1]] == [message.id for message in messages]
    assert len(prepared.source_manifest) == 152
    assert prepared.input_tokens < prepared.input_limit


@pytest.mark.asyncio
async def test_explicit_long_character_envelope_is_not_shadowed_by_old_200k_guard():
    text = "def calculate(value): return value + 1\n" * 6500
    assert len(text) > 200_000
    message = HumanMessage(id="long-source", content=text)
    host = HostExecutor()
    prepared = await prepare(v3_request([message], input_target_tokens=128_000,
                                        max_characters=600_000), host)
    assert not host.requests and not prepared.compacted
    assert prepared.messages[1].content == text
    assert prepared.input_limit == 128_000 and prepared.input_tokens < prepared.input_limit


@pytest.mark.asyncio
async def test_pressure_mask_is_not_used_for_only_a_soft_trigger():
    messages = history(2, size=3200)
    value = replace(v3_request(messages, input_target_tokens=16_000,
                               pressure_mask_keep_recent_exchanges=0), soft_summary=False)
    host = HostExecutor()
    prepared = await prepare(value, host)
    assert prepared.input_tokens >= int(prepared.input_limit * value.policy.summarize_at)
    assert not prepared.masked_message_ids and not prepared.decisions and not host.requests
    assert [message.content for message in prepared.messages[1:-1]] == [message.content for message in messages]


@pytest.mark.asyncio
async def test_recent_large_saved_artifact_pressure_mask_and_real_paged_readback(environment):
    from conftest import create_chat

    settings, _, factory, client = environment
    _, _, run = create_chat(client)
    run_id = run["run_id"]
    store = ArtifactStore(settings.data_dir / "context-pressure-test")
    original = json.dumps({"path": "large.py", "content": "模块实现\n" * 5000}, ensure_ascii=False)
    with factory.begin() as db:
        reference = store.put(db, run_id=run_id, kind="tool_result", name="large.json", content=original)
    call, result = tool_exchange(0, size=0)
    result = result.model_copy(update={"content": json.dumps(
        {**json.loads(original), "artifacts": [reference]}, ensure_ascii=False)})
    value = v3_request([call, result], input_target_tokens=4096, pressure_mask_keep_recent_exchanges=0)
    value = replace(value, scope=replace(value.scope, run_id=run_id))
    before = result.model_dump()
    host = HostExecutor()
    prepared = await prepare(value, host)
    assert not host.requests and not prepared.compacted
    assert prepared.decisions == ("tool_results_pressure_masked",)
    assert prepared.masked_message_ids == ("tool-0",) and prepared.masked_tokens_saved > 0
    stub = json.loads(prepared.messages[2].content)
    assert stub["artifact_ids"] == [reference["id"]] and "artifact_read" in stub["hint"]
    assert prepared.messages[0] == value.prefix_messages[0] and prepared.messages[-1] == value.suffix_messages[0]
    assert result.model_dump() == before
    interaction_groups(list(prepared.messages))
    with factory() as db:
        row = db.get(ArtifactVersion, stub["artifact_ids"][0])
        pages, offset = [], 0
        while offset < row.size_bytes:
            page, offset = store.read_text_page(row, run_id=run_id, offset=offset, limit=4097)
            pages.append(page)
    assert len(pages) > 1 and "".join(pages) == original


@pytest.mark.asyncio
async def test_ordinary_and_pressure_mask_savings_and_ids_are_not_double_counted():
    messages = history(6, size=3000)
    value = v3_request(messages, input_target_tokens=4096, pressure_mask_keep_recent_exchanges=0)
    host = HostExecutor()
    prepared = await prepare(value, host)
    assert not host.requests and not prepared.compacted
    assert prepared.decisions == ("tool_results_masked", "tool_results_pressure_masked")
    assert prepared.masked_message_ids == tuple(f"tool-{index}" for index in range(6))
    counter = counter_for_policy(value.policy)
    before = counter.count([value.system_message, *value.prefix_messages, *messages, *value.suffix_messages])
    assert prepared.masked_tokens_saved == before - prepared.input_tokens
    interaction_groups(list(prepared.messages))


@pytest.mark.asyncio
async def test_pressure_mask_default_still_leaves_recent_large_results_unmasked():
    host = HostExecutor()
    with pytest.raises(ContextPreparationError) as captured:
        await prepare(v3_request(history(1, size=5000), input_target_tokens=4096), host)
    assert captured.value.code == "CONTEXT_GROUP_LIMIT" and not host.requests
    assert captured.value.diagnostic["reason"] == "input_tokens"


@pytest.mark.asyncio
async def test_pressure_mask_keeps_its_explicit_recent_exchange_and_parallel_pairs():
    messages = history(6, size=3000)
    host = HostExecutor()
    prepared = await prepare(v3_request(messages, input_target_tokens=16_000,
                                        pressure_mask_keep_recent_exchanges=1), host)
    assert not host.requests and not prepared.compacted
    assert prepared.masked_message_ids == tuple(f"tool-{index}" for index in range(5))
    assert prepared.messages[-2].content == messages[-1].content
    first_call, first_result = tool_exchange(20)
    second_call, second_result = tool_exchange(21)
    parallel_call = first_call.model_copy(update={"tool_calls": first_call.tool_calls + second_call.tool_calls})
    prepared = await prepare(v3_request([parallel_call, first_result, second_result],
                                        input_target_tokens=4096, pressure_mask_keep_recent_exchanges=0), host)
    assert not host.requests and prepared.masked_message_ids == ("tool-20", "tool-21")
    groups = interaction_groups(list(prepared.messages))
    assert len(groups[1]) == 3 and groups[1][0].tool_calls == parallel_call.tool_calls


@pytest.mark.parametrize("change", [
    {"state": "UNKNOWN"}, {"status": "uncertain"}, {"error": "RESULT_UNKNOWN"},
    {"error_code": "RESULT_UNKNOWN"}, {"output_status": "partial"}, {"result_status": "running"},
    {"complete": False},
])
def test_pressure_mask_never_hides_unfinished_or_unknown_result(change):
    call, result = tool_exchange(0)
    data = {**json.loads(result.content), **change}
    result = result.model_copy(update={"content": json.dumps(data, ensure_ascii=False)})
    assert mask_older_tool_results([call, result], RUN, 0, require_completed=True) == [call, result]


def test_pressure_mask_rejects_partial_artifact_even_with_successful_tool_message():
    _, result = tool_exchange(0)
    data = json.loads(result.content)
    data["artifacts"][0]["metadata_json"] = {"complete": False}
    result = result.model_copy(update={"content": json.dumps(data, ensure_ascii=False)})
    assert masked_tool_message(result, RUN, require_completed=True) is None
    assert masked_tool_message(result.model_copy(update={"status": "error"}), RUN, require_completed=True) is None


@pytest.mark.asyncio
async def test_opted_in_ordinary_mask_also_preserves_older_unknown_results():
    messages = list(history(6, size=300))
    unknown = json.loads(messages[1].content)
    unknown["state"] = "UNKNOWN"
    messages[1] = messages[1].model_copy(update={"content": json.dumps(unknown, ensure_ascii=False)})
    host = HostExecutor()
    value = replace(v3_request(messages, input_target_tokens=4096,
                               pressure_mask_keep_recent_exchanges=0), soft_summary=False)
    prepared = await prepare(value, host)
    assert not host.requests and "tool-0" not in prepared.masked_message_ids
    assert prepared.messages[2].content == messages[1].content


@pytest.mark.parametrize("kwargs", [{"artifacts": False}, {"run_id": "another-run"}])
def test_pressure_mask_requires_own_saved_artifact(kwargs):
    call, result = tool_exchange(0, **kwargs)
    assert mask_older_tool_results([call, result], RUN, 0, require_completed=True) == [call, result]


@pytest.mark.asyncio
async def test_pressure_mask_cannot_bypass_incomplete_tool_pair_or_protected_requirement():
    call, result = tool_exchange(0, size=5000)
    host = HostExecutor()
    with pytest.raises(ContextPreparationError) as captured:
        await prepare(v3_request([call], pressure_mask_keep_recent_exchanges=0), host)
    assert captured.value.code == "CONTEXT_TOOL_PAIR" and not host.requests
    value = replace(v3_request([], input_target_tokens=4096, pressure_mask_keep_recent_exchanges=0),
                    prefix_messages=(HumanMessage(id="goal", content="Immutable objective"), call, result))
    with pytest.raises(ContextPreparationError) as captured:
        await prepare(value, host)
    assert captured.value.code == "CONTEXT_LIMIT" and not host.requests
    assert captured.value.diagnostic == {
        "reason": "input_tokens", "observed": counter_for_policy(value.policy).count(
            [value.system_message, *value.prefix_messages, *value.suffix_messages]),
        "limit": 4096, "window_known": False,
    }


@pytest.mark.asyncio
async def test_real_known_window_still_bounds_explicit_long_policy_without_summary():
    secret = "private protected objective " * 450
    host = HostExecutor()
    value = replace(v3_request([], input_target_tokens=128_000, max_messages=256, max_characters=600_000),
                    prefix_messages=(HumanMessage(id="goal", content=secret),), main_output_tokens=1000,
                    model_limits=ModelContextLimits(context_window_tokens=5500))
    with pytest.raises(ContextPreparationError) as captured:
        await prepare(value, host)
    diagnostic = captured.value.diagnostic
    assert diagnostic["reason"] == "input_tokens" and diagnostic["limit"] == 3476
    assert diagnostic["observed"] > diagnostic["limit"] and diagnostic["window_known"] is True
    assert set(diagnostic) == {"reason", "observed", "limit", "window_known"}
    assert secret not in str(captured.value) + json.dumps(diagnostic)
    assert not host.requests


@pytest.mark.parametrize("override, expected", [
    ({"message_count": 101}, {"reason": "message_count", "observed": 101, "limit": 100}),
    ({"serialized_characters": 200_001},
     {"reason": "serialized_characters", "observed": 200_001, "limit": 200_000}),
    ({"input_tokens": 32_001}, {"reason": "input_tokens", "observed": 32_001, "limit": 32_000}),
])
def test_admission_diagnostic_is_bounded_and_reports_frozen_guard(override, expected):
    counts = {"input_tokens": 1, "input_limit": 32_000, "message_count": 1, "message_limit": 100,
              "serialized_characters": 1, "character_limit": 200_000, "window_known": False}
    assert context_admission_diagnostic(**counts) is None
    assert context_admission_diagnostic(**{**counts, **override}) == {**expected, "window_known": False}


@pytest.mark.parametrize("change", [
    {"body": "private conversation"}, {"reason": "private conversation"}, {"observed": "private conversation"},
    {"observed": True}, {"limit": 0}, {"limit": -1}, {"observed": float("inf")},
    {"observed": 2**63}, {"observed": 100}, {"window_known": 1},
])
def test_persisted_admission_diagnostic_rejects_arbitrary_or_unbounded_detail(change):
    detail = {"reason": "message_count", "observed": 101, "limit": 100, "window_known": False}
    assert safe_admission_diagnostic({**detail, **change}) is None
    assert ContextPreparationError("CONTEXT_LIMIT", "Input rejected", diagnostic={**detail, **change}).diagnostic is None


def test_persisted_admission_diagnostic_copies_only_exact_rejection_facts():
    detail = {"reason": "message_count", "observed": 101, "limit": 100, "window_known": False}
    safe = safe_admission_diagnostic(detail)
    assert safe == detail and safe is not detail
    detail["observed"] = 999
    assert safe["observed"] == 101
    assert safe_admission_diagnostic(None) is None
