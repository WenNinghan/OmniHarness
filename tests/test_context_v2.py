from dataclasses import asdict, replace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_context_runtime import HostExecutor, prepare, request

from omniharness.context_policy import (
    ApproximateContextCounter,
    ContextPolicy,
    ContextPolicyError,
    ContextPolicyV2,
    UnicodeContextCounter,
    context_configuration_dict,
    counter_for_policy,
    parse_context_configuration,
)
from omniharness.context_runtime import ContextPreparationError, interaction_groups, public_message


def test_v2_is_explicit_and_v1_serialized_policy_is_unchanged():
    old, limits = parse_context_configuration({})
    assert type(old) is ContextPolicy and type(counter_for_policy(old)) is ApproximateContextCounter
    assert asdict(old) == {
        "version": "context-v1", "input_target_tokens": 16000, "summarize_at": .8,
        "summary_output_tokens": 4000, "keep_recent_tokens": 4000,
        "window_margin_tokens": 1024, "max_messages": 100, "max_characters": 200000,
    }
    new, _ = parse_context_configuration({"policy": {"version": "context-v2"}})
    assert new == ContextPolicyV2() and new.summary_target_tokens == 1200
    assert parse_context_configuration(context_configuration_dict(new, limits))[0] == new


@pytest.mark.parametrize("change", [
    {"summary_target_tokens": 4001}, {"summary_min_gain": 0}, {"summary_min_gain": True},
    {"summary_min_new_tokens": False}, {"summary_recovery_attempts": 2},
    {"summary_recovery_attempts": True}, {"summary_recovery_output_tokens": 16000},
    {"summary_recovery_output_tokens": 3000}, {"post_summary_target": .8},
    {"summary_thinking": "auto_switch"}, {"counter_profile": "new"},
])
def test_v2_rejects_unapproved_or_unbounded_policies(change):
    with pytest.raises(ContextPolicyError):
        replace(ContextPolicyV2(), **change)


def test_unicode_estimate_includes_full_native_protocol_and_tools():
    counter = UnicodeContextCounter()
    assert counter.estimated and counter.method == "unicode-weighted-v2"
    ascii_count = counter.count([HumanMessage(content="a" * 300)])
    cjk_count = counter.count([HumanMessage(content="字" * 300)])
    emoji_count = counter.count([HumanMessage(content="🧪" * 300)])
    assert 100 < ascii_count < 120 and 300 < cjk_count < 320 and emoji_count > 1200
    public = AIMessage(content="", tool_calls=[{"id": "c", "name": "read", "args": {"路径": "项目"}}])
    native = public.model_copy(update={"additional_kwargs": {"reasoning_content": "私有" * 600}})
    assert counter.count([native]) - counter.count([public]) == 1200
    assert counter.count([native], tools=({"description": "完整工具定义" * 100},)) > counter.count([native])


@pytest.mark.asyncio
async def test_v2_prompt_is_concise_and_reentry_across_transient_nodes_reuses_exact_sources():
    value = request(count=16, policy=ContextPolicyV2())
    host = HostExecutor()
    first = await prepare(value, host)
    count = len(host.requests)
    second = await prepare(replace(value, scope=replace(value.scope, task_id="next-turn", checkpoint_ns="next")), host)
    assert count and len(host.requests) == count and first.summary_refs == second.summary_refs
    assert all("at most 1200 estimated tokens" in item.messages[0].content for item in host.requests)
    assert all(item.output_tokens == 4000 for item in host.requests)
    changed = replace(value, prefix_messages=(HumanMessage(id="goal", content="A different task"),))
    await prepare(changed, host)
    assert len(host.requests) > count


@pytest.mark.asyncio
@pytest.mark.parametrize("new_size", [0, 100])
async def test_old_summary_without_sufficient_new_sources_never_causes_a_paid_call(new_size):
    history = [HumanMessage(id="omni-summary:existing", content="字" * 13500)]
    if new_size:
        history.append(HumanMessage(id="new", content="新" * new_size))
    host = HostExecutor()
    output = await prepare(replace(request(count=0, policy=ContextPolicyV2()), history_messages=tuple(history)), host)
    assert not host.requests and not output.compacted
    if new_size:
        assert output.decisions == ("insufficient_new_sources",)


@pytest.mark.asyncio
async def test_only_summary_cannot_bypass_hard_input_limit():
    host = HostExecutor()
    value = replace(request(count=0, policy=ContextPolicyV2()), history_messages=(
        HumanMessage(id="omni-summary:existing", content="字" * 17000), HumanMessage(id="tail", content="done")))
    with pytest.raises(ContextPreparationError, match="exceeds"):
        await prepare(value, host)
    assert not host.requests


@pytest.mark.asyncio
async def test_no_gain_keeps_original_history_and_reentry_reuses_saved_evidence_without_paying():
    host = HostExecutor(mutate=lambda stored: replace(stored, text="字" * 12000, usage={"output_tokens": 3000}))
    value = request(count=12, policy=ContextPolicyV2())
    output = await prepare(value, host)
    assert not output.compacted and output.decisions == ("insufficient_compression_gain",)
    assert len(host.requests) == 1 and output.summaries
    again = await prepare(replace(value, scope=replace(value.scope, checkpoint_ns="restarted")), host)
    assert again.messages == output.messages and len(host.requests) == 1


@pytest.mark.asyncio
async def test_native_reasoning_affects_full_window_admission_but_never_enters_summary():
    call = AIMessage(id="a", content="read", tool_calls=[{"id": "c", "name": "read", "args": {}}],
                     additional_kwargs={"reasoning_content": "私" * 17000})
    result = ToolMessage(id="t", content="Actual evidence", tool_call_id="c")
    # The entire pair is protected: admission must reject rather than drop its native payload.
    value = replace(request(count=0, policy=ContextPolicyV2()), prefix_messages=(call, result),
                    native_tool_messages=(call,))
    host = HostExecutor()
    with pytest.raises(ContextPreparationError, match="exceeds"):
        await prepare(value, host)
    assert not host.requests
    value = replace(request(count=16, policy=ContextPolicyV2()),
        history_messages=(call, result, *request(count=16).history_messages), native_tool_messages=(call,))
    prepared = await prepare(value, host)
    assert prepared.compacted
    assert all("私" not in item.messages[0].content for item in host.requests)
    assert "私" not in str(prepared.messages)


@pytest.mark.asyncio
async def test_unmatched_native_source_is_rejected():
    native = AIMessage(id="not-present", content="", tool_calls=[{"id": "c", "name": "read", "args": {}}])
    with pytest.raises(ContextPreparationError, match="differs"):
        await prepare(replace(request(count=0, policy=ContextPolicyV2()), native_tool_messages=(native,)))


@pytest.mark.asyncio
async def test_prepared_context_accepts_a_bound_recovered_result_with_its_own_cap():
    def recovered(summary):
        return replace(summary, invocation_id="recovery-call", binding_fingerprint="recovery-binding",
                       recovery_of=summary.invocation_id, request_binding=summary.binding_fingerprint,
                       output_limit=8000, usage={"output_tokens": 5000})
    result = await prepare(request(count=12, policy=ContextPolicyV2()), HostExecutor(mutate=recovered))
    assert result.compacted and result.summaries[0].output_limit == 8000


def native_exchange(identity, native_size):
    call = AIMessage(id=f"ai-{identity}", content="",
        tool_calls=[{"id": identity, "name": "read", "args": {"path": f"{identity}.py"}}],
        additional_kwargs={"reasoning_content": "私" * native_size})
    result = ToolMessage(id=f"tool-{identity}", tool_call_id=identity,
                         content="Persisted source and actual result. " * 20)
    return call, result


@pytest.mark.asyncio
@pytest.mark.parametrize("latest_size", [11000, 18000])
async def test_v2_oversized_complete_tail_is_summarized_with_all_sources_and_no_repeated_call(latest_size):
    # Entirely synthetic protocol payload: never copy provider reasoning into fixtures.
    first, latest = native_exchange("first", 4000), native_exchange("latest", latest_size)
    history = (*first, *latest)
    originals = [message.model_dump() for message in history]
    value = replace(request(count=0, policy=ContextPolicyV2()), history_messages=history,
                    native_tool_messages=(first[0], latest[0]), artifact_refs=("saved-tool-log",))
    host = HostExecutor()
    prepared = await prepare(value, host)

    assert prepared.compacted and len(host.requests) == 1
    assert prepared.input_tokens < 16000 * value.policy.post_summary_target
    assert {source.message_id for source in host.requests[0].sources} == {m.id for m in history}
    assert all(source.sha256 and source.artifact_refs == ("saved-tool-log",)
               for source in host.requests[0].sources)
    assert prepared.messages[0] == value.prefix_messages[0]
    assert prepared.messages[-1] == value.suffix_messages[0]
    interaction_groups(list(prepared.messages))
    assert all("私" not in request.messages[0].content for request in host.requests)
    assert [message.model_dump() for message in history] == originals

    # The resulting summary, without new history, must not trigger another paid call.
    again = await prepare(replace(value, history_messages=prepared.messages[1:-1], native_tool_messages=()), host)
    assert not again.compacted and len(host.requests) == 1


@pytest.mark.asyncio
async def test_v2_retains_small_native_tail_and_summarizes_only_whole_older_exchange():
    old, recent = native_exchange("old", 17000), native_exchange("recent", 200)
    host = HostExecutor()
    value = replace(request(count=0, policy=ContextPolicyV2()), history_messages=(*old, *recent),
                    native_tool_messages=(old[0], recent[0]))
    prepared = await prepare(value, host)
    assert {source.message_id for source in host.requests[0].sources} == {m.id for m in old}
    assert [m.id for m in prepared.messages][-3:-1] == [m.id for m in recent]
    assert all(m.id in {source.message_id for source in prepared.source_manifest} for m in (*old, *recent))
    interaction_groups(list(prepared.messages))


@pytest.mark.asyncio
async def test_v1_retention_ignores_v2_native_counter_extension():
    call, result = native_exchange("legacy", 18000)
    host = HostExecutor()
    value = replace(request(count=0, policy=ContextPolicy()), history_messages=(call, result),
                    native_tool_messages=(call,))
    prepared = await prepare(value, host)
    assert not prepared.compacted and not host.requests
    assert [m.id for m in prepared.messages][1:-1] == [call.id, result.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_summary", [False, True])
async def test_v2_gain_and_freshness_measure_native_transport_not_short_public_projection(prior_summary):
    call = AIMessage(id="short-public", content="",
        tool_calls=[{"id": "listing", "name": "workspace_list", "args": {}}],
        additional_kwargs={"reasoning_content": "私" * 18000})
    result = ToolMessage(id="listing-result", tool_call_id="listing", content="config.py")
    history = ((HumanMessage(id="omni-summary:previous", content="Earlier progress."),) if prior_summary else ())
    history = (*history, call, result)
    counter = UnicodeContextCounter()
    summary_text = "公开证据" * 100
    assert counter.count([public_message(call), result]) < ContextPolicyV2().summary_min_new_tokens
    assert counter.count([HumanMessage(content=summary_text)]) > counter.count([public_message(m) for m in history])
    host = HostExecutor(mutate=lambda stored: replace(stored, text=summary_text, usage={"output_tokens": 400}))
    value = replace(request(count=0, policy=ContextPolicyV2()), history_messages=history,
                    native_tool_messages=(call,))

    prepared = await prepare(value, host)
    assert prepared.compacted and len(host.requests) == 1
    assert not prepared.decisions and prepared.input_tokens < value.policy.input_target_tokens
    assert {source.message_id for source in host.requests[0].sources} == {m.id for m in history}
    assert "私" not in host.requests[0].messages[0].content
    again = await prepare(replace(value, history_messages=prepared.messages[1:-1], native_tool_messages=()), host)
    assert not again.compacted and len(host.requests) == 1
