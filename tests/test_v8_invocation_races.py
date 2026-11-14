"""Deterministic valid/invalid identity exclusion and database lock races."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from test_m3_host import prepared
from test_postgres import postgres_environment  # noqa: F401
from test_v8_invalid_audit import TestHost, call, error

from omniharness import m4_invalid_audit as audit
from omniharness.api import create_app
from omniharness.m3_agents import invocation_identity
from omniharness.m3_host import AgentOperationNeedsReview
from omniharness.m4_tools import AskUserArguments, CommandArguments, WorkspaceToolGateway
from omniharness.models import Clarification, OperationRecord, Run, RunBudget, ToolOperation
from omniharness.security import create_pairing_code
from omniharness.worker import Worker, WorkerLock


@pytest.mark.asyncio
async def test_valid_invalid_identity_interleavings(environment, monkeypatch):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    monkeypatch.setattr(audit, "_scope", lambda: {"id": "task-one", "write_paths": None})
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = TestHost(worker, run_id)
        gateway = WorkspaceToolGateway.__new__(WorkspaceToolGateway)
        gateway.host = host
        gateway.workspace = {"id": "test-workspace", "policy_version": 1, "tool_policy": {"tools": ["workspace_patch"]}}
        valid = call(run_id, "race-invalid-first", args={"path": "new.txt", "content": "ok"})
        invalid = call(run_id, "race-invalid-first")
        # The old race: guard passed, then the invalid transaction committed.
        await audit.guard_valid_invocation(host, valid, "workspace_patch")
        await audit.audit_invalid(host, invalid, "workspace_patch", error(invalid))
        identity = invocation_identity(valid, kind="tool_calls", role="execute", tool_name="workspace_patch")
        with pytest.raises(AgentOperationNeedsReview):
            await gateway._record(valid, valid.tool_call["args"], identity)
        with worker.transaction() as db:
            with pytest.raises(AgentOperationNeedsReview):
                audit.check_valid_reservation(db, host, valid, "workspace_patch", require_claim=True)
        with factory() as db:
            assert len(list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))) == 1
            assert db.get(RunBudget, run_id).tool_calls == 1
            claims = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id,
                                                                     OperationRecord.node_id == "v8-tool-preparation")))
            assert len(claims) == 1 and claims[0].result == {"kind": "invalid"}
        # A persisted valid preparation excludes the invalid call before the
        # former creates a snapshot, tool request or budget reservation.
        valid2 = call(run_id, "race-valid-first", args={"path": "new.txt", "content": "ok"})
        invalid2 = call(run_id, "race-valid-first")
        with worker.transaction() as db:
            host._run(db, "execute")
            audit.claim_valid_preparation(db, host, valid2, "workspace_patch")
        with pytest.raises(AgentOperationNeedsReview):
            await audit.audit_invalid(host, invalid2, "workspace_patch", error(invalid2))
        with worker.transaction() as db:
            host._run(db, "execute")
            audit.claim_valid_preparation(db, host, valid2, "workspace_patch")  # crash-safe retry
            audit.check_valid_reservation(db, host, valid2, "workspace_patch", require_claim=True)
        changed = call(run_id, "race-valid-first", args={"path": "other.txt", "content": "ok"})
        with worker.transaction() as db:
            host._run(db, "execute")
            with pytest.raises(AgentOperationNeedsReview):
                audit.claim_valid_preparation(db, host, changed, "workspace_patch")
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == 1
            assert len(list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))) == 1


@pytest.mark.asyncio
async def test_key_rotation_after_first_valid_claim_blocks_other_id(environment, monkeypatch):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    monkeypatch.setattr(audit, "_scope", lambda: {"id": "task-one", "write_paths": None})
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = TestHost(worker, run_id)
        valid = call(run_id, "valid-first", args={"path": "new.txt", "content": "ok"})
        with worker.transaction() as db:
            host._run(db, "execute")
            audit.claim_valid_preparation(db, host, valid, "workspace_patch")
        key_path = settings.secret_dir / "master.key"
        original = key_path.read_bytes()
        try:
            key_path.write_bytes(Fernet.generate_key())
            different = call(run_id, "different-id")
            with pytest.raises(AgentOperationNeedsReview):
                await audit.audit_invalid(host, different, "workspace_patch", error(different))
            next_valid = call(run_id, "next-valid", args={"path": "next.txt", "content": "ok"})
            with pytest.raises(AgentOperationNeedsReview):
                await audit.guard_valid_invocation(host, next_valid, "workspace_patch")
        finally:
            key_path.write_bytes(original)
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == 0
            assert not list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))


@pytest.mark.asyncio
async def test_command_preparation_claim_exists_before_snapshot(environment, monkeypatch):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    monkeypatch.setattr(audit, "_scope", lambda: {"id": "task-one", "write_paths": None})
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = TestHost(worker, run_id)
        gateway = WorkspaceToolGateway.__new__(WorkspaceToolGateway)
        gateway.host = host
        gateway.workspace = {"id": "test-workspace", "policy_version": 1, "tool_policy": {"tools": ["run_command"]}}
        gateway.patch_lock = asyncio.Lock()
        entered, release = asyncio.Event(), asyncio.Event()
        async def snapshot(*_args, **_kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("simulated snapshot interruption")
        gateway._broker = snapshot
        valid = call(run_id, "command-race", args={"command": "test"})
        valid.tool_call["name"] = "run_command"
        invalid = call(run_id, "command-race", args={"command": 3})
        invalid.tool_call["name"] = "run_command"
        identity = invocation_identity(valid, kind="tool_calls", role="execute", tool_name="run_command")
        task = asyncio.create_task(gateway._record(valid, {"command": "test"}, identity))
        await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(ValidationError) as validation:
            CommandArguments.model_validate(invalid.tool_call["args"])
        with pytest.raises(AgentOperationNeedsReview):
            await audit.audit_invalid(host, invalid, "run_command", validation.value)
        release.set()
        with pytest.raises(RuntimeError, match="simulated snapshot interruption"):
            await task
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == 0
            assert not list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
            assert len(list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id,
                                                                     OperationRecord.node_id == "v8-tool-preparation")))) == 1


@pytest.mark.asyncio
async def test_ask_user_rechecks_invalid_identity_in_reservation_transaction(environment, monkeypatch):
    from omniharness import m4_collaboration
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    scope = {"id": "task-one", "write_paths": None}
    monkeypatch.setattr(audit, "_scope", lambda: scope)
    monkeypatch.setattr(m4_collaboration, "current_task_scope", lambda: scope)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = TestHost(worker, run_id)
        gateway = WorkspaceToolGateway.__new__(WorkspaceToolGateway)
        gateway.host = host
        valid = call(run_id, "ask-race", args={"question": "Please clarify"})
        valid.tool_call["name"] = "ask_user"
        invalid = call(run_id, "ask-race", args={"question": ""})
        invalid.tool_call["name"] = "ask_user"
        with pytest.raises(ValidationError) as validation:
            AskUserArguments.model_validate(invalid.tool_call["args"])
        original_guard = audit.guard_valid_invocation
        async def interleaved_guard(*args):
            await original_guard(*args)
            await audit.audit_invalid(host, invalid, "ask_user", validation.value)
        monkeypatch.setattr(audit, "guard_valid_invocation", interleaved_guard)
        with pytest.raises(AgentOperationNeedsReview):
            await gateway._ask_user(valid)
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == 1
            assert not list(db.scalars(select(Clarification).where(Clarification.run_id == run_id)))
            assert len(list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))) == 1


@pytest.mark.asyncio
async def test_cross_tool_ask_and_task_cannot_reuse_claimed_identity(environment, monkeypatch):
    from omniharness import m4_collaboration
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker = Worker(settings, engine)
    scope = {"id": "task-one", "write_paths": None}
    monkeypatch.setattr(audit, "_scope", lambda: scope)
    monkeypatch.setattr(m4_collaboration, "current_task_scope", lambda: scope)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = TestHost(worker, run_id)
        gateway = WorkspaceToolGateway.__new__(WorkspaceToolGateway)
        gateway.host = host
        command = call(run_id, "cross-ask", args={"command": "test"})
        command.tool_call["name"] = "run_command"
        with worker.transaction() as db:
            host._run(db, "execute")
            audit.claim_valid_preparation(db, host, command, "run_command")
        question = call(run_id, "cross-ask", args={"question": "clarify"})
        question.tool_call["name"] = "ask_user"
        with pytest.raises(AgentOperationNeedsReview):
            await gateway._ask_user(question)

        # Once a task was reserved, a same-ID ordinary request cannot claim it
        # or start a snapshot. The opposite order is also blocked under _run.
        delegated = call(run_id, "task-first", args={"subagent_type": "execute"})
        delegated.tool_call["name"] = "task"
        task_identity = invocation_identity(delegated, kind="tool_calls", role="execute", tool_name="task")
        assert await host.reserve_invocation("tool_calls", "execute", "task", task_identity)
        ordinary = call(run_id, "task-first", args={"path": "new.txt", "content": "ok"})
        with worker.transaction() as db:
            host._run(db, "execute")
            with pytest.raises(AgentOperationNeedsReview):
                audit.claim_valid_preparation(db, host, ordinary, "workspace_patch")

        ordinary = call(run_id, "tool-first", args={"path": "new.txt", "content": "ok"})
        with worker.transaction() as db:
            host._run(db, "execute")
            audit.claim_valid_preparation(db, host, ordinary, "workspace_patch")
        delegated = call(run_id, "tool-first", args={"subagent_type": "execute"})
        delegated.tool_call["name"] = "task"
        task_identity = invocation_identity(delegated, kind="tool_calls", role="execute", tool_name="task")
        with pytest.raises(AgentOperationNeedsReview):
            await host.reserve_invocation("tool_calls", "execute", "task", task_identity)

        invalid = call(run_id, "invalid-first")
        await audit.audit_invalid(host, invalid, "workspace_patch", error(invalid))
        delegated = call(run_id, "invalid-first", args={"subagent_type": "execute"})
        delegated.tool_call["name"] = "task"
        task_identity = invocation_identity(delegated, kind="tool_calls", role="execute", tool_name="task")
        with pytest.raises(AgentOperationNeedsReview):
            await host.reserve_invocation("tool_calls", "execute", "task", task_identity)
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == 2  # task + invalid only
            assert not list(db.scalars(select(Clarification).where(Clarification.run_id == run_id)))


@pytest.mark.asyncio
async def test_identical_ask_user_reentry_reuses_clarification_and_budget(environment, monkeypatch):
    from omniharness import m4_collaboration, m4_tools
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    with factory.begin() as db:
        db.get(Run, run_id).workflow_version = "stage-agent-v8"
    worker = Worker(settings, engine)
    scope = {"id": "task-one", "write_paths": None}
    monkeypatch.setattr(audit, "_scope", lambda: scope)
    monkeypatch.setattr(m4_collaboration, "current_task_scope", lambda: scope)
    seen = []
    def resume(payload):
        seen.append(payload["clarification_id"])
        return {"clarification_id": payload["clarification_id"], "answer": "answer"}
    monkeypatch.setattr(m4_tools, "interrupt", resume)
    async def wait_operations():
        pass
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        gateway = WorkspaceToolGateway.__new__(WorkspaceToolGateway)
        gateway.host = TestHost(worker, run_id)
        gateway.scheduler = SimpleNamespace(wait_operations=wait_operations)
        question = call(run_id, "same-question", args={"question": "clarify"})
        question.tool_call["name"] = "ask_user"
        first, replay = await gateway._ask_user(question), await gateway._ask_user(question)
        assert first.content == replay.content and len(seen) == 2 and seen[0] == seen[1]
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == 1
            assert len(list(db.scalars(select(Clarification).where(Clarification.run_id == run_id)))) == 1


class TransactionWorker:
    """Two test hosts use independent PG sessions to exercise the Run lock."""
    def __init__(self, settings, factory, *, before_commit=None, release=None, started=None):
        self.settings, self.factory = settings, factory
        self.lock = SimpleNamespace(lost=False)
        self.before_commit, self.release, self.started = before_commit, release, started

    @contextmanager
    def transaction(self):
        with self.factory.begin() as db:
            if self.started:
                self.started.set()
            yield db
            if self.before_commit:
                self.before_commit.set()
                assert self.release.wait(10)


@pytest.mark.parametrize("first", ["valid", "invalid"])
def test_postgres_independent_transactions_exclude_same_identity(postgres_environment, monkeypatch, first):  # noqa: F811 - shared opt-in fixture
    settings, engine, factory, _ = postgres_environment
    monkeypatch.setattr(audit, "_scope", lambda: {"id": "task-one", "write_paths": None})
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        run_id = prepared(settings, engine, factory, client)
        valid = call(run_id, "cross-thread-id", args={"path": "new.txt", "content": "ok"})
        invalid = call(run_id, "cross-thread-id")
        committed_ready, release, second_started = Event(), Event(), Event()
        a = TestHost(TransactionWorker(settings, factory, before_commit=committed_ready, release=release), run_id)
        b = TestHost(TransactionWorker(settings, factory, started=second_started), run_id)

        def valid_call(host):
            with host.worker.transaction() as db:
                host._run(db, "execute")
                audit.claim_valid_preparation(db, host, valid, "workspace_patch")

        def invalid_call(host):
            return asyncio.run(audit.audit_invalid(host, invalid, "workspace_patch", error(invalid)))

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_op, second_op = ((valid_call, invalid_call) if first == "valid" else (invalid_call, valid_call))
            winner = pool.submit(first_op, a)
            assert committed_ready.wait(10)
            loser = pool.submit(second_op, b)
            assert second_started.wait(10)
            with pytest.raises(TimeoutError):
                loser.result(timeout=0.2)  # Its SELECT FOR UPDATE is blocked.
            release.set()
            winner.result(timeout=10)
            with pytest.raises(AgentOperationNeedsReview):
                loser.result(timeout=10)
        with factory() as db:
            assert db.get(RunBudget, run_id).tool_calls == (0 if first == "valid" else 1)
            rows = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
            claims = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id,
                                                                    OperationRecord.node_id == "v8-tool-preparation")))
            assert (len(rows), len(claims)) == ((0, 1) if first == "valid" else (1, 1))
            assert claims[0].result == {"kind": first}
