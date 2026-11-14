import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from omniharness import providers
from omniharness.providers import (
    ProviderError,
    RuntimeModelConfig,
    build_chat_model,
    probe_model,
    stream_chat,
    validate_model_parameters,
)


def config(adapter="openai-compatible", **kwargs):
    values = {
        "id": "test-model",
        "adapter_type": adapter,
        "model_name": "test-model",
        "api_key": "unit-test-private-key",
        "base_url": "http://127.0.0.1:12345/v1",
    }
    values.update(kwargs)
    return RuntimeModelConfig(**values)


@pytest.mark.parametrize(
    "adapter,module,class_name",
    [
        ("openai", "langchain_openai", "ChatOpenAI"),
        ("openai-compatible", "omniharness.compatible_model", "CompatibleChatModel"),
        ("anthropic", "langchain_anthropic", "ChatAnthropic"),
        ("gemini", "langchain_google_genai", "ChatGoogleGenerativeAI"),
    ],
)
@pytest.mark.parametrize("output_limit", [None, 4096])
def test_official_adapter_configuration(monkeypatch, adapter, module, class_name, output_limit):
    captured = {}

    def constructor(**kwargs):
        captured.update(kwargs)
        return "built-client"

    def importer(name):
        assert name == module
        return SimpleNamespace(**{class_name: constructor})

    monkeypatch.setattr(providers.importlib, "import_module", importer)
    cfg = config(adapter, parameters={"temperature": 0.2}, timeout_seconds=37,
                 **({"max_output_tokens": output_limit} if output_limit is not None else {}))
    expected_limit = 100_000 if output_limit is None else output_limit
    assert build_chat_model(cfg) == "built-client"
    assert captured["model"] == "test-model"
    assert captured["api_key"] == cfg.api_key
    assert captured["base_url"] == cfg.base_url
    assert captured["timeout"] == 37
    assert captured["max_retries"] == 0
    assert captured["temperature"] == 0.2
    if adapter in {"openai", "openai-compatible"}:
        assert captured["use_responses_api"] is (adapter == "openai")
        assert captured["stream_usage"] is True
    if adapter == "gemini":
        assert captured["max_output_tokens"] == expected_limit
        assert captured["vertexai"] is False
    else:
        assert captured["max_tokens"] == expected_limit


@pytest.mark.parametrize(
    "key",
    [
        "model",
        "api_key",
        "base_url",
        "client",
        "http_client",
        "default_headers",
        "model_kwargs",
        "extra_body",
    ],
)
def test_parameters_cannot_override_transport_or_credentials(key):
    with pytest.raises(ProviderError) as error:
        build_chat_model(config(parameters={key: "do-not-print-this"}))
    assert error.value.code == "INVALID_PARAMETERS"
    assert "do-not-print-this" not in str(error.value)


@pytest.mark.parametrize(
    "parameters", [{"temperature": float("nan")}, {"top_p": 1.5}, {"seed": True}, {"temperature": -1}]
)
def test_invalid_sampling_values(parameters):
    with pytest.raises(ProviderError, match="参数"):
        build_chat_model(config(parameters=parameters))


@pytest.mark.parametrize(
    "adapter,parameters",
    [
        ("openai-compatible", {"reasoning_effort": "high"}),
        ("openai", {"top_k": 10}),
        ("anthropic", {"seed": 42}),
        ("gemini", {"temperature": "secret-not-a-number"}),
        ("mock", {"temperature": 0.2}),
    ],
)
def test_shared_parameter_validation_is_adapter_specific(adapter, parameters):
    with pytest.raises(ProviderError) as error:
        validate_model_parameters(adapter, parameters)
    assert error.value.code == "INVALID_PARAMETERS"
    assert "secret-not-a-number" not in error.value.public_message


def test_shared_parameter_validation_needs_no_credential():
    assert validate_model_parameters("gemini", {"top_k": 10, "temperature": 0.2}) == {
        "top_k": 10,
        "temperature": 0.2,
    }


def test_missing_credentials_never_use_environment_or_mock(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-used")
    with pytest.raises(ProviderError) as error:
        build_chat_model(config("openai", api_key=None))
    assert error.value.code == "MISSING_API_KEY"
    assert "unit-test-private-key" not in repr(config())


def test_missing_compatible_key_is_explicit_not_ambient(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        providers.importlib,
        "import_module",
        lambda _: SimpleNamespace(CompatibleChatModel=lambda **kw: captured.update(kw)),
    )
    build_chat_model(config(api_key=None))
    assert captured["api_key"] == "omniharness-no-key-configured"


class ScriptedModel:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(("chat", messages))
        return AIMessage(content="OK")

    async def astream(self, messages):
        self.calls.append(("stream", messages))
        yield AIMessageChunk(
            content="Hello", usage_metadata={"input_tokens": 4, "output_tokens": 0, "total_tokens": 4}
        )
        yield AIMessageChunk(
            content=[{"type": "reasoning", "reasoning": "hidden"}, {"type": "text", "text": " world"}]
        )
        yield AIMessageChunk(
            content="", usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
        )

    def bind_tools(self, tools, **kwargs):
        parent = self

        class Bound:
            async def ainvoke(self, messages):
                parent.calls.append(("tool", messages))
                if kwargs.get("tool_choice") in {"any", "auto"}:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "_probe_add",
                                "args": {"a": 19, "b": 23},
                                "id": "probe-call",
                                "type": "tool_call",
                            }
                        ],
                    )
                assert isinstance(messages[-1], ToolMessage)
                assert messages[-1].tool_call_id == "probe-call"
                assert messages[-1].content == "42"
                return AIMessage(content="The tool returned 42.")

        return Bound()

    def with_structured_output(self, schema, **kwargs):
        parent = self

        class Structured:
            async def ainvoke(self, messages):
                parent.calls.append(("structured", messages))
                return schema(answer=42)

        return Structured()


@pytest.mark.asyncio
async def test_stream_emits_only_visible_text_and_cumulative_usage(monkeypatch):
    monkeypatch.setattr(providers, "build_chat_model", lambda _: ScriptedModel())
    deltas = [d async for d in stream_chat(config(), [{"role": "user", "content": "hello"}])]
    assert "".join(d.text for d in deltas) == "Hello world"
    assert deltas[-1].usage == {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6, "cost": None}


@pytest.mark.asyncio
async def test_all_probes_execute_and_tool_probe_completes_round_trip(monkeypatch):
    model = ScriptedModel()
    monkeypatch.setattr(providers, "build_chat_model", lambda _, **kwargs: model)
    result = await probe_model(config(), ["chat", "streaming", "tools", "structured"])
    assert all(value["status"] == "supported" for value in result.values())
    assert [kind for kind, _ in model.calls] == ["chat", "stream", "tool", "tool", "structured"]


@pytest.mark.asyncio
async def test_tool_binding_alone_is_not_capability_proof(monkeypatch):
    model = ScriptedModel()
    model.bind_tools = lambda *args, **kwargs: model
    monkeypatch.setattr(providers, "build_chat_model", lambda _, **kwargs: model)
    result = await probe_model(config(), ["tools"])
    assert result["tools"]["status"] == "unsupported"


@pytest.mark.asyncio
async def test_mock_is_visible_and_does_not_claim_live_capabilities():
    cfg = config("mock", api_key=None, base_url=None)
    deltas = [d async for d in stream_chat(cfg, [{"role": "user", "content": "hello"}])]
    assert "[模拟]" in "".join(d.text for d in deltas)
    assert all(d.usage is None for d in deltas)
    result = await probe_model(cfg, ["chat", "streaming", "tools", "structured"])
    assert all(v["status"] == "unsupported" and "[模拟]" in v["message"] for v in result.values())


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "PROVIDER_AUTH"),
        (403, "PROVIDER_AUTH"),
        (429, "PROVIDER_RATE_LIMIT"),
        (404, "PROVIDER_NOT_FOUND"),
        (400, "PROVIDER_REQUEST_REJECTED"),
        (500, "PROVIDER_UNAVAILABLE"),
    ],
)
@pytest.mark.asyncio
async def test_provider_errors_never_expose_raw_text(monkeypatch, status, code):
    class SecretError(Exception):
        status_code = status

    class Broken(ScriptedModel):
        async def astream(self, messages):
            raise SecretError("Authorization: Bearer secret-123 https://user:password@example.test")
            yield

    monkeypatch.setattr(providers, "build_chat_model", lambda _: Broken())
    with pytest.raises(ProviderError) as error:
        _ = [d async for d in stream_chat(config(), [{"role": "user", "content": "hello"}])]
    assert error.value.code == code
    assert not any(secret in str(error.value) for secret in ("secret-123", "Authorization", "password"))
    result = await probe_model(config(), ["streaming"])
    assert result["streaming"]["status"] == "error"
    assert "secret-123" not in json.dumps(result)


@pytest.fixture
def compatible_server(monkeypatch):
    # Exercise loopback HTTP directly even when the developer has a system proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "body": body})
            if body.get("tool_choice") == "required":
                payload = b'{"error":{"message":"Forced tool choice is unavailable in thinking mode"}}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if body.get("tools") and body["messages"][-1]["role"] != "tool":
                    deltas = [{"tool_calls": [{"index": 0, "id": "wire-call", "type": "function",
                               "function": {"name": "_probe_add", "arguments": '{"a":19,"b":23}'}}]}]
                    finish = "tool_calls"
                else:
                    deltas = [{"content": "42"}] if body.get("tools") else [
                        {"content": "Local "}, {"content": "wire response"}]
                    finish = "stop"
                for delta in deltas:
                    item = {
                        "id": "local-stream",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "wire-model",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                    self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
                    self.wfile.flush()
                final = {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(final) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            message = {"role": "assistant", "content": "OK"}
            if body.get("response_format"):
                message["content"] = '{"answer":42}'
            elif body["messages"][-1]["role"] == "tool":
                message["content"] = "42"
            elif body.get("tools"):
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "wire-call",
                            "type": "function",
                            "function": {"name": "_probe_add", "arguments": '{"a":19,"b":23}'},
                        }
                    ],
                }
            result = {
                "id": "local-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "wire-model",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_official_sdk_against_real_local_compatible_http_protocol(compatible_server):
    endpoint, requests = compatible_server
    cfg = replace(config(), model_name="wire-model", base_url=endpoint)
    result = await probe_model(cfg, ["chat", "streaming", "tools", "structured"])
    assert all(value["status"] == "supported" for value in result.values()), result
    deltas = [d async for d in stream_chat(cfg, [{"role": "user", "content": "hello"}])]
    assert "".join(d.text for d in deltas) == "Local wire response"
    assert all(request["path"] == "/v1/chat/completions" for request in requests)
    assert any(request["body"]["messages"][-1]["role"] == "tool" for request in requests)
    first_tool = next(request["body"] for request in requests if request["body"].get("tools"))
    assert first_tool["tool_choice"] == "auto"
    assert first_tool["max_tokens"] == cfg.max_output_tokens
    assert first_tool["stream"] is True
    assert result["tools"]["transport"] == providers.NATIVE_AGENT_STREAM
    assert any(
        request["body"].get("response_format", {}).get("type") == "json_schema" for request in requests
    )
    assert any(request["body"].get("stream_options") == {"include_usage": True} for request in requests)
