"""Exercise the pinned SDK parser via real httpx clients and MockTransport."""
import json

import httpx
import pytest
from opensandbox.adapters.command_adapter import CommandsAdapter
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxApiException, SandboxException
from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts
from opensandbox.models.sandboxes import SandboxEndpoint
from opensandbox.transport import RetryPolicy

from omniharness.sandbox_stream import sandbox_response_hook

PRIVATE = "DO-NOT-LOG-private-key-and-storage-path"


def event(kind, *, legacy=False, newline=b"\n", **values):
    payload = json.dumps({"type": kind, "timestamp": 1, **values}, ensure_ascii=False).encode()
    return (b"" if legacy else b"data: ") + payload + newline * 2


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed, self.read_count = chunks, False, 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


async def sdk_run(chunks, *, status=200, content_type="text/event-stream", encoding=None,
                  output=None, starts=None, **limits):
    source = Chunks(chunks)
    seen = []

    async def transport(request):
        seen.append(request)
        headers = {"content-type": content_type, "x-request-id": PRIVATE}
        if encoding:
            headers["content-encoding"] = encoding
        return httpx.Response(status, headers=headers, stream=source)

    config = ConnectionConfig(domain="sandbox.test", protocol="http", api_key=PRIVATE,
                              use_server_proxy=True, transport=httpx.MockTransport(transport),
                              retry_policy=RetryPolicy.disabled(), disable_metrics=True,
                              event_hooks={"response": [sandbox_response_hook(**limits)]})
    adapter = CommandsAdapter(config, SandboxEndpoint(endpoint="sandbox.test"))

    async def on_stdout(message):
        if output is not None:
            output.append(message.text)

    async def on_init(message):
        if starts is not None:
            starts.append(message.id)

    try:
        result = await adapter.run(["python", "-V"], opts=RunCommandOpts(background=False),
                                   handlers=ExecutionHandlers(on_stdout=on_stdout, on_init=on_init,
                                                              skip_accumulation=True))
        return result, source, seen
    finally:
        await adapter._sse_client.aclose()
        await adapter._httpx_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy,newline", [(False, b"\n"), (False, b"\r\n"), (False, b"\r"),
                                            (True, b"\n")])
async def test_real_sdk_preserves_utf8_blank_lines_and_trailing_output(legacy, newline):
    data = b"".join([
        event("init", text="cmd-1", legacy=legacy, newline=newline),
        event("stdout", text="中文\n\n", legacy=legacy, newline=newline),
        event("execution_complete", legacy=legacy, newline=newline),
        event("stdout", text="tail\n", legacy=legacy, newline=newline),
    ])
    logs, starts = [], []
    # One-byte chunks also split UTF-8 code points and CRLF pairs.
    result, source, seen = await sdk_run([data[i:i + 1] for i in range(len(data))],
                                         output=logs, starts=starts)
    assert result.id == "cmd-1" and result.exit_code == 0
    assert starts == ["cmd-1"] and "".join(logs) == "中文\n\ntail\n"
    assert len(seen) == 1 and source.closed


@pytest.mark.asyncio
async def test_standard_multiline_data_and_comment_heartbeats():
    init = b': keepalive\n\ndata: {"type": "init",\ndata: "timestamp": 1, "text": "cmd-1"}\n\n'
    result, _, _ = await sdk_run([init, event("execution_complete")])
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_execd_captured_echo_stream_includes_ping_and_preserves_exact_bytes():
    # Safe printf fixture captured from the actual M4-A execd, no credentials.
    body = (
        b'{"type":"init","text":"881f49305c5141be8bc5a9d1a355941a","timestamp":1794151808475}\n\n'
        b'{"type":"ping","text":"pong","timestamp":1794151808476}\n\n'
        b'{"type":"stdout","text":"m4-stream","timestamp":1794151808478}\n\n'
        b'{"type":"execution_complete","exit_code":0,"execution_time":3,"timestamp":1794151808478}\n\n'
    )
    logs = []
    result, _, _ = await sdk_run([body[i:i + 7] for i in range(0, len(body), 7)], output=logs)
    assert result.id == "881f49305c5141be8bc5a9d1a355941a" and result.exit_code == 0
    assert logs == ["m4-stream"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_known_informational_events_never_stand_in_for_completion(legacy):
    events = [event("init", text="cmd-1", legacy=legacy),
              event("ping", text="pong", legacy=legacy),
              event("status", text="busy", legacy=legacy)]
    result, _, _ = await sdk_run([*events, event("execution_complete", legacy=legacy)])
    assert result.exit_code == 0
    with pytest.raises(SandboxException, match="without a confirmed result"):
        await sdk_run(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("damaged", [
    b"data: " + PRIVATE.encode() + b"\n\n",
    b"data:\n\n",
    b'event: empty\n\n',
    b'ignored: ' + PRIVATE.encode() + b'\n\n',
    event("unknown", text=PRIVATE),
    event("ping"),
    event("status"),
    event("stdout", text=PRIVATE).replace(b'"timestamp": 1', b'"timestamp": "bad"'),
    b'data: {"type":"stdout","timestamp":1,"text":"\xff"}\n\n',
    event("init", text=PRIVATE),  # Duplicate init cannot change operation identity.
])
async def test_malformed_events_cannot_be_skipped_even_with_later_success(damaged, caplog):
    logs = []
    with pytest.raises(SandboxException) as error:
        await sdk_run([event("init", text="cmd-1"), damaged, event("execution_complete")], output=logs)
    assert PRIVATE not in str(error.value) and PRIVATE not in caplog.text
    assert not logs


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [b"", b'data: {"type":"execution_complete","timestamp":1}',
                                      b'data: {"type":"execution_complete","timestamp":1}\n'])
async def test_truncated_or_missing_terminal_event_is_not_success(ending, caplog):
    with pytest.raises(SandboxException):
        await sdk_run([event("init", text="cmd-1"), ending])
    assert PRIVATE not in caplog.text


@pytest.mark.asyncio
async def test_corrupt_trailer_after_complete_is_not_success(caplog):
    with pytest.raises(SandboxException):
        await sdk_run([event("init", text="cmd-1"), event("execution_complete"),
                       b"data: " + PRIVATE.encode()])
    assert PRIVATE not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 404, 429, 500])
async def test_http_errors_keep_status_but_never_read_or_log_body(status, caplog):
    source = Chunks([PRIVATE.encode()])

    async def handle(request):
        return httpx.Response(status, headers={"x-request-id": PRIVATE}, stream=source)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                 event_hooks={"response": [sandbox_response_hook()]}) as client:
        with pytest.raises(SandboxApiException) as error:
            await client.get("http://sandbox.test/sandboxes/deleted")
    assert error.value.status_code == status
    assert error.value.response_body is None and error.value.request_id is None
    assert source.read_count == 0 and source.closed
    assert PRIVATE not in caplog.text and PRIVATE not in str(error.value)


@pytest.mark.asyncio
async def test_real_sdk_http_error_cannot_echo_sensitive_body(caplog):
    with pytest.raises(SandboxApiException) as error:
        await sdk_run([PRIVATE.encode()], status=403)
    assert error.value.status_code == 403
    assert error.value.response_body is None and PRIVATE not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type,encoding", [("application/json", None),
                                                   ("text/event-stream", "gzip")])
async def test_wrong_response_format_fails_before_sdk_decode(content_type, encoding, caplog):
    with pytest.raises(SandboxException):
        await sdk_run([PRIVATE.encode()], content_type=content_type, encoding=encoding)
    assert PRIVATE not in caplog.text


@pytest.mark.asyncio
async def test_event_size_limit_precedes_log_callback():
    logs = []
    with pytest.raises(SandboxException, match="size limit"):
        await sdk_run([event("init", text="cmd-1"), event("stdout", text="x" * 500)],
                      output=logs, max_event_bytes=128, max_stream_bytes=1000)
    assert not logs


@pytest.mark.asyncio
async def test_total_wire_limit_bounds_many_small_events():
    with pytest.raises(SandboxException, match="storage limit"):
        await sdk_run([event("init", text="cmd-1"), *[event("stdout", text="x") for _ in range(10)],
                       event("execution_complete")], max_event_bytes=128, max_stream_bytes=256)


@pytest.mark.asyncio
async def test_known_failure_exit_is_preserved():
    result, _, _ = await sdk_run([event("init", text="cmd-1"),
                                 event("error", error={"ename": "exit", "evalue": "7", "exit_code": 7})])
    assert result.exit_code == 7


@pytest.mark.asyncio
async def test_validated_response_bytes_are_unchanged():
    payload = b"\xef\xbb\xbf" + event("init", text="cmd-1") + event("execution_complete")

    async def handle(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks([payload]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                 event_hooks={"response": [sandbox_response_hook()]}) as client:
        assert (await client.get("http://sandbox.test/command")).content == payload


@pytest.mark.asyncio
async def test_transport_close_failure_is_sanitized(caplog):
    class BrokenClose(Chunks):
        async def aclose(self):
            raise OSError(PRIVATE)

    async def handle(request):
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=BrokenClose([]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                 event_hooks={"response": [sandbox_response_hook()]}) as client:
        with pytest.raises(SandboxException) as error:
            await client.get("http://sandbox.test/command", headers={"accept": "text/event-stream"})
    assert PRIVATE not in str(error.value) and PRIVATE not in caplog.text


def test_invalid_stream_limits_are_rejected():
    for values in ({"max_event_bytes": 0}, {"max_stream_bytes": True},
                   {"max_event_bytes": 100, "max_stream_bytes": 10}):
        with pytest.raises(ValueError):
            sandbox_response_hook(**values)
