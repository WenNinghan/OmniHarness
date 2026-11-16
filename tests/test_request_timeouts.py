"""Real loopback HTTP faults, without vendor requests or SDK retry masking."""
import asyncio
import json
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from omniharness.providers import RuntimeModelConfig, build_chat_model, probe_model, stream_chat
from omniharness.request_policy import REQUEST_POLICY_KEY


@dataclass
class Scenario:
    delay: float = 0
    steps: list = field(default_factory=list)
    status: int = 200
    closed: threading.Event = field(default_factory=threading.Event)
    disconnected: threading.Event = field(default_factory=threading.Event)


@pytest.fixture
def fault_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    scenarios, requests = {}, []
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def write(self, value):
            self.wfile.write(value)
            self.wfile.flush()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            scenario = scenarios[body["messages"][-1]["content"]]
            try:
                if stop.wait(scenario.delay):
                    return
                if scenario.status != 200:
                    payload = b'{"error":{"message":"local rejection","type":"test"}}'
                    self.send_response(scenario.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.write(payload)
                    return
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for delay, delta in scenario.steps:
                        if stop.wait(delay):
                            return
                        if isinstance(delta, bytes):
                            self.write(delta)
                            continue
                        chunk = {"id": "local-fault", "object": "chat.completion.chunk", "created": 1,
                                 "model": "local-fault", "choices": [{"index": 0, "delta": delta,
                                                                      "finish_reason": None}]}
                        self.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    self.write(("data: " + json.dumps(final) + "\n\ndata: [DONE]\n\n").encode())
                    return
                result = {"id": "local-fault", "object": "chat.completion", "created": 1,
                          "model": "local-fault", "choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": "OK"}}],
                          "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                scenario.disconnected.set()
            finally:
                scenario.closed.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", scenarios, requests
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def configured(endpoint, **timeouts):
    limits = {"connect_seconds": 1, "read_seconds": 2, "write_seconds": 1, "pool_seconds": 1,
              "first_chunk_seconds": 1.5, "idle_seconds": 0.5, "total_seconds": 3}
    limits.update(timeouts)
    return RuntimeModelConfig(id="fault", adapter_type="openai-compatible", model_name="local-fault",
                              base_url=endpoint, api_key="test-only", timeout_seconds=1,
                              parameters={REQUEST_POLICY_KEY: {"version": 1, "timeouts": limits}})


async def collect(model, prompt):
    return [chunk async for chunk in model.astream([("user", prompt)])]


async def closes(scenario):
    assert await asyncio.to_thread(scenario.disconnected.wait, 1.5), "cancelled HTTP stream remained open"


async def test_nonstream_waits_beyond_legacy_scalar_and_local_policy_is_not_sent(fault_server):
    endpoint, scenarios, requests = fault_server
    scenarios["slow"] = Scenario(delay=1.15)
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint), http_async_client=client)
        message = await model.ainvoke([("user", "slow")])
    assert message.content == "OK"
    assert len(requests) == 1
    assert not any(key.startswith("omni_") or "timeout" in key for key in requests[0])
    assert model.max_retries == 0


async def test_nonstream_total_deadline_and_http_read_are_independent(fault_server):
    endpoint, scenarios, requests = fault_server
    scenarios["total"] = Scenario(delay=0.4)
    scenarios["read"] = Scenario(delay=0.4)
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint, total_seconds=0.12, first_chunk_seconds=0.1,
                                             idle_seconds=0.1), http_async_client=client)
        with pytest.raises(TimeoutError) as error:
            await model.ainvoke([("user", "total")])
        assert error.value.omni_timeout_phase == "total"
        model = build_chat_model(configured(endpoint, read_seconds=0.1), http_async_client=client)
        with pytest.raises(Exception) as error:
            await model.ainvoke([("user", "read")])
        assert "timeout" in type(error.value).__name__.lower()
        cause = error.value
        while cause is not None and not isinstance(cause, httpx.ReadTimeout):
            cause = cause.__cause__
        assert isinstance(cause, httpx.ReadTimeout)
    assert len(requests) == 2  # No hidden SDK retry on either ambiguous timeout.


async def test_empty_frames_and_heartbeats_do_not_extend_first_model_progress(fault_server):
    endpoint, scenarios, requests = fault_server
    scenario = scenarios["empty"] = Scenario(steps=[(0, {"role": "assistant", "content": ""}),
        *[(0.04, b": keepalive\n\n") for _ in range(30)]])
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint, first_chunk_seconds=0.16), http_async_client=client,
                                 native_streaming=True)
        with pytest.raises(TimeoutError) as error:
            await collect(model, "empty")
        assert error.value.omni_timeout_phase == "first_chunk"
        assert error.value.omni_response_started is True
        await closes(scenario)
    assert len(requests) == 1


async def test_text_then_heartbeat_only_times_out_idle_and_closes_socket(fault_server):
    endpoint, scenarios, requests = fault_server
    scenario = scenarios["idle"] = Scenario(steps=[(0, {"content": "first"}),
        *[(0.04, b": keepalive\n\n") for _ in range(30)]])
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint, idle_seconds=0.14), http_async_client=client,
                                 native_streaming=True)
        with pytest.raises(TimeoutError) as error:
            await collect(model, "idle")
        assert error.value.omni_timeout_phase == "stream_idle"
        assert error.value.omni_response_started is True
        await closes(scenario)
    assert len(requests) == 1


async def test_active_stream_obeys_total_deadline(fault_server):
    endpoint, scenarios, _ = fault_server
    scenario = scenarios["active"] = Scenario(steps=[(0.04, {"content": "x"}) for _ in range(30)])
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint, total_seconds=0.28, first_chunk_seconds=0.2,
                                             idle_seconds=0.2), http_async_client=client,
                                 native_streaming=True)
        with pytest.raises(TimeoutError) as error:
            await collect(model, "active")
        assert error.value.omni_timeout_phase == "total" and error.value.omni_response_started is True
        await closes(scenario)


async def test_cancellation_closes_real_stream_and_does_not_retry(fault_server):
    endpoint, scenarios, requests = fault_server
    scenario = scenarios["cancel"] = Scenario(steps=[(0.04, {"content": "x"}) for _ in range(40)])
    got_chunk = asyncio.Event()
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint), http_async_client=client, native_streaming=True)

        async def consume():
            async for chunk in model.astream([("user", "cancel")]):
                if chunk.content:
                    got_chunk.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(got_chunk.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await closes(scenario)
    assert len(requests) == 1


async def test_each_concurrent_request_has_its_own_progress_clock(fault_server):
    endpoint, scenarios, requests = fault_server
    scenarios["good"] = Scenario(steps=[(0.05, {"content": "x"}) for _ in range(7)])
    bad = scenarios["bad"] = Scenario(steps=[(0.45, {"content": "too late"})])
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint, first_chunk_seconds=0.16, idle_seconds=0.12),
                                 http_async_client=client, native_streaming=True)
        good, error = await asyncio.gather(collect(model, "good"), collect(model, "bad"), return_exceptions=True)
        assert "".join(chunk.content for chunk in good) == "xxxxxxx"
        assert isinstance(error, TimeoutError) and error.omni_timeout_phase == "first_chunk"
        assert error.omni_response_started is False
        await asyncio.to_thread(bad.closed.wait, 1)
    assert len(requests) == 2


async def test_slow_consumer_is_not_cancelled_outside_provider_await(fault_server):
    endpoint, scenarios, _ = fault_server
    scenarios["consumer"] = Scenario(steps=[(0, {"content": "x"}), (0.01, {"content": "y"})])
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint, total_seconds=0.2, first_chunk_seconds=0.15,
                                             idle_seconds=0.15), http_async_client=client,
                                 native_streaming=True)
        stream = model.astream([("user", "consumer")])
        try:
            await anext(stream)
            await asyncio.sleep(0.25)  # Caller work is outside a transport timeout context.
            with pytest.raises(TimeoutError):
                await anext(stream)
        finally:
            with suppress(Exception):
                await stream.aclose()


async def test_chat_and_probe_choose_new_total_instead_of_legacy_scalar(fault_server):
    endpoint, scenarios, requests = fault_server
    scenarios["slow chat"] = Scenario(steps=[(1.15, {"content": "OK"})])
    scenarios["Reply with the single word OK."] = Scenario(delay=1.15)
    cfg = configured(endpoint)
    deltas = [item async for item in stream_chat(cfg, [{"role": "user", "content": "slow chat"}])]
    assert "".join(item.text for item in deltas) == "OK"
    result = await probe_model(cfg, ["chat"])
    assert result["chat"]["status"] == "supported", result
    assert len(requests) == 2


async def test_complete_rejection_has_not_started_response_and_no_sdk_retry(fault_server):
    endpoint, scenarios, requests = fault_server
    scenarios["rate limit"] = Scenario(status=429)
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint), http_async_client=client, native_streaming=True)
        with pytest.raises(Exception) as error:
            await collect(model, "rate limit")
        assert error.value.status_code == 429 and error.value.omni_response_started is False
    assert len(requests) == 1
