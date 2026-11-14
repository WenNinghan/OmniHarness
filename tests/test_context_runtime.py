import asyncio
from dataclasses import replace

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from omniharness.context_policy import (
    ApproximateContextCounter,
    ContextPolicyError,
    ModelContextLimits,
)
from omniharness.context_runtime import (
    ContextInput,
    ContextPreparationError,
    ContextScope,
    StoredSummary,
    count_summary_output,
    interaction_groups,
    prepare_context,
    validate_summary_output,
)


class HostExecutor:
    """Offline host double: the product durable adapter is tested separately."""
    def __init__(self, *, available=50, failure=None, mutate=None):
        self.available, self.failure, self.mutate = available, failure, mutate
        self.requests, self.saved = [], {}
        self.lock = asyncio.Lock()
        self.stopped = False

    async def execute(self, request):
        async with self.lock:
            if self.stopped:
                raise RuntimeError("pause/cancel at host boundary")
            if request.invocation_id in self.saved:
                saved = self.saved[request.invocation_id]
                assert saved.binding_fingerprint == request.binding_fingerprint
                return saved
            if not self.available:
                raise RuntimeError("shared model budget exhausted")
            self.available -= 1
            self.requests.append(request)
            if self.failure:
                raise self.failure
            stored = StoredSummary("artifact-" + request.invocation_id, request.invocation_id,
                                   request.binding_fingerprint, "History summarized; evidence is retained.",
                                   request.sources, usage={"input_tokens": request.input_tokens, "output_tokens": 10})
            if self.mutate:
                stored = self.mutate(stored)
            self.saved[request.invocation_id] = stored
            return stored


def request(*, size=1200, count=20, **kwargs):
    messages = tuple(HumanMessage(id=f"source-{index}", content=f"History {index}: " + "测" * size)
                     for index in range(count))
    return ContextInput(ContextScope("run-1", "execute", "model-1", "frozen-model"), messages,
                        prefix_messages=(HumanMessage(id="goal", content="Original objective"),),
                        suffix_messages=(HumanMessage(id="omni-command:latest", content="Keep the latest requirement"),),
                        system_message=SystemMessage(content="Host policy"), **kwargs)


async def prepare(value, host=None):
    return await prepare_context(value, model=FakeListChatModel(responses=["must not be invoked directly"]),
                                 summary_executor=host or HostExecutor())


@pytest.mark.asyncio
async def test_below_threshold_is_no_call_and_preserves_public_messages():
    host = HostExecutor()
    value = request(size=10, count=2)
    result = await prepare(value, host)
    assert not host.requests and not result.compacted and not result.summary_refs
    assert [m.id for m in result.messages] == ["goal", "source-0", "source-1", "omni-command:latest"]
    assert result.input_limit == 16000 and result.counting_is_estimate
    assert value.main_output_tokens == 100000


@pytest.mark.asyncio
async def test_sdk_summary_keeps_protected_requirements_and_complete_source_mapping():
    host = HostExecutor()
    value = request(artifact_refs=("immutable-test-log-v1",))
    before = [message.model_dump() for message in value.history_messages]
    result = await prepare(value, host)
    assert result.compacted and result.input_tokens < 16000
    assert result.messages[0] == value.prefix_messages[0] and result.messages[-1] == value.suffix_messages[0]
    assert [message.model_dump() for message in value.history_messages] == before
    assert len(result.source_manifest) == len(value.history_messages) + 2
    summarized_ids = {source.message_id for source in result.summaries[-1].sources}
    remaining_ids = {message.id for message in result.messages}
    assert {message.id for message in value.history_messages} <= summarized_ids | remaining_ids
    assert "goal" not in summarized_ids and "omni-command:latest" not in summarized_ids
    for summary in host.requests:
        assert summary.purpose == "context_summary" and summary.output_tokens == 4000
        assert summary.input_tokens <= summary.input_limit
        assert summary.artifact_refs == ("immutable-test-log-v1",)
        assert all(source.sha256 and source.message_id for source in summary.sources)
    assert result.messages[1].id == "omni-summary:" + result.summaries[-1].artifact_id


@pytest.mark.asyncio
async def test_long_history_is_batched_without_implicit_trimming():
    host = HostExecutor()
    result = await prepare(request(count=80), host)
    assert len(host.requests) > 2
    for before, after in zip(host.requests, host.requests[1:]):
        assert set(before.sources) < set(after.sources)
        assert after.parent_summary_refs == (host.saved[before.invocation_id].artifact_id,)
    assert len(result.summaries[-1].sources) + sum(m.id.startswith("source-") for m in result.messages) == 80


@pytest.mark.asyncio
async def test_completed_matching_summary_is_reused_on_prepare_reentry():
    host = HostExecutor()
    value = request()
    first = await prepare(value, host)
    charged = len(host.requests)
    second = await prepare(value, host)
    assert len(host.requests) == charged
    assert first.messages == second.messages and first.summary_refs == second.summary_refs


@pytest.mark.asyncio
async def test_model_and_policy_change_produce_new_bound_requests():
    host = HostExecutor()
    value = request()
    first = await prepare(value, host)
    second = await prepare(replace(value, policy=replace(value.policy, window_margin_tokens=2048)), host)
    third = await prepare(replace(value, scope=replace(value.scope, model_fingerprint="changed")), host)
    assert set(first.summary_refs).isdisjoint(second.summary_refs)
    assert set(first.summary_refs).isdisjoint(third.summary_refs)


@pytest.mark.asyncio
async def test_private_reasoning_removed_but_tool_calls_and_pairs_remain():
    messages = (
        HumanMessage(id="u", content="Read source"),
        AIMessage(id="a", content=[{"type": "thinking", "thinking": "PRIVATE reasoning"},
                                   {"type": "text", "text": "Reading source"}],
                  additional_kwargs={"reasoning_content": "PRIVATE extra", "signature": "PRIVATE signature"},
                  tool_calls=[{"name": "read", "args": {"path": "src.py"}, "id": "call-1", "type": "tool_call"}]),
        ToolMessage(id="t", content="Source text", tool_call_id="call-1"),
    )
    value = replace(request(count=0), history_messages=messages)
    result = await prepare(value)
    assistant = next(m for m in result.messages if isinstance(m, AIMessage))
    assert assistant.tool_calls == messages[1].tool_calls and assistant.additional_kwargs == {}
    assert "PRIVATE" not in str([m.model_dump() for m in result.messages])
    assert interaction_groups(list(result.messages))
    assert "PRIVATE" in str(messages[1].model_dump())  # Raw evidence untouched.


@pytest.mark.asyncio
async def test_summary_prompt_does_not_lose_long_structured_tool_content_or_reasoning():
    call = AIMessage(id="call", content="", additional_kwargs={"reasoning_content": "PRIVATE"},
                     tool_calls=[{"name": "inspect", "args": {"json": "x" * 1200},
                                  "id": "call-1", "type": "tool_call"}])
    tool = ToolMessage(id="tool", tool_call_id="call-1", content=[{"type": "text", "text": "y" * 1200}])
    host = HostExecutor()
    value = request(count=25)
    result = await prepare(replace(value, history_messages=(call, tool, *value.history_messages)), host)
    prompts = "\n".join(req.messages[0].content for req in host.requests)
    assert "x" * 1200 in prompts and "y" * 1200 in prompts and "PRIVATE" not in prompts
    assert result.compacted


@pytest.mark.asyncio
async def test_compaction_never_splits_parallel_tool_results():
    host = HostExecutor()
    value = request(count=18)
    exchange = (AIMessage(id="call", content="", tool_calls=[
        {"name": "read", "args": {}, "id": "c1"}, {"name": "read", "args": {}, "id": "c2"},
    ]), ToolMessage(id="result2", tool_call_id="c2", content="y" * 1000),
        ToolMessage(id="result1", tool_call_id="c1", content="x" * 1000))
    result = await prepare(replace(value, history_messages=(*value.history_messages, *exchange,
                                                            HumanMessage(id="recent", content="r" * 2000))), host)
    covered = {source.message_id for source in result.summaries[-1].sources}
    retained = {message.id for message in result.messages}
    assert {"call", "result1", "result2"} <= covered or {"call", "result1", "result2"} <= retained
    assert interaction_groups(list(result.messages))


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [
    (ToolMessage(id="t", content="orphan", tool_call_id="missing"),),
    (AIMessage(id="a", content="", tool_calls=[{"name": "read", "args": {}, "id": "c"}]),),
])
async def test_incomplete_tool_groups_rejected_before_summary(broken):
    host = HostExecutor()
    with pytest.raises(ContextPreparationError) as exc:
        await prepare(replace(request(count=0), history_messages=broken), host)
    assert exc.value.code == "CONTEXT_TOOL_PAIR" and not host.requests


@pytest.mark.asyncio
async def test_large_atomic_tool_group_requires_artifact_paging_without_partial_summary():
    host = HostExecutor()
    messages = (AIMessage(id="a", content="", tool_calls=[{"name": "read", "args": {}, "id": "c"}]),
                ToolMessage(id="t", content="large" * 10000, tool_call_id="c"),
                HumanMessage(id="tail", content="recent"))
    with pytest.raises(ContextPreparationError, match="complete exchange"):
        await prepare(replace(request(count=0), history_messages=messages), host)
    assert not host.requests


@pytest.mark.asyncio
async def test_protected_input_and_known_window_fail_before_any_paid_summary():
    host = HostExecutor()
    for value in (
        replace(request(), suffix_messages=(HumanMessage(id="latest", content="x" * 20000),)),
        replace(request(), model_limits=ModelContextLimits(context_window_tokens=50000)),
    ):
        with pytest.raises(ContextPolicyError):
            await prepare(value, host)
    assert not host.requests


@pytest.mark.asyncio
async def test_original_message_limit_applies_after_compaction():
    host = HostExecutor()
    value = request(count=120, size=5)
    result = await prepare(value, host)
    assert result.compacted and len(result.messages) <= 100


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [
    lambda summary: replace(summary, sources=()),
    lambda summary: replace(summary, binding_fingerprint="wrong"),
    lambda summary: replace(summary, text=""),
    lambda summary: replace(summary, finish_reason="length"),
    lambda summary: replace(summary, finish_reason=None),
    lambda summary: replace(summary, output_status="reasoning_only"),
    lambda summary: replace(summary, artifact_id=""),
    lambda summary: replace(summary, text="too long" * 1000, usage=None),
    lambda summary: replace(summary, usage={"output_tokens": 4001}),
])
async def test_invalid_saved_summary_never_replaces_history(mutate):
    value = request()
    original = value.history_messages
    host = HostExecutor(mutate=mutate)
    with pytest.raises(ContextPreparationError):
        await prepare(value, host)
    assert len(host.requests) == 1 and value.history_messages == original


@pytest.mark.asyncio
async def test_failure_and_budget_exhaustion_do_not_trigger_internal_retry():
    for host in (HostExecutor(failure=RuntimeError("upstream failed")), HostExecutor(available=0)):
        with pytest.raises(RuntimeError):
            await prepare(request(), host)
        assert len(host.requests) <= 1


@pytest.mark.parametrize("reported", [None, -1, True, 2613.0, "2613"])
def test_invalid_or_missing_summary_usage_uses_explicit_estimate(reported):
    tokens, estimated = count_summary_output("摘要" * 2202, {"output_tokens": reported})
    assert tokens > 4000 and estimated is True


@pytest.mark.parametrize("reported", [0, 2613, 4000, 4001])
def test_summary_output_uses_reported_tokens_instead_of_character_estimate(reported):
    assert count_summary_output("摘要" * 2202, {"output_tokens": reported}) == (reported, False)


@pytest.mark.asyncio
async def test_complete_chinese_summary_over_4000_characters_with_actual_2613_tokens_is_usable():
    # Regression from the live Flash response: stop, 4,404 public characters,
    # 2,613 output tokens (including reasoning). Input estimation is separate.
    text = ("保留任务目标、原始约束和实际工具结果，后续修改必须使用授权目录并重新执行验证。" * 140)[:4404]
    assert len(text) == 4404
    host = HostExecutor(mutate=lambda summary: replace(summary, text=text, usage={"output_tokens": 2613}))
    result = await prepare(request(count=12), host)
    assert result.compacted and len(host.requests) == 1 and result.input_tokens <= result.input_limit


@pytest.mark.asyncio
async def test_known_output_usage_never_bypasses_final_input_window_guard():
    host = HostExecutor(mutate=lambda summary: replace(summary, text="中文结果" * 5000,
                                                     usage={"output_tokens": 2613}))
    with pytest.raises(ContextPreparationError) as error:
        await prepare(request(count=12), host)
    assert error.value.code == "CONTEXT_LIMIT" and len(host.requests) == 1


@pytest.mark.asyncio
async def test_pause_is_checked_by_host_without_consuming_requirement():
    host = HostExecutor()
    host.stopped = True
    value = request()
    with pytest.raises(RuntimeError, match="pause/cancel"):
        await prepare(value, host)
    assert not host.requests
    assert value.suffix_messages[0].id == "omni-command:latest"


@pytest.mark.asyncio
async def test_parallel_scopes_have_distinct_sources_and_share_host_budget():
    host = HostExecutor(available=1)
    first = request(count=12)
    second = replace(first, scope=replace(first.scope, role="review", task_id="task-2"))
    results = await asyncio.gather(prepare(first, host), prepare(second, host), return_exceptions=True)
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    assert sum(isinstance(item, RuntimeError) for item in results) == 1
    assert len(host.requests) == 1 and host.available == 0


@pytest.mark.parametrize("message", [
    AIMessage(content="", additional_kwargs={"reasoning_content": "private"}, response_metadata={"finish_reason": "stop"}),
    AIMessage(content="partial", response_metadata={"finish_reason": "length"}),
    AIMessage(content="unconfirmed"),
    AIMessage(content="", tool_calls=[{"name": "read", "args": {}, "id": "c"}],
              response_metadata={"finish_reason": "tool_calls"}),
])
def test_host_output_validator_rejects_nonfinal_summaries(message):
    with pytest.raises(ContextPreparationError):
        validate_summary_output(message)


def test_host_output_validator_returns_only_public_answer():
    message = AIMessage(content=[{"type": "thinking", "thinking": "PRIVATE"},
                                 {"type": "text", "text": "Public summary"}],
                        response_metadata={"finish_reason": "end_turn"})
    assert validate_summary_output(message) == "Public summary"


def test_repeated_provider_call_id_in_separate_complete_exchanges_is_unambiguous():
    messages = [AIMessage(id="a1", content="", tool_calls=[{"name": "read", "args": {}, "id": "call-0"}]),
                ToolMessage(id="t1", content="first", tool_call_id="call-0"),
                AIMessage(id="a2", content="", tool_calls=[{"name": "read", "args": {}, "id": "call-0"}]),
                ToolMessage(id="t2", content="second", tool_call_id="call-0")]
    assert len(interaction_groups(messages)) == 2


@pytest.mark.asyncio
async def test_stable_ids_required_and_tools_are_part_of_protected_input_budget():
    host = HostExecutor()
    with pytest.raises(ContextPreparationError, match="persisted message IDs"):
        await prepare(replace(request(), history_messages=(HumanMessage(content="missing ID"),)), host)
    schema = {"type": "function", "function": {"name": "tool", "description": "x" * 20000,
                                               "parameters": {"type": "object"}}}
    with pytest.raises(ContextPreparationError):
        await prepare(replace(request(), tool_schemas=(schema,)), host)
    assert not host.requests and ApproximateContextCounter().estimated
