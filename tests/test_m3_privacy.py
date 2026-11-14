"""Synthetic-key canary with an official adapter and a loopback-only server."""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langsmith import tracing_context
from langsmith.utils import tracing_is_enabled
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from test_m3_agent_config import ROLE_VERSIONS, enqueue_and_freeze, mark_tools, model
from test_postgres import postgres_environment  # noqa: F401 - opt-in isolated PG fixture

import omniharness.services as services
from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema, open_fenced_saver, register_fence
from omniharness.m3_agents import AgentExternalCallFailed, AgentRole, build_controlled_agent
from omniharness.m3_host import FencedAgentHost
from omniharness.models import Run, RunEvent
from omniharness.providers import RuntimeModelConfig, build_chat_model
from omniharness.security import create_pairing_code
from omniharness.worker import Worker, WorkerLock

CANARY = "synthetic-test-only-key-canary-please-never-use"


@pytest.fixture
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


@pytest.mark.asyncio
async def test_provider_error_canary_never_reaches_fenced_checkpoint_or_events(request, monkeypatch, caplog):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    url = os.environ["OMNI_TEST_POSTGRES_URL"]
    if make_url(url).password is not None:
        pytest.fail("Use a password-free test URL and PGPASSWORD", pytrace=False)
    checkpoint_schema = f"privacy_{uuid4().hex}"
    await initialize_checkpoint_schema(url, schema=checkpoint_schema)
    calls, tracing_states = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            json.loads(self.rfile.read(size))
            calls.append(self.headers.get("Authorization") == f"Bearer {CANARY}")
            if len(calls) == 1:
                body = {"id": "synthetic-parent", "object": "chat.completion", "created": 1,
                        "model": "fake-model", "choices": [{"index": 0, "finish_reason": "tool_calls",
                            "message": {"role": "assistant", "content": None, "tool_calls": [{
                                "id": "delegate-1", "type": "function", "function": {"name": "task",
                                    "arguments": json.dumps({"description": "test", "subagent_type": "researcher"})},
                            }]}}]}
                self.send_response(200)
            else:
                body = {"error": {"message": "synthetic error " + CANARY, "type": "invalid_request_error"}}
                self.send_response(400)
            data = json.dumps(body).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    monkeypatch.setenv("LANGSMITH_TRACING_V2", "true")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    try:
        with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
            assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
            configured = model(client, "privacy", url=fake_url, key=CANARY)
            mark_tools(factory, configured["id"])
            conversation = client.post("/api/v1/conversations", json={}).json()
            with factory.begin() as db:
                run_id = enqueue_and_freeze(db, conversation["id"], configured["id"], settings)
                db.get(Run, run_id).status = "running"
            worker = Worker(settings, engine)

            class AuditedHost(FencedAgentHost):
                async def reserve_invocation(self, kind, role, tool_name, identity):
                    tracing_states.append(tracing_is_enabled())
                    return await super().reserve_invocation(kind, role, tool_name, identity)

            with WorkerLock(engine, settings) as owner:
                worker.lock = owner
                host = AuditedHost(worker, run_id, "agent-v1", role_versions=ROLE_VERSIONS, plugin_versions={},
                                   role_bindings={"coordinator": "think", "researcher": "plan"})
                ticket = register_fence(owner, run_id, "agent-v1", schema=checkpoint_schema)
                async with open_fenced_saver(url, ticket) as saver:
                    def make_model():
                        return build_chat_model(RuntimeModelConfig(id="synthetic", adapter_type="openai-compatible",
                            model_name="fake-model", api_key=CANARY, base_url=fake_url))

                    agent = build_controlled_agent(
                        coordinator=AgentRole("coordinator", "test", make_model()),
                        subagents=[AgentRole("researcher", "test", make_model())],
                        hooks=host, checkpointer=saver, require_durable_ids=True,
                    )
                    with tracing_context(enabled=False), pytest.raises(AgentExternalCallFailed) as exc:
                        await agent.ainvoke({"messages": [("user", "delegate to researcher")]},
                                            {"configurable": {"thread_id": run_id}}, durability="sync")
                    assert CANARY not in str(exc.value)
            assert len(calls) >= 2 and all(calls)
            assert tracing_states and not any(tracing_states)
            assert CANARY not in client.get(f"/api/v1/runs/{run_id}").text
            assert CANARY not in caplog.text

            # Exercise the actual experimental API -> Worker branch as well;
            # the global gate is patched only inside this isolated test.
            settings.enable_agent = True
            settings.checkpoint_schema = checkpoint_schema
            monkeypatch.setattr(services, "AGENT_RUNTIME_WIRED", True)
            original_reserve = FencedAgentHost.reserve_invocation

            async def record_tracing(self, kind, role, tool_name, identity):
                tracing_states.append(tracing_is_enabled())
                return await original_reserve(self, kind, role, tool_name, identity)

            monkeypatch.setattr(FencedAgentHost, "reserve_invocation", record_tracing)
            second_conversation = client.post("/api/v1/conversations", json={}).json()
            response = client.post(f"/api/v1/conversations/{second_conversation['id']}/messages", json={
                "content": "delegate to researcher", "model_config_id": configured["id"],
                "client_request_id": "worker-canary", "workflow_type": "agent",
            })
            assert response.status_code == 202 and CANARY not in response.text
            product_run = response.json()["run_id"]
            calls.clear()
            product_worker = Worker(settings, engine)
            with WorkerLock(engine, settings) as owner:
                product_worker.lock = owner
                assert await product_worker.run_once() == product_run
            assert len(calls) >= 2 and all(calls)
            assert tracing_states and not any(tracing_states)
            public = client.get(f"/api/v1/runs/{product_run}")
            assert public.status_code == 200 and public.json()["status"] == "pending_review"
            assert CANARY not in public.text and CANARY not in caplog.text
            with factory() as db:
                assert CANARY not in repr([event.payload for event in db.scalars(
                    select(RunEvent).where(RunEvent.run_id.in_([run_id, product_run])))])
            with engine.connect() as connection:
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    for row in connection.execute(text(f"SELECT * FROM {checkpoint_schema}.{table}")):
                        for value in row:
                            if isinstance(value, (bytes, bytearray, memoryview)):
                                assert CANARY.encode() not in bytes(value)
                            else:
                                assert CANARY not in repr(value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
