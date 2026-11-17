"""Real loopback protocol faults; no external model, controller or tool calls."""
import asyncio
import hashlib
import json
import socket
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import httpx2
import pytest
from test_request_timeouts import configured

from omniharness import request_diagnostics
from omniharness.provider_recovery import classify_failure
from omniharness.providers import ProviderError, build_chat_model
from omniharness.request_diagnostics import DIAGNOSTIC_KEY, failure_diagnostic, safe_transport_diagnostic

CANARY = "wire-secret-key-url-header-body-error-canary"


@pytest.fixture
def diagnostic_wire(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    stop, requests, disconnected = threading.Event(), [], threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def write(self, value):
            self.wfile.write(value)
            self.wfile.flush()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            mode = body["messages"][-1]["content"]
            try:
                status = 429 if mode == "rejected" else 200
                self.send_response(status)
                self.send_header("x-request-id", CANARY + "-" + mode)
                self.send_header("set-cookie", "token=" + CANARY)
                self.send_header("authorization", "Bearer " + CANARY)
                if status == 429:
                    payload = json.dumps({"error": {"message": CANARY, "type": "local"}}).encode()
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.write(payload)
                    return
                if mode == "truncated-body":
                    self.send_header("Content-Length", "9999")
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.write(b'{"id":"partial"')
                    self.connection.shutdown(socket.SHUT_WR)
                    return
                if body.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    if mode == "bad-protocol":
                        self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()

                    def event(delta, finish=None):
                        frame = {"id": "local", "object": "chat.completion.chunk", "model": "local", "created": 1,
                                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                        return ("data: " + json.dumps(frame) + "\n\n").encode()

                    if mode == "slow":
                        stop.wait(0.15)
                    role_frame = event({"role": "assistant", "content": ""})
                    self.write((f"{len(role_frame):x}\r\n".encode() + role_frame + b"\r\n")
                               if mode == "bad-protocol" else role_frame)
                    if mode == "first-timeout":
                        for _ in range(40):
                            if stop.wait(0.02):
                                return
                            self.write(b": keepalive\n\n")
                        return
                    if mode == "bad-protocol":
                        stop.wait(0.02)
                        self.write(b"invalid-chunk-length\r\n")
                        return
                    self.write(event({"content": "OK"}))
                    if mode == "missing-finish":
                        return
                    if mode in {"idle-timeout", "cancel", "total-timeout"}:
                        for _ in range(80):
                            if stop.wait(0.02):
                                return
                            self.write(event({"content": "x"}) if mode == "total-timeout" else b": heartbeat\n\n")
                        return
                    self.write(event({}, "stop"))
                    self.write(b"data: [DONE]\n\n")
                    if mode == 'done-open':
                        stop.wait(0.4)  # SDK should close consumption after DONE, before HTTP EOF.
                    return
                payload = json.dumps({"id": "local", "object": "chat.completion", "created": 1, "model": "local",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.flush()
                if mode == "read-timeout":
                    stop.wait(0.4)
                self.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                disconnected.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{CANARY}/v1", requests, disconnected
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def assert_trace(value, mode, *, status=200):
    trace = safe_transport_diagnostic(value)
    assert trace is not None and trace == value
    assert trace["response_status_code"] == status
    assert trace["request_id_sha256"] == hashlib.sha256((CANARY + "-" + mode).encode()).hexdigest()
    assert CANARY not in json.dumps(trace)
    assert trace["milestones_ms"]["total"] >= trace["milestones_ms"]["response_headers"]
    assert "first_byte" not in trace["milestones_ms"]
    return trace


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("policy", [False, True])
async def test_success_trace_is_attached_once_for_regular_and_native_ainvoke(diagnostic_wire, native, policy):
    endpoint, requests, _ = diagnostic_wire
    config = configured(endpoint)
    if not policy:
        config = replace(config, parameters={})
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(config, http_async_client=client, native_streaming=native)
        message = await model.ainvoke([("user", "ok")])
        trace = assert_trace(message.response_metadata[DIAGNOSTIC_KEY], "ok")
        assert trace["response_started"] is True
        assert ("first_chunk" in trace["milestones_ms"]) is native
        assert (trace["transport_mode"] == "compatible-native-stream-v1") is native
        assert "headers" not in message.response_metadata
        assert model.max_retries == 0 and not model.include_response_headers
    assert len(requests) == 1


async def test_direct_astream_has_one_final_metadata_chunk_with_unmerged_hash_and_times(diagnostic_wire):
    endpoint, _, _ = diagnostic_wire
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint), http_async_client=client, native_streaming=True)
        chunks = [chunk async for chunk in model.astream([("user", "ok")])]
    assert sum(DIAGNOSTIC_KEY in chunk.response_metadata for chunk in chunks) == 1
    merged = chunks[0]
    for chunk in chunks[1:]:
        merged += chunk
    assert merged.content == "OK"
    trace = assert_trace(merged.response_metadata[DIAGNOSTIC_KEY], "ok")
    assert trace["milestones_ms"]["first_chunk"] <= trace["milestones_ms"]["first_semantic_progress"]


@pytest.mark.parametrize("mode,native,error_type", [
    ("truncated-body", False, Exception), ("bad-protocol", True, Exception),
    ("read-timeout", False, Exception), ("missing-finish", True, ProviderError),
])
async def test_headers_then_disconnect_or_protocol_fault_keeps_chain_and_acceptance_unknown(
        diagnostic_wire, mode, native, error_type):
    endpoint, requests, _ = diagnostic_wire
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint, read_seconds=0.08), http_async_client=client,
                                 native_streaming=native)
        with pytest.raises(error_type) as error:
            await model.ainvoke([("user", mode)])
    trace = assert_trace(getattr(error.value, DIAGNOSTIC_KEY), mode)
    assert trace["response_started"] is (mode in {"bad-protocol", "missing-finish"})
    assert trace["exception_chain"]
    assert classify_failure(error.value) is None
    if mode in {"truncated-body", "bad-protocol", "read-timeout"}:
        assert any(item["type"].startswith("httpcore.") for item in trace["exception_chain"])
        assert trace["last_network_phase"].endswith(".failed")
    if mode == "missing-finish":
        assert error.value.code == "OUTPUT_INCOMPLETE"
    assert CANARY not in json.dumps(failure_diagnostic(error.value, "ainvoke_default"))
    assert len(requests) == 1


@pytest.mark.parametrize("mode,phase,limits", [
    ("first-timeout", "first_chunk", {"first_chunk_seconds": 0.1}),
    ("idle-timeout", "stream_idle", {"idle_seconds": 0.1}),
    ("total-timeout", "total", {"total_seconds": 0.2, "first_chunk_seconds": 0.15, "idle_seconds": 0.15}),
])
async def test_native_timeouts_keep_observed_phase_and_model_progress(diagnostic_wire, mode, phase, limits):
    endpoint, requests, _ = diagnostic_wire
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint, **limits), http_async_client=client, native_streaming=True)
        with pytest.raises(TimeoutError) as error:
            await model.ainvoke([("user", mode)])
    trace = assert_trace(getattr(error.value, DIAGNOSTIC_KEY), mode)
    assert trace["timeout_phase"] == phase and trace["response_started"] is True
    assert ("first_semantic_progress" in trace["milestones_ms"]) is (phase != "first_chunk")
    assert classify_failure(error.value) is None and len(requests) == 1


async def test_complete_429_headers_are_distinct_from_model_response_started(diagnostic_wire):
    endpoint, requests, _ = diagnostic_wire
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint), http_async_client=client, native_streaming=True)
        with pytest.raises(Exception) as error:
            await model.ainvoke([("user", "rejected")])
    trace = assert_trace(getattr(error.value, DIAGNOSTIC_KEY), "rejected", status=429)
    assert trace["response_started"] is False and "first_chunk" not in trace["milestones_ms"]
    assert classify_failure(error.value).code == "HTTP_429_REJECTED" and len(requests) == 1


async def test_parallel_requests_use_independent_trace_and_context_is_reset_before_yield(diagnostic_wire):
    endpoint, _, _ = diagnostic_wire
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint), http_async_client=client, native_streaming=True)
        fast, slow = await asyncio.gather(model.ainvoke([("user", "fast")]), model.ainvoke([("user", "slow")]))
        first = assert_trace(fast.response_metadata[DIAGNOSTIC_KEY], "fast")
        second = assert_trace(slow.response_metadata[DIAGNOSTIC_KEY], "slow")
        assert second["milestones_ms"]["total"] > first["milestones_ms"]["total"] + 75
        stream = model.astream([("user", "ok")])
        try:
            await anext(stream)
            assert request_diagnostics.current_trace() is None
        finally:
            await stream.aclose()


async def test_cancel_preserves_original_cancel_and_closes_the_real_socket(diagnostic_wire):
    endpoint, requests, disconnected = diagnostic_wire
    received = asyncio.Event()
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint), http_async_client=client, native_streaming=True)

        async def consume():
            async for chunk in model.astream([("user", "cancel")]):
                if chunk.content:
                    received.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(received.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        trace = assert_trace(getattr(error.value, DIAGNOSTIC_KEY), "cancel")
        assert trace["response_started"] is True
        assert await asyncio.to_thread(disconnected.wait, 1)
    assert len(requests) == 1


async def test_broken_observation_does_not_replace_success_or_the_original_transport_error(diagnostic_wire, monkeypatch):
    endpoint, _, _ = diagnostic_wire

    def broken(*args, **kwargs):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(request_diagnostics.RequestTrace, "diagnostic", broken)
    async with httpx.AsyncClient(trust_env=False) as client:
        model = build_chat_model(configured(endpoint, read_seconds=0.08), http_async_client=client)
        message = await model.ainvoke([("user", "ok")])
        assert message.content == "OK"
        with pytest.raises(Exception) as error:
            await model.ainvoke([("user", "read-timeout")])
        assert not isinstance(error.value, RuntimeError)
        assert "timeout" in type(error.value).__name__.lower()
    assert request_diagnostics.current_trace() is None


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('mode', ['ok', 'truncated-body', 'read-timeout', 'rejected'])
async def test_sdk_default_httpx2_client_really_observes_loopback_without_transport_replacement(
        diagnostic_wire, native, mode):
    endpoint, requests, _ = diagnostic_wire
    model = build_chat_model(configured(endpoint, read_seconds=0.08), native_streaming=native)
    client = model.root_async_client._client
    try:
        # Do not supply http_async_client: this is the SDK's actual default
        # sending path, which the legacy-HTTPX-only tests did not exercise.
        assert isinstance(client, httpx2.AsyncClient) and not isinstance(client, httpx.AsyncClient)
        transport = client._transport
        request_diagnostics.install_httpx_hooks(model)
        assert client._transport is transport
        assert [hook.__name__ for hook in client.event_hooks['request']].count('_request_hook') == 1
        assert [hook.__name__ for hook in client.event_hooks['response']].count('_response_hook') == 1
        if mode == 'ok' or mode == 'read-timeout' and native:
            response = await model.ainvoke([('user', mode)])
            trace = assert_trace(response.response_metadata[DIAGNOSTIC_KEY], mode)
            assert trace['response_started'] is True
        else:
            with pytest.raises(Exception) as error:
                await model.ainvoke([('user', mode)])
            trace = assert_trace(getattr(error.value, DIAGNOSTIC_KEY), mode, status=429 if mode == 'rejected' else 200)
            assert trace['response_started'] is False
            if mode != 'rejected':
                types = {item['type'] for item in trace['exception_chain']}
                assert any(name.startswith('httpx2.') for name in types)
                assert any(name.startswith('httpcore2.') for name in types)
                assert trace['last_network_phase'].endswith('.failed')
                assert classify_failure(error.value) is None
            else:
                assert classify_failure(error.value).code == 'HTTP_429_REJECTED'
        assert 'request_start' in trace['milestones_ms']
        assert 'response_headers' in trace['milestones_ms']
        assert trace['last_network_phase'] != 'unknown'
        assert client._transport is transport
        assert len(requests) == 1
    finally:
        await model.root_async_client.close()
        model.root_client.close()
    assert client.is_closed


async def test_sdk_default_client_connection_unavailable_is_proven_not_dispatched(diagnostic_wire):
    _, requests, _ = diagnostic_wire
    # Keep the port bound without a listener: no HTTP can be accepted and no
    # unrelated process can claim it. Windows may time out rather than refuse.
    with socket.socket() as unavailable:
        unavailable.bind(('127.0.0.1', 0))
        endpoint = f'http://127.0.0.1:{unavailable.getsockname()[1]}/v1'
        model = build_chat_model(configured(endpoint, connect_seconds=0.2))
        try:
            with pytest.raises(Exception) as error:
                await model.ainvoke([('user', 'connection-refused')])
            failure = classify_failure(error.value)
            assert failure.code == 'CONNECT_NOT_DISPATCHED' and failure.retryable is True
            trace = getattr(error.value, DIAGNOSTIC_KEY)
            assert trace['response_status_code'] is None and trace['response_started'] is False
            assert 'request_start' in trace['milestones_ms'] and 'response_headers' not in trace['milestones_ms']
            assert any(item['type'] in {'httpx2.ConnectError', 'httpx2.ConnectTimeout'}
                       for item in trace['exception_chain'])
            assert trace['last_network_phase'] == 'connection.connect_tcp.failed'
        finally:
            await model.root_async_client.close()
            model.root_client.close()
    assert requests == []


async def test_sdk_default_client_real_pool_timeout_is_proven_not_dispatched(diagnostic_wire):
    endpoint, requests, _ = diagnostic_wire
    model = build_chat_model(configured(endpoint, pool_seconds=0.08), native_streaming=True)
    client = model.root_async_client._client
    # Test-only pool capacity on the actual SDK-created transport. Production
    # construction, transport selection and connection settings stay unchanged.
    pool = client._transport._pool
    pool._max_connections = 1
    stream = model.astream([('user', 'cancel')])
    try:
        await anext(stream)  # Response is intentionally held open on connection one.
        with pytest.raises(Exception) as error:
            await model.ainvoke([('user', 'ok')])
        failure = classify_failure(error.value)
        assert failure.code == 'POOL_NOT_DISPATCHED' and failure.retryable is True
        trace = getattr(error.value, DIAGNOSTIC_KEY)
        assert trace['response_status_code'] is None and trace['response_started'] is False
        assert any(item['type'] == 'httpx2.PoolTimeout' for item in trace['exception_chain'])
        assert len(requests) == 1  # Timed-out pool acquisition never reached the server.
    finally:
        await stream.aclose()
        await model.root_async_client.close()
        model.root_client.close()


@pytest.mark.parametrize('error', [httpx2.ReadTimeout(CANARY), httpx2.WriteTimeout(CANARY),
                                  httpx2.ReadError(CANARY), httpx2.WriteError(CANARY)])
def test_httpx2_ambiguous_io_still_blocks_recovery_even_with_connect_cause(error):
    error.__cause__ = httpx2.ConnectError(CANARY)
    assert classify_failure(error) is None


@pytest.mark.parametrize('attribute,value', [('omni_response_started', True), ('omni_timeout_phase', 'total'),
                                            ('omni_timeout_phase', 'first_chunk'), ('omni_timeout_phase', 'stream_idle')])
def test_httpx2_observed_output_and_host_deadlines_still_block_recovery(attribute, value):
    error = httpx2.ConnectError(CANARY)
    setattr(error, attribute, value)
    assert classify_failure(error) is None


@pytest.mark.parametrize('direct_stream', [False, True])
async def test_default_sdk_done_before_http_eof_is_consumer_close_not_remote_failure(
        diagnostic_wire, direct_stream):
    endpoint, requests, _ = diagnostic_wire
    model = build_chat_model(configured(endpoint), native_streaming=True)
    events = []

    async def observe(name, info):
        # Test observer inspects only a fixed allowlisted exception type, not
        # str(exception), wire payload, headers or arbitrary metadata.
        if name.endswith('.failed'):
            error = info.get('exception') if isinstance(info, dict) else None
            events.append((name, 'GeneratorExit' if isinstance(error, GeneratorExit) else 'other'))

    async def request(request):
        request.extensions['trace'] = observe

    model.root_async_client._client.event_hooks['request'].insert(0, request)
    try:
        if direct_stream:
            chunks = [chunk async for chunk in model.astream([('user', 'done-open')])]
            assert ''.join(chunk.content for chunk in chunks) == 'OK'
            trace = assert_trace(chunks[-1].response_metadata[DIAGNOSTIC_KEY], 'done-open')
        else:
            response = await model.ainvoke([('user', 'done-open')])
            assert response.content == 'OK'
            trace = assert_trace(response.response_metadata[DIAGNOSTIC_KEY], 'done-open')
        assert trace['exception_chain'] == []
        if not direct_stream:
            assert ('http11.receive_response_body.failed', 'GeneratorExit') in events
            assert trace['last_network_phase'] == 'response_stream.consumer_closed'
        else:
            # Direct astream may defer generator finalization beyond its last
            # metadata snapshot. Never invent a close or remote EOF event.
            assert not trace['last_network_phase'].endswith('.failed')
        assert trace['response_started'] is True and len(requests) == 1
    finally:
        await model.root_async_client.close()
        model.root_client.close()


async def test_default_sdk_cancel_still_has_real_cancelled_error_not_success(diagnostic_wire):
    endpoint, requests, disconnected = diagnostic_wire
    model = build_chat_model(configured(endpoint), native_streaming=True)
    received = asyncio.Event()

    async def consume():
        async for chunk in model.astream([('user', 'cancel')]):
            if chunk.content:
                received.set()

    try:
        task = asyncio.create_task(consume())
        await asyncio.wait_for(received.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        trace = assert_trace(getattr(error.value, DIAGNOSTIC_KEY), 'cancel')
        assert trace['exception_chain'][0]['type'] == 'CancelledError'
        assert trace['response_started'] is True
        assert classify_failure(error.value) is None
        assert len(requests) == 1 and await asyncio.to_thread(disconnected.wait, 1)
    finally:
        await model.root_async_client.close()
        model.root_client.close()
