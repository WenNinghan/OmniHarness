"""v4 uses the existing real PG fencing/restart suite with local-only models."""
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_m4_collaboration import ParallelPlanModel, ScopedToolModel, prepare_v4
from test_postgres import postgres_environment  # noqa: F401

from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema
from omniharness.models import Run, RunBudget, ToolOperation
from omniharness.security import create_pairing_code
from omniharness.worker import WorkerLock


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "pause_approve", "requirement", "cancel"])
async def test_v4_worker_owner_restart_approval_and_cancel(request, monkeypatch, tmp_path, action):
    import test_m4_runtime_postgres as existing

    def prepare_compact(*args, **kwargs):
        prepared = prepare_v4(*args, **kwargs)
        worker, run_id, *_ = prepared
        from omniharness.models import Run
        with worker.sessions.begin() as db:
            run = db.get(Run, run_id)
            run.config_snapshot = {**run.config_snapshot, "agent": {**run.config_snapshot["agent"], "strategy": "compact"}}
        return prepared

    monkeypatch.setattr(existing, "prepare", prepare_compact)
    await existing.test_worker_approval_survives_owner_restart_without_implicit_execution(
        request, monkeypatch, tmp_path, action)


@pytest.mark.asyncio
async def test_v4_parallel_tool_subgraphs_use_distinct_pg_checkpoints(request, tmp_path):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    settings.enable_agent = settings.enable_tools = True
    settings.sandbox_key_file = settings.secret_dir / "sandbox-test.key"
    settings.sandbox_key_file.write_text("local-test-key-only-" * 3)
    settings.checkpoint_schema = "m4_collab_" + uuid4().hex
    await initialize_checkpoint_schema(os.environ["OMNI_TEST_POSTGRES_URL"], schema=settings.checkpoint_schema)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)})
        worker, run_id, _, broker, sandbox, project = prepare_v4((settings, engine, factory, client), tmp_path)
        for name in ("a.py", "b.py"):
            (project / name).write_text("print(0)\n")

        def models(role, config):
            if role == "execute":
                return ScopedToolModel()
            chosen = ParallelPlanModel(role)
            chosen.task_paths = [["a.py"], ["b.py"]]
            return chosen

        worker.agent_model_factory = models
        worker.workspace_broker, worker.sandbox_executor = broker, sandbox
        with factory.begin() as db:
            run = db.get(Run, run_id)
            run.status = "queued"
            run.config_snapshot = {**run.config_snapshot, "agent": {**run.config_snapshot["agent"], "strategy": "compact"}}
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert await worker.run_once() == run_id
        with factory() as db:
            assert db.get(Run, run_id).status == "completed"
            assert db.get(RunBudget, run_id).model_calls == 10
            operations = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
            assert len(operations) == 6 and all(op.status == "completed" for op in operations)
            assert {op.result["host_task_id"] for op in operations} == {"t1", "t2"}
        assert all((project / name).read_text() == "print(42)\n" for name in ("a.py", "b.py"))
