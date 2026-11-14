import asyncio
import json
import socket

import pytest
import uvicorn
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from omniharness.mcp_tools import MCPBoundaryError, MCPConfiguration, MCPRegistry, MCPResultUnknown
from omniharness.settings import Settings


@pytest.fixture
async def mcp_server(tmp_path):
    calls = []
    server = MCPServer("OmniHarness local test", version="1", log_level="ERROR")

    @server.tool()
    def echo(message: str) -> str:
        calls.append(message)
        return message

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def record(value: str) -> str:
        calls.append(value)
        return "recorded"

    @server.tool()
    def lost_response(value: str) -> str:
        calls.append(value)
        return "drop-this-response"

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.listen()
    app = server.streamable_http_app(json_response=True, stateless_http=True)
    async def corrupt_result(scope, receive, send):
        async def wrapped(message):
            if message['type'] == 'http.response.start':
                message = {**message, 'headers': [(k, v) for k, v in message['headers'] if k != b'content-length']}
            if message['type'] == 'http.response.body' and b'drop-this-response' in message.get('body', b''):
                message = {**message, 'body': b'{invalid-json'}
            await send(message)
        await app(scope, receive, wrapped)
    uv = uvicorn.Server(uvicorn.Config(corrupt_result, log_level='error', lifespan='on'))
    task = asyncio.create_task(uv.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not uv.started:
                if task.done():
                    task.result()
                await asyncio.sleep(.02)
        tools = await server.list_tools()
        config = MCPConfiguration.model_validate({"servers": [{
            "id": "local", "url": f"http://127.0.0.1:{port}/mcp", "tools": [
                {"name": item.name, "description": "local fixture", "input_schema": item.input_schema,
                 **({"side_effect": "read"} if item.name == "echo" else {})} for item in tools
            ],
        }]})
        filename = tmp_path / 'mcp.json'
        filename.write_text(config.model_dump_json(), encoding='utf-8')
        settings = Settings(_env_file=None, mcp_config_file=filename, secret_dir=tmp_path / 'secrets')
        yield settings, config.model_dump(), calls
    finally:
        uv.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


async def test_real_streamable_http_returns_unicode_and_calls_once(mcp_server):
    settings, config, calls = mcp_server
    result = await MCPRegistry(settings, config).execute('local', 'echo', {'message': '一行\n\n第二行'})
    assert result['response']['isError'] is False
    assert calls == ['一行\n\n第二行']
    assert '第二行' in json.dumps(result, ensure_ascii=False)


async def test_local_policy_overrides_remote_readonly_annotation(mcp_server):
    settings, config, calls = mcp_server
    _, tool = MCPRegistry(settings, config).validate('local', 'record', {'value': 'test'})
    assert tool.side_effect == 'external'
    assert calls == []


async def test_unregistered_invalid_and_changed_config_never_dispatch(mcp_server):
    settings, config, calls = mcp_server
    registry = MCPRegistry(settings, config)
    for server, tool, arguments in [('other', 'echo', {'message': 'no'}),
                                    ('local', 'unregistered', {}), ('local', 'echo', {'message': 123})]:
        with pytest.raises(MCPBoundaryError):
            await registry.execute(server, tool, arguments)
    changed = json.loads(json.dumps(config))
    changed['version'] += 1
    settings.mcp_config_file.write_text(json.dumps(changed))
    with pytest.raises(MCPBoundaryError, match='changed'):
        await registry.execute('local', 'echo', {'message': 'no'})
    assert calls == []


async def test_remote_schema_mismatch_stops_before_tool_dispatch(mcp_server):
    settings, config, calls = mcp_server
    config['servers'][0]['tools'][0]['input_schema']['description'] = 'different contract'
    settings.mcp_config_file.write_text(json.dumps(config))
    with pytest.raises(MCPBoundaryError, match='before tool dispatch'):
        await MCPRegistry(settings, config).execute('local', 'echo', {'message': 'no'})
    assert calls == []


async def test_dispatched_result_loss_is_unknown_and_never_retried(mcp_server):
    settings, config, calls = mcp_server
    with pytest.raises(MCPResultUnknown, match='not retried'):
        await MCPRegistry(settings, config).execute('local', 'lost_response', {'value': 'once'})
    assert calls == ['once']


def test_command_and_mcp_argument_contracts_are_independent():
    from pydantic import ValidationError

    from omniharness.m4_tools import CommandArguments, MCPArguments

    with pytest.raises(ValidationError):
        CommandArguments(command='python -V', cwd='../escape')
    assert MCPArguments(server_id='local', tool_name='echo', arguments={'message': 'ok'}).arguments == {'message': 'ok'}
