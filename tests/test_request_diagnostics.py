"""Safe diagnostic contracts and observers never expose untrusted wire content."""
import asyncio
import errno
import json
from copy import deepcopy
from types import SimpleNamespace

import httpcore
import httpx
import httpx2
import openai
import pytest

from omniharness.m3_agents import AgentExternalCallFailed
from omniharness.request_diagnostics import (
    DIAGNOSTIC_KEY,
    RequestTrace,
    active_trace,
    current_trace,
    exception_chain,
    failure_diagnostic,
    install_httpx_hooks,
    safe_transport_diagnostic,
    success_diagnostic,
)

CANARY = "secret-canary-must-never-be-recorded"


def sample():
    return {"schema_version": 1, "transport_mode": "ainvoke_default", "exception_chain": [],
            "timeout_phase": None, "response_started": None, "response_status_code": None,
            "request_id_sha256": None, "milestones_ms": {}, "last_network_phase": "unknown"}


@pytest.mark.parametrize("key,value", [
    ("schema_version", True), ("schema_version", 2), ("transport_mode", []),
    ("transport_mode", {}), ("transport_mode", True),
    ("transport_mode", CANARY), ("exception_chain", [{"type": CANARY, "relation": "root"}]),
    ("exception_chain", [{"type": "other", "relation": CANARY}]),
    ("exception_chain", [{"type": {}, "relation": "root"}]),
    ("exception_chain", [{"type": "other", "relation": []}]),
    ("exception_chain", [{"type": "other", "relation": "root", "text": CANARY}]),
    ("exception_chain", [{"type": "other", "relation": "cause"}] * 9),
    ("timeout_phase", CANARY), ("timeout_phase", []), ("timeout_phase", {}), ("timeout_phase", False),
    ("response_started", 1),
    ("response_status_code", True), ("response_status_code", 99), ("response_status_code", 600),
    ("request_id_sha256", CANARY), ("milestones_ms", {CANARY: 1}),
    ("milestones_ms", {"total": True}), ("milestones_ms", {"total": float("nan")}),
    ("milestones_ms", {"total": float("inf")}), ("milestones_ms", {"total": 10 ** 1000}),
    ("milestones_ms", {"total": -1}), ("last_network_phase", "https://" + CANARY),
    ("last_network_phase", []), ("last_network_phase", {}), ("last_network_phase", True),
])
def test_strict_trace_rejects_unbounded_untrusted_or_unknown_fields(key, value):
    trace = sample()
    trace[key] = value
    assert safe_transport_diagnostic(trace) is None
    assert safe_transport_diagnostic({**sample(), "body": CANARY}) is None


def test_safe_trace_is_a_detached_copy_and_success_reads_only_exact_single_message():
    trace = sample()
    trace.update(exception_chain=[{"type": "httpcore.ReadError", "relation": "cause"}],
                 response_status_code=200, milestones_ms={"response_headers": 4, "total": 8})
    safe = safe_transport_diagnostic(trace)
    safe["exception_chain"][0]["type"] = "other"
    safe["milestones_ms"]["total"] = 99
    assert trace["exception_chain"][0]["type"] == "httpcore.ReadError"
    assert trace["milestones_ms"]["total"] == 8
    message = {"content": CANARY, "response_metadata": {DIAGNOSTIC_KEY: trace, "body": CANARY}}
    assert success_diagnostic({"message": message}) == trace
    assert success_diagnostic({"messages": [message]}) == trace
    assert success_diagnostic({"messages": [message, message]}) is None
    assert success_diagnostic({"message": {"content": CANARY}}) is None


def test_cause_and_suppressed_context_are_bounded_and_cyclic_safe():
    errors = [RuntimeError(CANARY) for _ in range(12)]
    for earlier, later in zip(errors, errors[1:]):
        earlier.__cause__ = later
    errors[-1].__context__ = errors[0]
    errors[0].__context__ = httpcore.ReadError(CANARY)
    chain = exception_chain(errors[0])
    assert len(chain) == 8
    assert all(relation in {"root", "cause", "context"} for _, relation in chain)


def test_sanitized_wrapper_preserves_recognizable_sdk_and_httpcore_cause_types():
    request = httpx.Request("POST", "https://" + CANARY + ".invalid/path")
    sdk = openai.APIConnectionError(message=CANARY, request=request)
    sdk.__cause__ = httpx.ReadError(CANARY)
    sdk.__cause__.__cause__ = httpcore.ReadError(CANARY)
    wrapper = AgentExternalCallFailed(CANARY)
    wrapper.__context__ = sdk
    wrapper.__suppress_context__ = True
    result = failure_diagnostic(wrapper, "ainvoke_default")
    assert result["error_code"] == "PROVIDER_CONNECTION" and result["exception_category"] == "connection"
    assert result["http_status"] is None
    trace = result["transport_diagnostic"]
    assert trace["response_started"] is None and trace["milestones_ms"] == {}
    assert [item["type"] for item in trace["exception_chain"]] == [
        "AgentExternalCallFailed", "openai.APIConnectionError", "httpx.ReadError", "httpcore.ReadError"]
    assert CANARY not in json.dumps(result)


def test_failure_extends_original_classification_and_does_not_invent_observations():
    error = TimeoutError(CANARY)
    error.omni_timeout_phase = "total"
    error.omni_response_started = False
    result = failure_diagnostic(error, "compatible-native-stream-v1")
    assert {key: result[key] for key in ("error_code", "exception_category", "http_status", "transport_mode")} == {
        "error_code": "PROVIDER_TIMEOUT", "exception_category": "timeout", "http_status": None,
        "transport_mode": "compatible-native-stream-v1"}
    assert result["transport_diagnostic"]["timeout_phase"] == "total"
    assert result["transport_diagnostic"]["response_started"] is False
    assert result["transport_diagnostic"]["response_status_code"] is None
    assert result["transport_diagnostic"]["milestones_ms"] == {}


async def test_context_trace_is_request_local_and_resets_after_errors():
    first, second = RequestTrace(), RequestTrace()
    gate = asyncio.Event()

    async def observe(trace):
        with active_trace(trace):
            gate.set()
            await asyncio.sleep(0)
            assert current_trace() is trace
        assert current_trace() is None

    await asyncio.gather(observe(first), observe(second))
    await gate.wait()
    assert current_trace() is None
    with pytest.raises(RuntimeError):
        with active_trace(first):
            raise RuntimeError(CANARY)
    assert current_trace() is None


async def test_httpx_observer_preserves_existing_hooks_and_trace_callback():
    events, headers = [], []

    async def previous(name, info):
        events.append(name)

    async def request_hook(request):
        request.extensions["trace"] = previous
        headers.append("request")

    async def response_hook(response):
        headers.append("response")

    async with httpx.AsyncClient(event_hooks={"request": [request_hook], "response": [response_hook]}) as client:
        model = SimpleNamespace(root_async_client=SimpleNamespace(_client=client))
        install_httpx_hooks(model)
        install_httpx_hooks(model)
        assert len(client.event_hooks["request"]) == 2 and len(client.event_hooks["response"]) == 2
        trace = RequestTrace()
        request = httpx.Request("POST", "http://" + CANARY + ".invalid")
        with active_trace(trace):
            for hook in client.event_hooks["request"]:
                await hook(request)
            await request.extensions["trace"]("http11.receive_response_body.failed", {"body": CANARY})
            response = httpx.Response(200, headers={"x-request-id": CANARY}, request=request)
            for hook in client.event_hooks["response"]:
                await hook(response)
        diagnostic = trace.diagnostic()
        assert events == ["http11.receive_response_body.failed"] and headers == ["request", "response"]
        assert diagnostic["response_status_code"] == 200 and diagnostic["response_started"] is False
        assert len(diagnostic["request_id_sha256"]) == 64
        assert CANARY not in json.dumps(diagnostic)
        assert diagnostic["last_network_phase"] == "http11.receive_response_body.failed"
        assert not client.is_closed
    assert client.is_closed


def test_failure_trace_does_not_mutate_preexisting_safe_metadata():
    error = httpx.ReadError(CANARY)
    trace = sample()
    trace["milestones_ms"] = {"total": 5}
    setattr(error, DIAGNOSTIC_KEY, deepcopy(trace))
    result = failure_diagnostic(error, "ainvoke_default")
    assert result["transport_diagnostic"]["exception_chain"][0]["type"] == "httpx.ReadError"
    assert getattr(error, DIAGNOSTIC_KEY) == trace


def test_consumer_close_is_distinct_from_eof_and_real_failure_remains_sticky():
    trace = RequestTrace()
    trace.network('http11.receive_response_body.failed', failure_type='generator_exit')
    trace.network('http11.response_closed.complete')
    assert trace.diagnostic()['last_network_phase'] == 'response_stream.consumer_closed'
    assert trace.hard_network_failed is False
    trace.network('http11.receive_response_body.failed')
    trace.network('http11.receive_response_body.failed', failure_type='generator_exit')
    trace.network('http11.response_closed.complete')
    assert trace.diagnostic()['last_network_phase'] == 'http11.receive_response_body.failed'
    assert trace.network_failed is True


def test_linux_cancel_then_generator_exit_is_explained_only_for_actual_success():
    trace = RequestTrace()
    trace.network('http11.receive_response_body.failed', failure_type='cancelled')
    assert trace.diagnostic()['last_network_phase'] == 'http11.receive_response_body.failed'
    trace.network('http11.receive_response_body.failed', failure_type='generator_exit')
    trace.network('http11.response_closed.complete')
    assert trace.diagnostic()['last_network_phase'] == 'response_stream.consumer_closed'
    for error in (asyncio.CancelledError(), TimeoutError(), RuntimeError()):
        assert trace.diagnostic(error)['last_network_phase'] == 'http11.receive_response_body.failed'


@pytest.mark.parametrize('hard_before_close', [False, True])
def test_real_or_unknown_network_failure_is_never_relabelled_as_cleanup(hard_before_close):
    trace = RequestTrace()
    trace.network('http11.receive_response_body.failed', failure_type='cancelled')
    if hard_before_close:
        trace.network('http11.receive_response_body.failed', failure_type='other')
    trace.network('http11.receive_response_body.failed', failure_type='generator_exit')
    if not hard_before_close:
        trace.network('http11.receive_response_body.failed', failure_type='other')
    trace.network('http11.response_closed.complete')
    assert trace.diagnostic()['last_network_phase'] == 'http11.receive_response_body.failed'


async def test_hook_exact_linux_cancel_then_generator_exit_preserves_outer_cancel():
    trace = RequestTrace()
    async with httpx2.AsyncClient() as client:
        install_httpx_hooks(SimpleNamespace(root_async_client=SimpleNamespace(_client=client)))
        request = httpx2.Request('POST', 'http://127.0.0.1:1')
        with active_trace(trace):
            await client.event_hooks['request'][-1](request)
            callback = request.extensions['trace']
            await callback('http11.receive_response_body.failed', {'exception': asyncio.CancelledError(CANARY)})
            assert trace.diagnostic()['last_network_phase'] == 'http11.receive_response_body.failed'
            await callback('http11.receive_response_body.failed', {'exception': GeneratorExit(CANARY)})
            await callback('http11.response_closed.complete', {'return_value': None})
        assert trace.diagnostic()['last_network_phase'] == 'response_stream.consumer_closed'
        cancelled = trace.diagnostic(asyncio.CancelledError(CANARY))
        assert cancelled['last_network_phase'] == 'http11.receive_response_body.failed'
        assert cancelled['exception_chain'][0]['type'] == 'CancelledError'
        assert CANARY not in json.dumps(cancelled)


@pytest.mark.parametrize('error,label', [
    (ConnectionResetError(errno.ECONNRESET, CANARY), 'ConnectionResetError'),
    (ConnectionAbortedError(errno.ECONNABORTED, CANARY), 'ConnectionAbortedError'),
    (ConnectionRefusedError(errno.ECONNREFUSED, CANARY), 'ConnectionRefusedError'),
    (BrokenPipeError(errno.EPIPE, CANARY), 'BrokenPipeError'),
    (TimeoutError(errno.ETIMEDOUT, CANARY), 'TimeoutError'),
    (OSError(errno.EIO, CANARY), 'OSError'),
])
def test_os_error_static_subtype_and_exact_errno_are_saved_without_message(error, label):
    result = failure_diagnostic(error, 'ainvoke_default')
    chain = result['transport_diagnostic']['exception_chain']
    assert chain[0] == {'type': label, 'relation': 'root', 'errno': error.errno}
    assert CANARY not in json.dumps(result)


@pytest.mark.parametrize('number', [True, False, None, 65536, -65536, 10 ** 1000, 1.0, CANARY, [], {}])
def test_errno_is_strictly_bounded_integer_and_never_echoes_invalid_values(number):
    trace = sample()
    trace['exception_chain'] = [{'type': 'OSError', 'relation': 'root', 'errno': number}]
    assert safe_transport_diagnostic(trace) is None


def test_invalid_os_errno_attribute_is_omitted_rather_than_copied():
    error = OSError(CANARY)
    error.errno = True
    trace = failure_diagnostic(error, 'ainvoke_default')['transport_diagnostic']
    assert trace['exception_chain'][0] == {'type': 'OSError', 'relation': 'root'}
    assert CANARY not in json.dumps(trace)
    trace['exception_chain'] = [{'type': 'httpx2.ReadError', 'relation': 'root', 'errno': errno.ECONNRESET}]
    assert safe_transport_diagnostic(trace) is None
