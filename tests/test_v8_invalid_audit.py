"""V8 invalid calls are one fenced transaction with durable stop decisions."""
import asyncio
import json
from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from test_m3_host import prepared
from test_postgres import postgres_environment  # noqa: F401

from omniharness import m4_invalid_audit as audit
from omniharness.api import create_app
from omniharness.m3_agents import AgentBoundaryError, InvocationIdentity
from omniharness.m3_host import AgentOperationNeedsReview, FencedAgentHost
from omniharness.m4_tools import PatchArgumentsV8, WorkspaceToolGateway
from omniharness.models import OperationRecord, Run, RunBudget, RunBudgetReservation, RunEvent, ToolOperation
from omniharness.security import create_pairing_code
from omniharness.services import finish_operation, operation_id
from omniharness.worker import Worker, WorkerLock


class TestHost(FencedAgentHost):
    __test__ = False
    workflow_version = "stage-agent-v8"
    plugin_versions = {"builtin-invalid-tool-audit": "1"}

    def __init__(self, worker, run_id):
        super().__init__(worker, run_id, "stage-agent-v8", role_versions={},
                         plugin_versions=self.plugin_versions, role_bindings={"execute": "execute"})

    async def check_control(self, *_args):
        pass

    def _verify_identity(self, identity):
        if identity.thread_id != self.run_id:
            raise AgentBoundaryError("Wrong thread")

    def _run(self, db, *_args, **_kwargs):
        return db.scalar(select(Run).where(Run.id == self.run_id).with_for_update()), {"model": {"id": "test"}}


def call(run_id, call_id, *, args=None, task="graph-task-canary"):
    return SimpleNamespace(tool_call={"id": call_id, "name": "workspace_patch", "args": args or {
        "path": "private-path-canary", "content": "private-content-canary", "expected_sha256": "bad"}},
        runtime=SimpleNamespace(execution_info=SimpleNamespace(
            thread_id=run_id, task_id=task, checkpoint_ns="private-namespace-canary")))


def error(request):
    try:
        PatchArgumentsV8.model_validate(request.tool_call["args"])
    except ValidationError as exc:
        return exc
    raise AssertionError("invalid fixture unexpectedly accepted")


@pytest.mark.asyncio
async def test_atomic_audit_replay_limits_privacy_and_key_rotation(environment, monkeypatch):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    monkeypatch.setattr(audit, "_scope", lambda: {"id": "task-secret-canary", "write_paths": None})
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = TestHost(worker, run_id)
        first = call(run_id, "private-call-canary-1")
        saved = await audit.audit_invalid(host, first, "workspace_patch", error(first))
        assert await audit.audit_invalid(host, first, "workspace_patch", error(first)) == saved
        changed = call(run_id, "private-call-canary-1", args={"path": "other", "content": "changed", "expected_sha256": "bad"})
        with pytest.raises(AgentOperationNeedsReview):
            await audit.audit_invalid(host, changed, "workspace_patch", error(changed))
        valid = call(run_id, "private-call-canary-1", args={"path": "a", "content": "b"})
        with pytest.raises(AgentOperationNeedsReview):
            await audit.guard_valid_invocation(host, valid, "workspace_patch")
        second = call(run_id, "private-call-canary-2")
        third = call(run_id, "private-call-canary-3")
        await audit.audit_invalid(host, second, "workspace_patch", error(second))
        for _ in range(2):
            with pytest.raises(audit.InvalidToolLimitReached):
                await audit.audit_invalid(host, third, "workspace_patch", error(third))
        fourth = call(run_id, "private-call-canary-4")
        with pytest.raises(audit.InvalidToolLimitReached):
            await audit.audit_invalid(host, fourth, "workspace_patch", error(fourth))
        with factory() as db:
            rows = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "tool.failed")))
            ledger = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id)))
            reservations = list(db.scalars(select(RunBudgetReservation).where(RunBudgetReservation.run_id == run_id)))
            assert db.get(RunBudget, run_id).tool_calls == 3
            assert len(rows) == len(events) == len(reservations) == 3
            assert all(r.status == "failed" and not r.slot_claimed for r in rows)
            rendered = json.dumps([[r.task_id, r.tool_call_id, r.parameters, r.result, r.binding_fingerprint] for r in rows]
                                  + [e.payload for e in events] + [x.result for x in ledger])
            for canary in ("private-path-canary", "private-content-canary", "private-call-canary",
                           "task-secret-canary", "graph-task-canary", "private-namespace-canary"):
                assert canary not in rendered
        original = (settings.secret_dir / "master.key").read_bytes()
        try:
            (settings.secret_dir / "master.key").write_bytes(Fernet.generate_key())
            with pytest.raises(AgentOperationNeedsReview):
                await audit.audit_invalid(host, first, "workspace_patch", error(first))
        finally:
            (settings.secret_dir / "master.key").write_bytes(original)


@pytest.mark.asyncio
async def test_failure_rolls_back_everything_and_global_sixth_stops(environment, monkeypatch):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    scope = {"id": "task-one", "write_paths": None}
    monkeypatch.setattr(audit, "_scope", lambda: scope)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = TestHost(worker, run_id)
        first = call(run_id, "call-1")
        original = audit.finish_tool_request
        def crash(*_args, **_kwargs):
            raise RuntimeError("injected")
        monkeypatch.setattr(audit, "finish_tool_request", crash)
        with pytest.raises(RuntimeError):
            await audit.audit_invalid(host, first, "workspace_patch", error(first))
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == 0
            assert not list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
            assert not list(db.scalars(select(RunBudgetReservation).where(RunBudgetReservation.run_id == run_id)))
            assert not list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id)))
        monkeypatch.setattr(audit, "finish_tool_request", original)
        # Two per task keep the per-task threshold below three until the sixth.
        for i in range(6):
            scope["id"] = f"task-{i // 2}"
            request = call(run_id, f"call-{i}")
            if i == 5:
                with pytest.raises(audit.InvalidToolLimitReached):
                    await audit.audit_invalid(host, request, "workspace_patch", error(request))
                with pytest.raises(audit.InvalidToolLimitReached):
                    await audit.audit_invalid(host, request, "workspace_patch", error(request))
            else:
                await audit.audit_invalid(host, request, "workspace_patch", error(request))
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == 6
            assert len(list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))) == 6


@pytest.mark.asyncio
async def test_committed_task_limit_blocks_new_fenced_reservations_but_allows_replay(environment, monkeypatch):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    monkeypatch.setattr(audit, "_scope", lambda: {"id": "task-one", "write_paths": None})
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = TestHost(worker, run_id)
        prior = InvocationIdentity(thread_id=run_id, invocation_id="a" * 64, binding_fingerprint="b" * 64)
        assert await host.reserve_invocation("tool_calls", "execute", "workspace_read", prior)
        with worker.transaction() as db:
            reservation = db.get(RunBudgetReservation, {"run_id": run_id, "invocation_id": prior.invocation_id})
            record_id = operation_id(run_id, "agent:execute:tool_calls", {"invocation_id": prior.invocation_id})
            finish_operation(db, record_id, "completed", result={"binding_fingerprint": reservation.binding_fingerprint,
                                                                  "response": {"content": "saved"}})
        for n in range(3):
            request = call(run_id, f"limit-call-{n}")
            if n == 2:
                with pytest.raises(audit.InvalidToolLimitReached):
                    await audit.audit_invalid(host, request, "workspace_patch", error(request))
            else:
                await audit.audit_invalid(host, request, "workspace_patch", error(request))
        resumed = TestHost(worker, run_id)
        assert await resumed.replay_invocation("tool_calls", "execute", "workspace_read", prior) == {"content": "saved"}
        for kind, name, digest in (("model_calls", None, "c"), ("tool_calls", "workspace_list", "d")):
            invocation = InvocationIdentity(thread_id=run_id, invocation_id=digest * 64, binding_fingerprint="e" * 64)
            with pytest.raises(audit.InvalidToolLimitReached):
                await resumed.reserve_invocation(kind, "execute", name, invocation)
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == 4
            assert db.get(RunBudget, run_id).model_calls == 0


@pytest.mark.asyncio
async def test_gateway_audits_ask_user_and_rejects_ungranted_names(environment, monkeypatch):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    monkeypatch.setattr(audit, "_scope", lambda: {"id": "task-one", "write_paths": None})
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        gateway = WorkspaceToolGateway.__new__(WorkspaceToolGateway)
        gateway.host = TestHost(worker, run_id)
        gateway.workspace = {"tool_policy": {"tools": ["workspace_patch"]}}
        question = call(run_id, "ask-sensitive-id", args={"question": "", "secret-canary": "hidden"})
        question.tool_call["name"] = "ask_user"
        response = await gateway.invoke(question)
        assert "INVALID_ARGUMENTS" in response.content and "secret-canary" not in response.content
        denied = call(run_id, "denied", args={"secret-canary": "hidden"})
        denied.tool_call["name"] = "unknown-sensitive-tool"
        with pytest.raises(AgentBoundaryError):
            await gateway.invoke(denied)
        with factory() as db:
            rows = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
            assert len(rows) == 1 and rows[0].tool_name == "ask_user"
            assert "secret-canary" not in json.dumps(rows[0].parameters)
            assert db.get(RunBudget, run_id).tool_calls == 1


@pytest.mark.asyncio
async def test_invalid_call_api_and_event_stream_expose_only_safe_metadata(environment, monkeypatch, caplog):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    monkeypatch.setattr(audit, "_scope", lambda: {"id": "task-secret-canary", "write_paths": None})
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        request = call(run_id, "private-call-canary-api")
        await audit.audit_invalid(TestHost(worker, run_id), request, "workspace_patch", error(request))
        with factory.begin() as db:
            db.get(Run, run_id).status = "failed"
        tools = client.get(f"/api/v1/runs/{run_id}/tools")
        events = client.get(f"/api/v1/runs/{run_id}/events")
        assert tools.status_code == events.status_code == 200
        assert "INVALID_ARGUMENTS" in tools.text and "tool.failed" in events.text
        output = tools.text + events.text + caplog.text
        for canary in ("private-path-canary", "private-content-canary", "private-call-canary",
                       "task-secret-canary", "graph-task-canary", "private-namespace-canary"):
            assert canary not in output


@pytest.mark.asyncio
async def test_postgres_fenced_parallel_audit_and_crash_rollback(postgres_environment, monkeypatch):  # noqa: F811 - shared opt-in fixture
    settings, engine, factory, _ = postgres_environment
    current_scope = ContextVar("test_audit_scope")
    monkeypatch.setattr(audit, "_scope", lambda: {"id": current_scope.get(), "write_paths": None})
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        run_id = prepared(settings, engine, factory, client)
        worker = Worker(settings, engine)
        with WorkerLock(engine, settings) as lock:
            worker.lock = lock
            host = TestHost(worker, run_id)
            async def send(n, task):
                token = current_scope.set(task)
                try:
                    request = call(run_id, f"postgres-call-{n}")
                    return await audit.audit_invalid(host, request, "workspace_patch", error(request))
                finally:
                    current_scope.reset(token)
            # Same ID is delivered concurrently and can charge only once.
            results = await asyncio.gather(*[send(0, "task-0") for _ in range(5)])
            assert all(item == results[0] for item in results)
            original = audit.finish_tool_request
            def crash(*_args, **_kwargs):
                raise RuntimeError("injected before commit")
            monkeypatch.setattr(audit, "finish_tool_request", crash)
            with pytest.raises(RuntimeError):
                await send(1, "task-1")
            monkeypatch.setattr(audit, "finish_tool_request", original)
            with factory() as db:
                assert db.get(RunBudget, run_id).tool_calls == 1
                assert len(list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))) == 1
            # Distinct identities and tasks execute concurrently; the sixth
            # committed result is durable and blocks its own replay.
            outcomes = await asyncio.gather(*(send(i, f"task-{i // 2}") for i in range(1, 6)),
                                            return_exceptions=True)
            assert sum(isinstance(x, audit.InvalidToolLimitReached) for x in outcomes) == 1
            assert all(isinstance(x, (dict, audit.InvalidToolLimitReached)) for x in outcomes)
            with pytest.raises(audit.InvalidToolLimitReached):
                await send(5, "task-2")
            with factory() as db:
                assert db.get(RunBudget, run_id).tool_calls == 6
                assert len(list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))) == 6
                assert len(list(db.scalars(select(RunBudgetReservation).where(RunBudgetReservation.run_id == run_id)))) == 6
                assert len(list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id,
                                     RunEvent.event_type == "tool.failed")))) == 6
