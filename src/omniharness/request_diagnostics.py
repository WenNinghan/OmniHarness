"""Bounded transport observations. Never persist exception text or wire content."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import re
import socket
import ssl
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpcore
import httpcore2
import httpx
import httpx2
import openai

DIAGNOSTIC_KEY = "omni_transport_diagnostic"
_MODES = {"ainvoke_default", "compatible-native-stream-v1", "unknown"}
_PHASES = {"first_chunk", "stream_idle", "total"}
_MILESTONES = {"request_start", "response_headers", "first_chunk", "first_semantic_progress",
               "last_semantic_progress", "total"}
_NETWORK_PHASES = {"unknown", "response_stream.consumer_closed"} | {
    f"{prefix}.{phase}.{event}"
    for prefix, phases in {
        "connection": ("connect_tcp", "connect_unix_socket", "start_tls"),
        "http11": ("send_request_headers", "send_request_body", "receive_response_headers",
                   "receive_response_body", "response_closed"),
        "http2": ("send_connection_init", "send_request_headers", "send_request_body",
                  "receive_response_headers", "receive_response_body", "response_closed"),
    }.items() for phase in phases for event in ("started", "complete", "failed")
}
_ERROR_TYPES = (
    (asyncio.CancelledError, "CancelledError"),
    (GeneratorExit, "GeneratorExit"),
    *((getattr(httpx, name), "httpx." + name) for name in (
        "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectError", "ReadError",
        "WriteError", "RemoteProtocolError", "LocalProtocolError", "ProxyError", "UnsupportedProtocol",
        "DecodingError", "TooManyRedirects", "HTTPStatusError")),
    *((getattr(httpcore, name), "httpcore." + name) for name in (
        "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectError", "ReadError",
        "WriteError", "RemoteProtocolError", "LocalProtocolError", "ProxyError", "UnsupportedProtocol")),
    *((getattr(httpx2, name), "httpx2." + name) for name in (
        "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectError", "ReadError",
        "WriteError", "RemoteProtocolError", "LocalProtocolError", "ProxyError", "UnsupportedProtocol",
        "DecodingError", "TooManyRedirects", "HTTPStatusError")),
    *((getattr(httpcore2, name), "httpcore2." + name) for name in (
        "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectError", "ReadError",
        "WriteError", "RemoteProtocolError", "LocalProtocolError", "ProxyError", "UnsupportedProtocol")),
    *((getattr(openai, name), "openai." + name) for name in (
        "APITimeoutError", "RateLimitError", "AuthenticationError", "PermissionDeniedError", "NotFoundError",
        "BadRequestError", "UnprocessableEntityError", "InternalServerError", "APIStatusError",
        "APIConnectionError", "APIResponseValidationError", "APIError")),
    (socket.gaierror, "socket.gaierror"), (socket.herror, "socket.herror"),
    (ssl.SSLCertVerificationError, "ssl.SSLCertVerificationError"), (ssl.SSLError, "ssl.SSLError"),
    (ConnectionResetError, "ConnectionResetError"), (ConnectionAbortedError, "ConnectionAbortedError"),
    (ConnectionRefusedError, "ConnectionRefusedError"), (BrokenPipeError, "BrokenPipeError"),
    (TimeoutError, "TimeoutError"), (ConnectionError, "ConnectionError"), (OSError, "OSError"),
)
_LOCAL_TYPES = {"AgentBoundaryError", "AgentExternalCallFailed", "ProviderError"}
_TYPE_NAMES = {name for _, name in _ERROR_TYPES} | _LOCAL_TYPES | {"SQLAlchemyError", "other"}
_OS_TYPE_NAMES = {"socket.gaierror", "socket.herror", "ssl.SSLCertVerificationError", "ssl.SSLError",
                  "ConnectionResetError", "ConnectionAbortedError", "ConnectionRefusedError", "BrokenPipeError",
                  "TimeoutError", "ConnectionError", "OSError"}
_CURRENT_TRACE: ContextVar[RequestTrace | None] = ContextVar("omni_request_trace", default=None)


def exception_chain(error):
    """Visit cause before context, including suppressed context, once per object."""
    pending, seen, result = [(error, "root")], set(), []
    while pending and len(result) < 8:
        item, relation = pending.pop()
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        result.append((item, relation))
        with suppress(Exception):
            pending.append((item.__context__, "context"))
            pending.append((item.__cause__, "cause"))
    return result


def _type_name(error):
    for cls, name in _ERROR_TYPES:
        if isinstance(error, cls):
            return name
    from sqlalchemy.exc import SQLAlchemyError

    from .m3_agents import AgentBoundaryError, AgentExternalCallFailed
    from .providers import ProviderError
    for cls, name in ((AgentExternalCallFailed, "AgentExternalCallFailed"),
                      (AgentBoundaryError, "AgentBoundaryError"), (ProviderError, "ProviderError"),
                      (SQLAlchemyError, "SQLAlchemyError")):
        if isinstance(error, cls):
            return name
    return "other"


def _request_id(headers):
    if not isinstance(headers, (httpx.Headers, httpx2.Headers)):
        return None
    for key in ("x-request-id", "request-id", "openai-request-id", "x-amzn-requestid"):
        value = headers.get(key)
        if isinstance(value, str) and 0 < len(value) <= 512:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return None


def _chain_item(error, relation):
    item = {"type": _type_name(error), "relation": relation}
    if isinstance(error, OSError):
        number = getattr(error, "errno", None)
        if type(number) is int and -65535 <= number <= 65535:
            item["errno"] = number
    return item


def _validated_transport_diagnostic(value) -> dict | None:
    """Validate the entire small namespace; unknown keys or values fail closed."""
    keys = {"schema_version", "transport_mode", "exception_chain", "timeout_phase", "response_started",
            "response_status_code", "request_id_sha256", "milestones_ms", "last_network_phase"}
    if (not isinstance(value, dict) or set(value) != keys or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or not isinstance(value.get("transport_mode"), str)
            or value["transport_mode"] not in _MODES):
        return None
    chain, times = value.get("exception_chain"), value.get("milestones_ms")
    if (not isinstance(chain, list) or len(chain) > 8 or not isinstance(times, dict)
            or set(times) - _MILESTONES):
        return None
    copied = []
    for item in chain:
        if (not isinstance(item, dict) or not {"type", "relation"}.issubset(item)
                or set(item) - {"type", "relation", "errno"}
                or not isinstance(item.get("type"), str) or item["type"] not in _TYPE_NAMES
                or not isinstance(item.get("relation"), str) or item["relation"] not in {"root", "cause", "context"}):
            return None
        if "errno" in item and (item["type"] not in _OS_TYPE_NAMES or type(item["errno"]) is not int
                                or not -65535 <= item["errno"] <= 65535):
            return None
        copied.append(dict(item))
    for number in times.values():
        if (type(number) not in (int, float) or not 0 <= number <= 86_400_000 or not math.isfinite(number)):
            return None
    phase, started, fingerprint = (value.get(key) for key in (
        "timeout_phase", "response_started", "request_id_sha256"))
    status = value.get("response_status_code")
    if (phase is not None and (not isinstance(phase, str) or phase not in _PHASES)
            or started is not None and type(started) is not bool
            or status is not None and (type(status) is not int or not 100 <= status <= 599)
            or fingerprint is not None and (not isinstance(fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None)
            or not isinstance(value.get("last_network_phase"), str)
            or value["last_network_phase"] not in _NETWORK_PHASES):
        return None
    return {"schema_version": 1, "transport_mode": value["transport_mode"], "exception_chain": copied,
            "timeout_phase": phase, "response_started": started, "response_status_code": status,
            "request_id_sha256": fingerprint,
            "milestones_ms": dict(times), "last_network_phase": value["last_network_phase"]}


def safe_transport_diagnostic(value) -> dict | None:
    try:
        return _validated_transport_diagnostic(value)
    except Exception:
        return None


@dataclass
class RequestTrace:
    transport_mode: str = "unknown"
    started_at: float = field(default_factory=time.monotonic)
    milestones: dict = field(default_factory=dict)
    response_started: bool = False
    request_id_sha256: str | None = None
    response_status_code: int | None = None
    last_network_phase: str = "unknown"
    network_failed: bool = False
    consumer_closed: bool = False
    hard_network_failed: bool = False

    def mark(self, name, *, replace=False):
        if name in _MILESTONES and (replace or name not in self.milestones):
            self.milestones[name] = min(86_400_000.0, round(max(0.0, time.monotonic() - self.started_at) * 1000, 3))

    def chunk(self, *, semantic=False):
        self.response_started = True
        self.mark("first_chunk")
        if semantic:
            self.mark("first_semantic_progress")
            self.mark("last_semantic_progress", replace=True)

    def model_response(self, *, semantic=False):
        self.response_started = True
        if semantic:
            self.mark("first_semantic_progress")
            self.mark("last_semantic_progress", replace=True)

    def network(self, name, *, failure_type=None):
        if name not in _NETWORK_PHASES or name == "response_stream.consumer_closed":
            return
        if name.endswith(".failed"):
            body_close = name in {"http11.receive_response_body.failed", "http2.receive_response_body.failed"}
            previous_failure, previous_hard = self.network_failed, self.hard_network_failed
            if body_close and failure_type == "generator_exit":
                # Record this even after an earlier internal cancellation.
                self.consumer_closed = True
            elif not (body_close and failure_type == "cancelled"):
                # Unknown failures are conservative too: only these exact
                # body-consumer control types can be cleanup observations.
                self.hard_network_failed = True
            self.network_failed = True
            if not previous_failure or self.hard_network_failed and not previous_hard:
                self.last_network_phase = name
            return
        if not self.network_failed:
            self.last_network_phase = name

    def diagnostic(self, error=None):
        self.mark("total", replace=True)
        chain = exception_chain(error) if error is not None else []
        phase = next((getattr(item, "omni_timeout_phase", None) for item, _ in chain
                      if isinstance(getattr(item, "omni_timeout_phase", None), str)
                      and getattr(item, "omni_timeout_phase") in _PHASES), None)
        started = self.response_started or any(getattr(item, "omni_response_started", False) is True
                                               for item, _ in chain)
        # Linux HTTPcore may report CancelledError then GeneratorExit while the
        # SDK closes a successful [DONE] stream. Only an actual successful call,
        # explicit GeneratorExit, and no other failure may explain that cleanup.
        # Error/cancellation diagnostics retain the observed *.failed phase.
        network_phase = ("response_stream.consumer_closed" if error is None and self.consumer_closed
                         and not self.hard_network_failed else self.last_network_phase)
        return safe_transport_diagnostic({"schema_version": 1,
            "transport_mode": self.transport_mode if isinstance(self.transport_mode, str)
                and self.transport_mode in _MODES else "unknown",
            "exception_chain": [_chain_item(item, relation) for item, relation in chain],
            "timeout_phase": phase, "response_started": started, "response_status_code": self.response_status_code,
            "request_id_sha256": self.request_id_sha256,
            "milestones_ms": self.milestones, "last_network_phase": network_phase})


@contextmanager
def active_trace(trace):
    token = _CURRENT_TRACE.set(trace)
    try:
        yield trace
    finally:
        _CURRENT_TRACE.reset(token)


def current_trace():
    return _CURRENT_TRACE.get()


async def _request_hook(request):
    trace = current_trace()
    if trace is None:
        return
    with suppress(Exception):
        trace.mark("request_start")
        previous = request.extensions.get("trace")

        async def callback(name, info):
            with suppress(Exception):
                # The SDK stops consuming at [DONE]; closing that async body
                # iterator emits GeneratorExit through HTTPcore's *.failed
                # trace even when the model completed successfully. Inspect
                # only its fixed type, never text, response bytes or other info.
                error = info.get("exception") if isinstance(info, dict) else None
                failure_type = ("generator_exit" if isinstance(error, GeneratorExit) else
                                "cancelled" if isinstance(error, asyncio.CancelledError) else "other")
                trace.network(name, failure_type=failure_type)
            if previous is not None:
                result = previous(name, info)
                if not inspect.iscoroutine(result):
                    raise TypeError("Asynchronous trace callback must return a coroutine")
                await result

        request.extensions["trace"] = callback


async def _response_hook(response):
    trace = current_trace()
    if trace is not None:
        with suppress(Exception):
            trace.mark("response_headers")  # Headers received, never advertised as first byte.
            trace.response_status_code = response.status_code
            trace.request_id_sha256 = _request_id(response.headers)


def install_httpx_hooks(model):
    """Reuse the SDK-owned client and lifecycle; never construct another client."""
    with suppress(Exception):
        client = getattr(getattr(model, "root_async_client", None), "_client", None)
        # The pinned OpenAI SDK uses HTTPX 2 by default; it also accepts a
        # caller-owned legacy HTTPX client. Observe both without replacing
        # either transport, its settings, or its lifecycle.
        if not isinstance(client, (httpx.AsyncClient, httpx2.AsyncClient)):
            return
        if _request_hook not in client.event_hooks["request"]:
            client.event_hooks["request"].append(_request_hook)
        if _response_hook not in client.event_hooks["response"]:
            client.event_hooks["response"].append(_response_hook)


def attach_error(error, trace):
    with suppress(Exception):
        setattr(error, DIAGNOSTIC_KEY, trace.diagnostic(error))


def _failure_diagnostic(exc, transport_mode) -> dict:
    """Keep original classifications, including recognizable sanitized causes."""
    from sqlalchemy.exc import SQLAlchemyError

    from .m3_agents import AgentBoundaryError
    from .providers import _safe_error
    chain = exception_chain(exc)
    selected = next((item for item, _ in chain if isinstance(item, (
        asyncio.CancelledError, TimeoutError, httpx.HTTPError, httpx2.HTTPError, openai.APIError, ConnectionError,
        SQLAlchemyError))), exc)
    # A sanitized host wrapper may hide the actual SDK failure in __context__.
    code = _safe_error(selected).code
    allowed = {"DEPENDENCY_MISSING", "PROVIDER_TIMEOUT", "PROVIDER_AUTH", "PROVIDER_RATE_LIMIT",
               "PROVIDER_NOT_FOUND", "PROVIDER_REQUEST_REJECTED", "PROVIDER_UNAVAILABLE",
               "PROVIDER_CONNECTION", "PROVIDER_ERROR"}
    if code not in allowed:
        code = "PROVIDER_ERROR"
    status = getattr(selected, "status_code", None)
    if type(status) is not int:
        status = getattr(getattr(selected, "response", None), "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    if isinstance(selected, asyncio.CancelledError):
        category = "cancelled"
    elif isinstance(selected, (TimeoutError, httpx.TimeoutException, httpx2.TimeoutException, openai.APITimeoutError)):
        category = "timeout"
    elif isinstance(selected, (httpx.HTTPStatusError, httpx2.HTTPStatusError, openai.APIStatusError)):
        category = "http_status"
    elif isinstance(selected, (ConnectionError, httpx.NetworkError, httpx2.NetworkError, openai.APIConnectionError)):
        category = "connection"
    elif isinstance(selected, SQLAlchemyError):
        category = "database"
    elif isinstance(selected, AgentBoundaryError):
        category = "boundary"
    else:
        category = "unknown"
    trace = next((safe_transport_diagnostic(getattr(item, DIAGNOSTIC_KEY, None)) for item, _ in chain
                  if safe_transport_diagnostic(getattr(item, DIAGNOSTIC_KEY, None)) is not None), None)
    if trace is None:
        trace = RequestTrace(transport_mode=transport_mode).diagnostic(exc)
        trace["milestones_ms"] = {}  # No invented transport duration for uninstrumented failures.
        trace["response_started"] = next((getattr(item, "omni_response_started") for item, _ in chain
                                           if type(getattr(item, "omni_response_started", None)) is bool), None)
    else:
        trace["exception_chain"] = [_chain_item(item, relation) for item, relation in chain]
    return {"error_code": code, "http_status": status, "exception_category": category,
            "transport_mode": transport_mode if isinstance(transport_mode, str)
                and transport_mode in _MODES else "unknown",
            "transport_diagnostic": trace}


def failure_diagnostic(exc, transport_mode) -> dict:
    try:
        return _failure_diagnostic(exc, transport_mode)
    except Exception:
        # Diagnostics must never mask the model or cancellation outcome.
        return {"error_code": "PROVIDER_ERROR", "http_status": None, "exception_category": "unknown",
                "transport_mode": transport_mode if isinstance(transport_mode, str)
                    and transport_mode in _MODES else "unknown", "transport_diagnostic": None}


def success_diagnostic(result_dict) -> dict | None:
    if not isinstance(result_dict, dict):
        return None
    messages = [result_dict["message"]] if isinstance(result_dict.get("message"), dict) else result_dict.get("messages")
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict):
        return None
    metadata = messages[0].get("response_metadata")
    return safe_transport_diagnostic(metadata.get(DIAGNOSTIC_KEY)) if isinstance(metadata, dict) else None
