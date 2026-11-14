"""Real loopback HTTP coverage of the compatible SDK extension and channel contract."""
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from omniharness.model_output import extract_output, require_complete_output
from omniharness.providers import ProviderError, RuntimeModelConfig, build_chat_model, stream_chat


@pytest.fixture
def protocol_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            mode = body["model"]
            usage = {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16}
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def emit(delta, finish=None, token_usage=None):
                    self.wfile.write(("data: " + json.dumps({
                        "id": "wire", "object": "chat.completion.chunk", "created": 1, "model": mode,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                        "usage": token_usage,
                    }) + "\n\n").encode())
                    self.wfile.flush()

                emit({"role": "assistant", "content": ""})
                emit({"reasoning_content": "provider ", "reasoning_details": [
                    {"type": "reasoning.summary", "summary": "redundant "}]})
                emit({"reasoning_content": "reasoning"})
                if mode in {"echo-length", "echo-stop", "echo-pieces-length"}:
                    if mode == "echo-pieces-length":
                        emit({"content": "provider "})
                        emit({"content": "reasoning"})
                    else:
                        emit({"content": "provider reasoning"})
                elif mode == "prefix-length":
                    emit({"content": "provider"})
                elif mode == "diverge-length":
                    emit({"content": "provider "})
                    emit({"content": "answer"})
                elif mode not in {"reasoning-only", "reasoning-length"}:
                    emit({"content": "final "})
                    emit({"content": "answer"}, token_usage=usage)
                if mode == "refusal":
                    emit({"refusal": "provider refused"})
                if mode != "missing-finish":
                    emit(None, "length" if mode.endswith("-length") else "stop", usage)
                # A duplicate total snapshot must not charge output tokens twice.
                emit({}, token_usage=usage)
                self.wfile.write(b"data: [DONE]\n\n")
                return
            if body["messages"][-1]["role"] == "tool":
                message = {"role": "assistant", "content": "42", "reasoning_content": "result reasoning"}
                finish = "stop"
            elif body.get("tools"):
                message = {"role": "assistant", "content": "", "reasoning_content": "call reasoning",
                           "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque-native-state"}],
                           "tool_calls": [{"id": "call-1", "type": "function",
                                           "function": {"name": "add", "arguments": '{"a":19,"b":23}'}}]}
                finish = "tool_calls"
            else:
                message = {"role": "assistant", "content": "answer", "reasoning_content": "separate reasoning"}
                finish = None if mode == "missing-finish" else "stop"
            payload = json.dumps({"id": "wire", "object": "chat.completion", "created": 1, "model": mode,
                                  "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                  "usage": usage}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = RuntimeModelConfig("test", "openai-compatible", "normal", api_key="test-only-key",
                             base_url=f"http://127.0.0.1:{server.server_port}/v1")
    try:
        yield cfg, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("output_limit", [None, 4096])
async def test_http_stream_keeps_reasoning_answer_and_usage_separate(protocol_server, output_limit):
    cfg, requests = protocol_server
    if output_limit is not None:
        cfg = replace(cfg, max_output_tokens=output_limit)
    deltas = [delta async for delta in stream_chat(cfg, [{"role": "user", "content": "test"}])]
    assert "".join(delta.text for delta in deltas) == "final answer"
    assert "".join(delta.reasoning for delta in deltas) == "provider reasoning"
    assert deltas[-1].finish_reason == "stop"
    assert deltas[-1].output_status == "complete"
    assert deltas[-1].usage == {"input_tokens": 7, "output_tokens": 9, "total_tokens": 16, "cost": None}
    assert requests[0]["max_tokens"] == (100_000 if output_limit is None else output_limit)
    assert "max_completion_tokens" not in requests[0]


@pytest.mark.parametrize("mode,code,status", [
    ("answer-length", "OUTPUT_TRUNCATED", "truncated"),
    ("reasoning-length", "OUTPUT_TRUNCATED", "truncated"),
    ("reasoning-only", "REASONING_ONLY", "reasoning_only"),
    ("missing-finish", "OUTPUT_INCOMPLETE", "interrupted"),
    ("refusal", "OUTPUT_FILTERED", "filtered"),
])
async def test_http_incomplete_stream_retains_channels_and_never_succeeds(protocol_server, mode, code, status):
    cfg, _ = protocol_server
    deltas = []
    with pytest.raises(ProviderError) as exc:
        async for delta in stream_chat(replace(cfg, model_name=mode), [{"role": "user", "content": "test"}]):
            deltas.append(delta)
    assert exc.value.code == code
    assert deltas[-1].output_status == status
    assert deltas[-1].usage["total_tokens"] == 16
    assert "".join(delta.reasoning for delta in deltas) == "provider reasoning"
    assert "test-only-key" not in str(exc.value)


async def test_nonstream_missing_finish_is_not_a_complete_result(protocol_server):
    cfg, _ = protocol_server
    response = await build_chat_model(replace(cfg, model_name="missing-finish")).ainvoke("test")
    assert extract_output(response).reasoning == "separate reasoning"
    with pytest.raises(ProviderError) as exc:
        require_complete_output(response)
    assert exc.value.code == "OUTPUT_INCOMPLETE"


async def test_native_reasoning_replay_is_bound_to_same_model_and_tools(protocol_server):
    cfg, requests = protocol_server
    model = build_chat_model(cfg)
    tool = {"type": "function", "function": {"name": "add", "description": "Add integers", "parameters": {
        "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"]}}}
    first = HumanMessage(content="add")
    response = await model.bind_tools([tool]).ainvoke([first])
    assert require_complete_output(response, allow_tools=True) == ""
    assert extract_output(response).reasoning == "call reasoning"
    history = [first, response, ToolMessage(content="42", tool_call_id="call-1")]
    result = await model.bind_tools([tool]).ainvoke(history)
    assert require_complete_output(result) == "42"
    native = requests[-1]["messages"][1]
    assert native["reasoning_content"] == "call reasoning"
    assert native["reasoning_details"][0]["data"] == "opaque-native-state"
    await build_chat_model(replace(cfg, model_name="different-model")).bind_tools([tool]).ainvoke(history)
    assert "reasoning_content" not in requests[-1]["messages"][1]
    await model.ainvoke([first, response])
    assert "reasoning_content" not in requests[-1]["messages"][1]


def test_explicit_blocks_only_never_guess_from_answer_language_or_example_tags():
    message = AIMessage(content=[{"type": "thinking", "thinking": "native thought", "signature": "opaque"},
                                 {"type": "text", "text": "visible answer"}])
    parsed = extract_output(message)
    assert (parsed.text, parsed.reasoning) == ("visible answer", "native thought")
    example = 'We need an example:\n```xml\n<think>test</think>\n```'
    assert extract_output(AIMessage(content=example)).text == example
    assert extract_output(AIMessage(content=example)).reasoning == ""


@pytest.mark.parametrize("metadata,code", [
    ({"stop_reason": "max_tokens"}, "OUTPUT_TRUNCATED"),
    ({"finish_reason": "MAX_TOKENS"}, "OUTPUT_TRUNCATED"),
    ({"finish_reason": "SAFETY"}, "OUTPUT_FILTERED"),
    ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, "OUTPUT_TRUNCATED"),
])
def test_native_provider_finish_reasons(metadata, code):
    with pytest.raises(ProviderError) as exc:
        require_complete_output(AIMessage(content="partial", response_metadata=metadata))
    assert exc.value.code == code


@pytest.mark.parametrize("mode,answer", [
    ("echo-length", ""),
    ("echo-pieces-length", ""),
    ("echo-stop", "provider reasoning"),
    ("prefix-length", "provider"),
    ("diverge-length", "provider answer"),
])
async def test_http_truncated_verbatim_echo_only_is_removed(protocol_server, mode, answer):
    cfg, _ = protocol_server
    deltas = []
    try:
        async for delta in stream_chat(replace(cfg, model_name=mode), [{"role": "user", "content": "test"}]):
            deltas.append(delta)
    except ProviderError as exc:
        assert mode.endswith("-length")
        assert exc.code == "OUTPUT_TRUNCATED"
    else:
        assert mode == "echo-stop"
    assert "".join(delta.text for delta in deltas) == answer
    assert "".join(delta.reasoning for delta in deltas) == "provider reasoning"
    assert deltas[-1].output_status == ("complete" if mode == "echo-stop" else "truncated")


@pytest.mark.parametrize("finish,text,expected", [
    ("length", "identical", ""),
    ("stop", "identical", "identical"),
    (None, "identical", "identical"),
    ("length", "identical plus answer", "identical plus answer"),
    ("length", "identi", "identi"),
    ("length", " identical", " identical"),
])
def test_complete_response_echo_requires_exact_equality_and_truncation(finish, text, expected):
    response = AIMessage(content=text, additional_kwargs={"reasoning_content": "identical"},
                         response_metadata={"finish_reason": finish})
    parsed = extract_output(response)
    assert parsed.text == expected
    assert parsed.reasoning == "identical"
    assert extract_output(response, deduplicate_echo=False).text == text


async def test_interrupted_echo_candidate_is_preserved_without_guessing(monkeypatch):
    from langchain_core.messages import AIMessageChunk

    from omniharness import providers

    class Interrupted:
        async def astream(self, _messages):
            yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "same text"})
            yield AIMessageChunk(content="same text")
            raise TimeoutError("raw error must stay private")

    monkeypatch.setattr(providers, "build_chat_model", lambda _: Interrupted())
    cfg = RuntimeModelConfig("test", "openai-compatible", "test", base_url="http://127.0.0.1:1/v1")
    deltas = []
    with pytest.raises(ProviderError) as exc:
        async for delta in stream_chat(cfg, [{"role": "user", "content": "test"}]):
            deltas.append(delta)
    assert exc.value.code == "PROVIDER_TIMEOUT"
    assert "".join(delta.text for delta in deltas) == "same text"
    assert "".join(delta.reasoning for delta in deltas) == "same text"


async def test_real_gemini_sdk_thought_blocks_enum_and_negative_usage_correction(monkeypatch):
    from google.genai.types import GenerateContentResponse
    from langchain_google_genai.chat_models import _response_to_result

    from omniharness import providers

    first = GenerateContentResponse.model_validate({
        "model_version": "gemini-3-pro", "candidates": [{"content": {"role": "model", "parts": [
            {"text": "native thought", "thought": True, "thought_signature": b"opaque-signature"}]}}],
        "usage_metadata": {"prompt_token_count": 10, "candidates_token_count": 0, "total_token_count": 10},
    })
    last = GenerateContentResponse.model_validate({
        "model_version": "gemini-3-pro", "candidates": [{"finish_reason": "STOP", "content": {
            "role": "model", "parts": [{"text": "native answer"}]}}],
        "usage_metadata": {"prompt_token_count": 8, "candidates_token_count": 2, "total_token_count": 10},
    })
    one = _response_to_result(first, stream=True).generations[0]
    two = _response_to_result(last, stream=True, prev_usage=one.message.usage_metadata).generations[0]
    assert two.message.usage_metadata["input_tokens"] == -2
    for generation in (one, two):
        generation.message.response_metadata.update(generation.generation_info or {})

    class ScriptedSDK:
        async def astream(self, _messages):
            yield one.message
            yield two.message

    monkeypatch.setattr(providers, "build_chat_model", lambda _: ScriptedSDK())
    cfg = RuntimeModelConfig("native", "gemini", "gemini-3-pro", api_key="local-test")
    deltas = [delta async for delta in stream_chat(cfg, [{"role": "user", "content": "test"}])]
    assert "".join(delta.text for delta in deltas) == "native answer"
    assert "".join(delta.reasoning for delta in deltas) == "native thought"
    assert deltas[-1].usage == {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10, "cost": None}
    assert deltas[-1].finish_reason == "STOP" and deltas[-1].output_status == "complete"


def test_real_anthropic_sdk_thinking_signature_and_stop_event():
    from anthropic.types import RawContentBlockDeltaEvent, RawMessageDeltaEvent
    from langchain_anthropic import ChatAnthropic

    # The conversion method does not need a transport; avoid constructing HTTP
    # clients so this contract test is strictly local and inexpensive.
    model = ChatAnthropic.model_construct(model="claude-test")
    thought, _ = model._make_message_chunk_from_anthropic_event(
        RawContentBlockDeltaEvent.model_validate({"type": "content_block_delta", "index": 0,
                                                  "delta": {"type": "thinking_delta", "thinking": "native thought"}}),
        coerce_content_to_string=False)
    signature, _ = model._make_message_chunk_from_anthropic_event(
        RawContentBlockDeltaEvent.model_validate({"type": "content_block_delta", "index": 0,
                                                  "delta": {"type": "signature_delta", "signature": "opaque"}}),
        coerce_content_to_string=False)
    final, _ = model._make_message_chunk_from_anthropic_event(
        RawMessageDeltaEvent.model_validate({"type": "message_delta", "delta": {
            "stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}}),
        coerce_content_to_string=False)
    assert extract_output(thought).reasoning == "native thought"
    assert extract_output(signature).reasoning == ""
    assert extract_output(final).finish_reason == "end_turn"
