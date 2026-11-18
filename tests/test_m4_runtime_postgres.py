"""Dedicated PG/fenced graph tests, with local models and sandbox test double."""
import os
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_m4_runtime import StageModel, ToolModel, prepare
from test_postgres import postgres_environment  # noqa: F401

from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema
from omniharness.models import Approval, Run, RunBudget, RunCommand, ToolOperation, WorkspaceLease
from omniharness.security import create_pairing_code
from omniharness.worker import Worker, WorkerLock


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "pause_approve", "requirement", "cancel"])
async def test_worker_approval_survives_owner_restart_without_implicit_execution(
        request, monkeypatch, tmp_path, action):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    settings.enable_agent = settings.enable_tools = True
    settings.sandbox_key_file = settings.secret_dir / "sandbox-test.key"
    settings.sandbox_key_file.write_text("local-test-key-only-" * 3)
    settings.checkpoint_schema = "m4_runtime_" + uuid4().hex
    await initialize_checkpoint_schema(os.environ["OMNI_TEST_POSTGRES_URL"], schema=settings.checkpoint_schema)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)})
        environment = (settings, engine, factory, client)
        worker, run_id, _, broker, sandbox, project = prepare(environment, tmp_path, approval=True)

        def configure(target):
            target.agent_model_factory = lambda role, config: ToolModel() if role == "execute" else StageModel(role)
            target.workspace_broker, target.sandbox_executor = broker, sandbox
            return target

        errors = []
        import omniharness.m3_worker as runtime
        original = runtime._review

        def review(*args, **kwargs):
            import traceback
            errors.append("".join(traceback.format_exception(*sys.exc_info())))
            return original(*args, **kwargs)
        monkeypatch.setattr(runtime, "_review", review)
        with factory.begin() as db:
            db.get(Run, run_id).status = "queued"
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            configure(worker)
            assert await worker.run_once() == run_id
        with factory() as db:
            run = db.get(Run, run_id)
            assert run.status == "awaiting_approval", errors
            assert not db.get(RunBudget, run_id).active_segment_open
            decision = db.scalar(select(Approval).join(ToolOperation).where(ToolOperation.run_id == run_id))
            approval_id, fingerprint = decision.id, decision.fingerprint
        assert sandbox.calls == []
        new_worker = configure(Worker(settings, engine))
        with WorkerLock(engine, settings) as owner:
            new_worker.lock = owner
            assert await new_worker.run_once() is None
            if action == "cancel":
                assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
            elif action == "requirement":
                response = client.post(f"/api/v1/runs/{run_id}/commands", json={
                    "command_type": "append_requirement", "client_request_id": "changed",
                    "content": "停止旧命令，按新要求重新规划。"})
                assert response.status_code in (200, 202), response.text
            else:
                if action == "pause_approve":
                    assert client.post(f"/api/v1/runs/{run_id}/commands", json={
                        "command_type": "pause", "client_request_id": "pause"}).status_code == 202
                    new_worker.consume_commands()
                response = client.post(f"/api/v1/approvals/{approval_id}/decision", json={
                    "decision": "approve", "fingerprint": fingerprint, "client_request_id": "approve"})
                assert response.status_code == 202, response.text
                if action == "pause_approve":
                    assert await new_worker.run_once() is None
                    assert sandbox.calls == []
                    assert client.post(f"/api/v1/runs/{run_id}/commands", json={
                        "command_type": "resume", "client_request_id": "resume"}).status_code == 202
            await new_worker.run_once()
        with factory() as db:
            run = db.get(Run, run_id)
            assert run.status == ("cancelled" if action == "cancel" else "completed"), errors
            assert db.get(WorkspaceLease, run.workspace_id) is None
            if action == "requirement":
                command = db.scalar(select(RunCommand).where(RunCommand.run_id == run_id,
                                                           RunCommand.command_type == "append_requirement"))
                assert command.payload["delivery"] == "prepared_for_model"
        assert broker.calls.count("patch") == 1
        assert sandbox.calls == (["python main.py"] if action in ("approve", "pause_approve") else [])
        assert (project / "main.py").read_text() == "print(42)\n"
