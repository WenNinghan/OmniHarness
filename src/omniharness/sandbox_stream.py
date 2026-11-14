"""Fail-closed response hooks for the pinned OpenSandbox 1.2.0 client.

The SDK owns command execution and SSE dispatch. This hook validates bounded
frames before they reach its permissive parser, which otherwise logs and skips
malformed events. Accepted bytes are passed through unchanged.
"""
from __future__ import annotations

import re
from codecs import BOM_UTF8
from collections.abc import Awaitable, Callable

import httpx
from httpx_sse import EventSource
from opensandbox.adapters.converter.event_node import EventNode
from opensandbox.exceptions import SandboxApiException, SandboxException


class SandboxStreamError(SandboxException):
    """Messages are fixed strings; never include a response or parser error."""


async def _close_response(response: httpx.Response) -> None:
    try:
        await response.aclose()
    except Exception:
        raise SandboxStreamError("Sandbox response could not be closed") from None


# CRLF is indivisible: a single CRLF is not an empty SSE line.
_FRAME_END = re.compile(rb"(?:\r\n|\n|\r(?!\n))(?:\r\n|\n|\r(?!\n))")
# ServerStreamEventType in the pinned execd BOM also emits ping/status. The
# SDK intentionally does not dispatch these informational events to callbacks.
_EVENT_TYPES = frozenset({"init", "stdout", "stderr", "result", "error", "ping", "status",
                          "execution_complete", "execution_count"})


class _EventValidation:
    def __init__(self):
        self.legacy: bool | None = None
        self.started = False
        self.completed = False
        self.authoritative_error = False
        self.first = True

    def frame(self, raw: bytes) -> None:
        data = raw
        if self.first and data.startswith(BOM_UTF8):
            data = data[len(BOM_UTF8):]
        self.first = False
        if self.legacy is None and data.strip():
            self.legacy = data.lstrip()[:1] in (b"{", b"[")
        try:
            # Strict UTF-8 avoids the SDK decoder replacing invalid bytes.
            decoded = data.decode("utf-8")
            if self.legacy:
                # The SDK's legacy normalizer supports LF-separated JSON only.
                if not data.endswith(b"\n\n"):
                    raise ValueError
                payloads = [decoded.strip()]
            else:
                # Reject ignored fields, rather than letting malformed data
                # disappear inside the tolerant SSE decoder.
                for line in re.split(r"\r\n|\r|\n", decoded):
                    if line and not line.startswith(":") and line.partition(":")[0] not in {
                        "data", "event", "id", "retry",
                    }:
                        raise ValueError
                response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)
                payloads = [event.data for event in EventSource(response).iter_sse()]
            for payload in payloads:
                if not payload.strip():
                    raise ValueError
                node = EventNode.model_validate_json(payload)
                if node.type not in _EVENT_TYPES:
                    raise ValueError
                if node.type == "init":
                    if self.started or not node.text:
                        raise ValueError
                    self.started = True
                elif not self.started:
                    raise ValueError
                if node.type in {"stdout", "stderr", "status", "ping"} and node.text is None:
                    raise ValueError
                if node.type == "error":
                    if node.error is None:
                        raise ValueError
                    # Some execd versions end failed commands with error only;
                    # the SDK treats a confirmed exit code as authoritative.
                    self.authoritative_error = node.error.exit_code is not None
                    if not self.authoritative_error:
                        try:
                            int(node.error.value)
                            self.authoritative_error = True
                        except (TypeError, ValueError):
                            pass
                if node.type == "execution_complete":
                    if self.completed:
                        raise ValueError
                    self.completed = True
                # Older execd may send trailing stdout after completion. The
                # SDK drains to EOF, so we must validate those frames too.
        except Exception:
            raise SandboxStreamError("Sandbox stream event is invalid; evidence is incomplete") from None

    def finish(self) -> None:
        if not self.started or not (self.completed or self.authoritative_error):
            raise SandboxStreamError("Sandbox stream ended without a confirmed result")


class _ValidatedStream(httpx.AsyncByteStream):
    def __init__(self, source: httpx.AsyncByteStream, *, max_event_bytes: int, max_stream_bytes: int):
        self.source = source
        self.max_event_bytes, self.max_stream_bytes = max_event_bytes, max_stream_bytes

    async def __aiter__(self):
        pending = bytearray()
        validation = _EventValidation()
        received = 0
        try:
            async for chunk in self.source:
                received += len(chunk)
                if received > self.max_stream_bytes:
                    raise SandboxStreamError("Sandbox stream storage limit reached; evidence is incomplete")
                # Bound our own buffering even if a transport returns one very
                # large chunk. The full log sink remains separately bounded.
                step = min(65536, self.max_event_bytes)
                for offset in range(0, len(chunk), step):
                    pending.extend(chunk[offset:offset + step])
                    while match := _FRAME_END.search(pending):
                        if match.end() == len(pending) and pending[-1:] == b"\r":
                            break  # A CRLF may be split across transport chunks.
                        end = match.end()
                        if end > self.max_event_bytes:
                            raise SandboxStreamError("Sandbox event exceeds the size limit; evidence is incomplete")
                        frame = bytes(pending[:end])
                        del pending[:end]
                        validation.frame(frame)
                        yield frame
                    if len(pending) > self.max_event_bytes:
                        raise SandboxStreamError("Sandbox event exceeds the size limit; evidence is incomplete")
            # A final CR is a complete newline at EOF. Anything else left is a
            # truncated event, even if the SDK would silently flush/ignore it.
            if pending:
                match = _FRAME_END.fullmatch(pending) if not pending.strip() else _FRAME_END.search(pending)
                if not match or match.end() != len(pending):
                    raise SandboxStreamError("Sandbox stream has a truncated event; evidence is incomplete")
                validation.frame(bytes(pending))
                yield bytes(pending)
            validation.finish()
        except SandboxStreamError:
            raise
        except Exception:
            raise SandboxStreamError("Sandbox stream could not be read; evidence is incomplete") from None

    async def aclose(self) -> None:
        try:
            await self.source.aclose()
        except Exception:
            raise SandboxStreamError("Sandbox stream could not be closed") from None


def sandbox_response_hook(
    *, max_event_bytes: int = 1024 * 1024, max_stream_bytes: int = 64 * 1024 * 1024,
) -> Callable[[httpx.Response], Awaitable[None]]:
    """Build a per-ConnectionConfig response hook; no global SDK mutation.

    Errors retain HTTP status (notably lifecycle 404) but never body, headers,
    request IDs or raw exceptions. SSE compression is rejected because this
    validator sits before httpx content decoding; request identity encoding.
    """
    if (isinstance(max_event_bytes, bool) or isinstance(max_stream_bytes, bool)
            or not isinstance(max_event_bytes, int) or not isinstance(max_stream_bytes, int)
            or not 1 <= max_event_bytes <= max_stream_bytes):
        raise ValueError("Invalid sandbox stream size limits")

    async def validate(response: httpx.Response) -> None:
        if not 200 <= response.status_code < 300:
            try:
                await response.aclose()
            except Exception:
                pass  # Preserve the status; never expose a close error body.
            raise SandboxApiException(message="Sandbox HTTP request failed", status_code=response.status_code)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        expects_sse = response.request.headers.get("accept", "") == "text/event-stream"
        if content_type != "text/event-stream":
            if expects_sse:
                await _close_response(response)
                raise SandboxStreamError("Sandbox command response is not an event stream")
            return
        if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
            await _close_response(response)
            raise SandboxStreamError("Sandbox stream encoding is unsupported")
        if not isinstance(response.stream, httpx.AsyncByteStream):
            raise SandboxStreamError("Sandbox stream transport is unsupported")
        response.stream = _ValidatedStream(response.stream, max_event_bytes=max_event_bytes,
                                           max_stream_bytes=max_stream_bytes)

    return validate
