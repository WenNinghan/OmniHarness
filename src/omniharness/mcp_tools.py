"""Explicit trusted MCP registrations over the official Streamable HTTP SDK."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaValidationError
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .services import parameters_digest
from .workspace_client import read_service_key


class MCPBoundaryError(RuntimeError):
    pass


class MCPResultUnknown(MCPBoundaryError):
    pass


class MCPToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    description: str = Field(max_length=1000)
    # Local policy wins; remote annotations never grant permissions.
    side_effect: str = Field(default="external", pattern=r"^(read|write|external)$")
    input_schema: dict

    @field_validator("input_schema")
    @classmethod
    def schema(cls, value):
        if len(json.dumps(value)) > 32768 or value.get("type") != "object":
            raise ValueError("MCP schema must be a bounded object schema")
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for reference in ("$ref", "$dynamicRef"):
                    if reference in item and not str(item[reference]).startswith("#"):
                        raise ValueError("Remote schema references are not allowed")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        Draft202012Validator.check_schema(value)
        return value


class MCPServerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    url: str
    enabled: bool = True
    credential_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+\.key$")
    tools: list[MCPToolPolicy] = Field(min_length=1, max_length=16)

    @field_validator("url")
    @classmethod
    def endpoint(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Use an explicit MCP HTTP endpoint without URL credentials")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "host.docker.internal"}:
            raise ValueError("Remote MCP endpoints require HTTPS")
        return value


class MCPConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1, le=1)
    version: int = Field(default=1, ge=1)
    servers: list[MCPServerPolicy] = Field(default_factory=list, max_length=8)


def load_mcp_configuration(settings) -> dict:
    if settings.mcp_config_file is None:
        return MCPConfiguration().model_dump()
    try:
        data = Path(settings.mcp_config_file).read_bytes()
        if len(data) > 1024 * 1024:
            raise ValueError
        config = MCPConfiguration.model_validate_json(data)
        if len({server.id for server in config.servers}) != len(config.servers):
            raise ValueError
        if any(len({tool.name for tool in server.tools}) != len(server.tools) for server in config.servers):
            raise ValueError
        return config.model_dump()
    except Exception:
        raise MCPBoundaryError("MCP configuration is invalid; no tool was dispatched") from None


class MCPRegistry:
    def __init__(self, settings, frozen: dict):
        self.settings = settings
        self.frozen = MCPConfiguration.model_validate(frozen)

    def validate(self, server_id: str, tool_name: str, arguments: dict):
        current = load_mcp_configuration(self.settings)
        if parameters_digest(current) != parameters_digest(self.frozen.model_dump()):
            raise MCPBoundaryError("MCP configuration changed; create a new Run")
        server = next((item for item in self.frozen.servers if item.id == server_id and item.enabled), None)
        tool = next((item for item in server.tools if item.name == tool_name), None) if server else None
        if server is None or tool is None:
            raise MCPBoundaryError("This MCP server or tool is not explicitly authorized")
        if not isinstance(arguments, dict) or len(json.dumps(arguments, ensure_ascii=False)) > 32768:
            raise MCPBoundaryError("MCP arguments exceed the configured limit")
        try:
            Draft202012Validator(tool.input_schema).validate(arguments)
        except SchemaValidationError:
            raise MCPBoundaryError("MCP arguments do not match the locally approved schema") from None
        return server, tool

    async def execute(self, server_id: str, tool_name: str, arguments: dict):
        server, tool = self.validate(server_id, tool_name, arguments)
        headers = {}
        if server.credential_ref:
            headers["Authorization"] = "Bearer " + read_service_key(self.settings.secret_dir / server.credential_ref)
        dispatched = False

        async def request_boundary(request):
            nonlocal dispatched
            target, expected = urlsplit(str(request.url)), urlsplit(server.url)
            if (target.scheme, target.netloc, target.path) != (expected.scheme, expected.netloc, expected.path):
                raise MCPBoundaryError("MCP request target changed")
            if request.method == "POST":
                try:
                    value = json.loads(await request.aread())
                except (ValueError, UnicodeError):
                    raise MCPBoundaryError("MCP request is invalid") from None
                if value.get("method") == "tools/call":
                    if dispatched:
                        raise MCPResultUnknown("MCP tool calls cannot be retried automatically")
                    dispatched = True

        try:
            async with asyncio.timeout(60):
                async with httpx2.AsyncClient(headers=headers, timeout=30, trust_env=False,
                        follow_redirects=False, event_hooks={"request": [request_boundary]}) as http:
                    transport = streamable_http_client(server.url, http_client=http)
                    async with Client(transport, input_required_max_rounds=1, cache=None) as client:
                        catalog = await client.list_tools()
                        remote = next((entry for entry in catalog.tools if entry.name == tool_name), None)
                        if remote is None or parameters_digest(remote.input_schema) != parameters_digest(tool.input_schema):
                            raise MCPBoundaryError("Remote MCP tool schema changed; registration must be reviewed")
                        result = await client.call_tool(tool_name, arguments, read_timeout_seconds=30)
                        value = result.model_dump(mode="json", exclude_none=True, by_alias=True)
                        if len(json.dumps(value, ensure_ascii=False).encode()) > 1024 * 1024:
                            raise MCPResultUnknown("MCP result exceeds the evidence storage limit")
                        return {"server_id": server_id, "tool_name": tool_name,
                                "response": value, **({"error": "MCP_TOOL_FAILED"} if result.is_error else {})}
        except asyncio.CancelledError:
            raise
        except Exception:
            if dispatched:
                raise MCPResultUnknown("MCP operation result is uncertain; it was not retried") from None
            raise MCPBoundaryError("MCP connection or schema check failed before tool dispatch") from None
