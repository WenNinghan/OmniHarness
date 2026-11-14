"""Native SDK transport contracts, entirely in-memory and without provider calls."""
import asyncio
import json

import httpx
import openai
import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from omniharness.compatible_model import CompatibleChatModel
from omniharness.model_output import require_complete_output
from omniharness.providers import ProviderError


def usage(prompt=10, completion=2):
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def frame(delta=None, finish=None, counts=None):
    return {"id": "offline", "object": "chat.completion.chunk", "created": 0, "model": "offline-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
                if delta is not None or finish else [], "usage": counts}


def event(row):
    return ("data: " + json.dumps(row) + "\n\n").encode()


def response(rows):
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=b"".join(map(event, rows)) + b"data: [DONE]\n\n")


def model(client, *, opt_in=True):
    return CompatibleChatModel(model="offline-model", api_key="test-only", base_url="https://example.invalid/v1",
                               http_async_client=client, use_responses_api=False, max_retries=0,
                               streaming=True, stream_usage=True, omni_normalize_stream_usage=opt_in)


def tool_frames(finish="tool_calls"):
    return [frame({"role": "assistant", "reasoning_content": "native ",
                   "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque-state"}],
                   "tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                                   "function": {"name": "add_one", "arguments": '{"value":'}}]}),
            frame({"reasoning_content": "reasoning", "tool_calls": [
                {"index": 0, "function": {"arguments": "41}"}}]}),
            frame(finish=finish), frame(counts=usage()), frame(counts=usage())]


@pytest.mark.asyncio
async def test_native_tool_round_trip_keeps_protocol_and_counts_cumulative_usage_once():
    requests, calls = [], []

    @tool
    def add_one(value: int) -> int:
        """Add one to a number."""
        calls.append(value)
        return value + 1

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return response(tool_frames() if len(requests) == 1 else [
            frame({"role": "assistant", "content": "42"}), frame(finish="stop"), frame(counts=usage(20, 3))])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        configured = model(client)
        graph = create_agent(configured, tools=[add_one])
        result = await graph.ainvoke({"messages": [HumanMessage(content="calculate")]})
    messages = result["messages"]
    first, answer = messages[1], messages[-1]
    assert calls == [41] and len(requests) == 2
    assert first.tool_calls == [{"name": "add_one", "args": {"value": 41}, "id": "call-1", "type": "tool_call"}]
    assert first.additional_kwargs["reasoning_content"] == "native reasoning"
    assert first.additional_kwargs["reasoning_details"][0]["data"] == "opaque-state"
    assert first.usage_metadata["total_tokens"] == 12
    assert first.response_metadata["finish_reason"] == "tool_calls"
    assert first.response_metadata["omni_compatible_origin"] == configured._origin
    restored = AIMessage.model_validate(first.model_dump(mode="json"))
    assert restored.tool_calls == first.tool_calls and restored.additional_kwargs == first.additional_kwargs
    assert restored.response_metadata == first.response_metadata and restored.usage_metadata == first.usage_metadata
    assert isinstance(messages[2], ToolMessage) and messages[2].tool_call_id == "call-1"
    assert requests[1]["messages"][1]["reasoning_content"] == "native reasoning"
    assert answer.content == "42" and answer.usage_metadata["total_tokens"] == 23
    for payload in requests:
        assert payload["stream"] is True and payload["stream_options"] == {"include_usage": True}
        assert "omni_normalize_stream_usage" not in payload
    for message in (first, answer):
        assert message.response_metadata["omni_transport"] == "compatible-native-stream-v1"
        assert "token_usage" not in message.response_metadata
        assert "_omni_usage_validity" not in message.response_metadata


@pytest.mark.parametrize("counts,known", [
    ([usage(), usage()], True),
    ([usage(10, 1), usage(10, 3)], True),
    ([usage(), usage(9, 1)], False),
    ([usage(), {"prompt_tokens": 10, "completion_tokens": 2}], False),
    ([usage(), {**usage(), "total_tokens": 999}], False),
    ([usage(), {**usage(), "prompt_tokens": True}], False),
    ([{**usage(), "prompt_tokens_details": {"cached_tokens": 3}},
      {**usage(), "prompt_tokens_details": {"cached_tokens": 2}}], False),
    ([{}], False),
    ([None], False),
])
@pytest.mark.asyncio
async def test_usage_repeated_increasing_decreasing_and_missing_frames(counts, known):
    rows = [frame({"role": "assistant", "content": "done"}), frame(finish="stop")]
    rows += [frame(counts=value) for value in counts]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(rows))) as client:
        answer = await model(client).ainvoke("offline")
    assert answer.content == "done"
    assert (answer.usage_metadata is not None) == known
    if known:
        assert answer.usage_metadata["total_tokens"] == counts[-1]["total_tokens"]
    assert answer.response_metadata["omni_stream_usage_status"] == ("complete" if known else "unknown")
    assert "token_usage" not in answer.response_metadata


@pytest.mark.asyncio
async def test_prefix_only_usage_is_not_mistaken_for_complete_final_usage():
    rows = [frame({"role": "assistant", "content": "first"}, counts=usage()),
            frame({"content": " later tokens"}), frame(finish="stop")]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(rows))) as client:
        answer = await model(client).ainvoke("offline")
    assert answer.usage_metadata is None


@pytest.mark.asyncio
async def test_native_usage_details_and_duplicate_finish_are_preserved_once():
    counts = {**usage(10, 4), "prompt_tokens_details": {"cached_tokens": 3},
              "completion_tokens_details": {"reasoning_tokens": 2}}
    rows = [frame({"role": "assistant", "content": "done"}), frame(finish="stop", counts=counts),
            frame(finish="stop", counts=counts)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(rows))) as client:
        answer = await model(client).ainvoke("offline")
    assert answer.usage_metadata == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
                                     "input_token_details": {"cache_read": 3},
                                     "output_token_details": {"reasoning": 2}}
    assert answer.response_metadata["finish_reason"] == "stop"


def test_sync_native_sdk_path_uses_same_final_usage_contract():
    rows = [frame({"role": "assistant", "content": "done"}), frame(finish="stop"),
            frame(counts=usage()), frame(counts=usage())]
    with httpx.Client(transport=httpx.MockTransport(lambda _: response(rows))) as client:
        configured = CompatibleChatModel(
            model="offline-model", api_key="test-only", base_url="https://example.invalid/v1", http_client=client,
            use_responses_api=False, max_retries=0, streaming=True, stream_usage=True, omni_normalize_stream_usage=True)
        answer = configured.invoke("offline")
    assert answer.usage_metadata["total_tokens"] == 12
    assert answer.response_metadata["omni_transport"] == "compatible-native-stream-v1"


@pytest.mark.parametrize("finish", [None, "unrecognized", "length"])
@pytest.mark.asyncio
async def test_finish_is_required_and_length_keeps_existing_truncation_classification(finish):
    rows = [frame({"role": "assistant", "content": "partial"}), frame(finish=finish), frame(counts=usage())]
    requests = []

    def handle(request):
        requests.append(request)
        return response(rows)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        if finish == "length":
            answer = await model(client).ainvoke("offline")
            with pytest.raises(ProviderError) as error:
                require_complete_output(answer)
            assert error.value.code == "OUTPUT_TRUNCATED"
        else:
            with pytest.raises(ProviderError) as error:
                await model(client).ainvoke("offline")
            assert error.value.code == "OUTPUT_INCOMPLETE"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_missing_finish_never_dispatches_complete_tool_arguments():
    calls, requests = [], []

    @tool
    def add_one(value: int) -> int:
        """Add one to a number."""
        calls.append(value)
        return value + 1

    def handle(request):
        requests.append(request)
        return response(tool_frames(finish=None))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        graph = create_agent(model(client), tools=[add_one])
        with pytest.raises(ProviderError, match="结束标记"):
            await graph.ainvoke({"messages": [HumanMessage(content="calculate")]})
    assert calls == [] and len(requests) == 1


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield event(frame({"role": "assistant", "content": "partial"}))
        raise httpx.ReadError("offline stream interruption")


@pytest.mark.asyncio
async def test_transport_interruption_never_returns_partial_success_or_retries():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenStream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(openai.APIConnectionError):
            await model(client).ainvoke("offline")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_same_model_concurrent_streams_keep_independent_usage():
    class Interleaved(httpx.AsyncByteStream):
        def __init__(self, value):
            self.value = value

        async def __aiter__(self):
            rows = [frame({"role": "assistant", "content": str(self.value)}), frame(finish="stop"),
                    frame(counts=usage(self.value, 2)), frame(counts=usage(self.value, 2))]
            for row in rows:
                await asyncio.sleep(0)
                yield event(row)
            yield b"data: [DONE]\n\n"

    def handle(request):
        value = int(json.loads(request.content)["messages"][-1]["content"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Interleaved(value))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        shared = model(client)
        answers = await asyncio.gather(shared.ainvoke("10"), shared.ainvoke("30"))
    assert [answer.usage_metadata["input_tokens"] for answer in answers] == [10, 30]
    assert [answer.content for answer in answers] == ["10", "30"]


@pytest.mark.asyncio
async def test_cancelled_stream_is_not_swallowed_or_retried():
    started, requests = asyncio.Event(), []

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event(frame({"role": "assistant", "content": "partial"}))
            started.set()
            await asyncio.Event().wait()

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Waiting())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        pending = asyncio.create_task(model(client).ainvoke("offline"))
        await asyncio.wait_for(started.wait(), timeout=5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_opt_out_stream_retains_original_cumulative_chunk_contract():
    rows = [frame({"role": "assistant", "content": "done"}), frame(finish="stop"),
            frame(counts=usage()), frame(counts=usage())]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(rows))) as client:
        chunks = [chunk async for chunk in model(client, opt_in=False).astream("offline")]
    assert [chunk.usage_metadata["total_tokens"] for chunk in chunks if chunk.usage_metadata] == [12, 12]
    assert all("omni_transport" not in chunk.response_metadata for chunk in chunks)
