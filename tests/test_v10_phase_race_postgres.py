"""Synthetic phase race tests using a disposable real PostgreSQL owner and row locks.

Closure, consumed identity, model response, sealing and graph proof are doubles.
These tests do not establish production correction eligibility or dispatch tools.
"""
import asyncio
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_m4_collaboration import prepare_v4
from test_postgres import postgres_environment  # noqa: F401

from omniharness import (
    correction_model,
    correction_phase,
    correction_reservation,
    m4_services,
    v10_closure,
    v10_drain,
    v10_sealed_saver,
)
from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema, open_fenced_saver, register_fence
from omniharness.m3_worker import _checkpoint_url
from omniharness.m4_contract import M4_V10_PLUGIN_VERSIONS
from omniharness.models import OperationRecord, Run, Workspace, WorkspacePolicy
from omniharness.security import create_pairing_code
from omniharness.services import begin_operation, operation_id
from omniharness.v10_owner_runtime import V10OwnerListener, _ConsumedCorrectionReceipt
from omniharness.worker import WorkerLock


@pytest.fixture(scope="module")
def event_loop_policy():
    return asyncio.WindowsSelectorEventLoopPolicy() if sys.platform == "win32" else asyncio.get_event_loop_policy()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["revoke", "taint", "budget", "repeat"])
async def test_phase_commit_races_are_fail_closed(postgres_environment, tmp_path, monkeypatch, case):  # noqa: F811
    settings, engine, factory, _ = postgres_environment
    settings.enable_tools = settings.v10_preview = True
    settings.checkpoint_schema = "phase_race_" + uuid4().hex[:12]
    monkeypatch.setattr(m4_services, "_require_preview_fixture", lambda *_: None)
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=settings.checkpoint_schema)
    with TestClient(create_app(settings, engine), base_url="http://localhost:8788",
                    headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, workspace, _, _, _ = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            snapshot = dict(run.config_snapshot)
            snapshot["agent"] = {**snapshot["agent"], "plugin_versions": M4_V10_PLUGIN_VERSIONS}
            ws = dict(snapshot["workspace"])
            ws["probe_identity"] = ws["grant"]["expected_identity"]
            ws["sandbox"] = {"endpoint": settings.sandbox_endpoint, "image": settings.sandbox_image,
                "instance_id": settings.instance_id, "snapshot_root": settings.sandbox_snapshot_root,
                "max_command_seconds": settings.sandbox_max_command_seconds}
            snapshot["workspace"] = ws
            run.config_snapshot, run.workflow_version = snapshot, "stage-agent-v10"
            row = db.get(Workspace, run.workspace_id)
            row.probe = {"identity": ws["probe_identity"]}
            db.get(WorkspacePolicy, (run.workspace_id, ws["policy_version"])).tool_policy = ws["tool_policy"]
            workspace_id = run.workspace_id
            model, created = begin_operation(db, run_id, "synthetic:model", {}, run.workflow_version)
            assert created
            model.status, model.result = "completed", {"response_mac": "synthetic"}
            model_id = model.operation_id
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            listener = await V10OwnerListener.start(worker)
            assert listener is not None
            worker._v10_owner_listener = listener
            try:
                ticket = register_fence(owner, run_id, "stage-agent-v10", schema=settings.checkpoint_schema)
                root_id = "synthetic-root"
                async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as saver:
                    await saver.conn.execute(
                        "INSERT INTO checkpoints(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                        "VALUES (%s, '', %s, '{}'::jsonb, '{}'::jsonb)", (run_id, root_id))
                    receipt = _ConsumedCorrectionReceipt(run_id, "{}", owner, owner.connection,
                        listener.connection, ticket, root_id, model_id, "binding", "closure")
                    listener._correction_receipts[run_id] = receipt
                    listener.consumed.add(run_id)
                    async def synthetic_closure(*args, **kwargs):
                        return True, "synthetic-scope"
                    monkeypatch.setattr(v10_closure, "inspect_consumed_correction_phase", synthetic_closure)
                    monkeypatch.setattr(correction_phase, "require_consumed_phase_identity",
                        lambda db, *args, **kwargs: (db.get(Run, run_id), receipt))
                    monkeypatch.setattr(correction_model, "load_recorded_correction_batch", lambda *args: [{"action": {"id": "synthetic-action", "task_id": "synthetic-task"},
                            "tool_call": {"id": "synthetic-call"}}])
                    monkeypatch.setattr(v10_sealed_saver, "verify_pinned_seal_inventory", lambda *args: ({}, "synthetic-head"))
                    monkeypatch.setattr(v10_drain, "get_drained_execution", lambda *args, **kwargs: object())
                    monkeypatch.setattr(correction_reservation, "validate_execute_host", lambda *args: None)
                    counter = iter([1, 0] if case == "budget" else [1, 1, 1, 1])
                    monkeypatch.setattr(correction_reservation, "remaining_active_budget", lambda *args, **kwargs: next(counter))
                    original_live = listener._live
                    count = 0
                    async def gated_live():
                        nonlocal count
                        count += 1
                        if count == 2 and case == "revoke":
                            def revoke():
                                with factory.begin() as db:
                                    db.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update()).enabled = False
                            await asyncio.to_thread(revoke)
                        if count == 2 and case == "taint":
                            listener.claim_failed(run_id)
                        return await original_live()
                    monkeypatch.setattr(listener, "_live", gated_live)
                    phase_id = operation_id(run_id, "v10-correction:phase", {"version": 1})
                    class SyntheticGraph:
                        async def aget_state(self, config):
                            assert config["configurable"]["thread_id"] == run_id
                            return SimpleNamespace(config={"configurable": {"checkpoint_id": root_id}},
                                values={"dispatch": {"tasks": [{"id": "synthetic-task", "write_paths": ["."]}]}})
                    kwargs = dict(ticket=ticket, root_id=root_id, graph=SyntheticGraph(),
                                  executor=object(), checkpointer=saver)
                    if case in {"revoke", "budget"}:
                        with pytest.raises(correction_reservation.CorrectionReservationUnavailable):
                            await correction_phase.prepare_correction_phase(worker, run_id, **kwargs)
                    else:
                        result = await correction_phase.prepare_correction_phase(worker, run_id, **kwargs)
                        assert result["tool_dispatch"] is False
                        if case == "repeat":
                            assert result["status"] == "PREPARED"
                            result = await correction_phase.prepare_correction_phase(worker, run_id, **kwargs)
                        assert result["status"] == "PENDING_REVIEW"
                    with factory() as db:
                        phase, run = db.get(OperationRecord, phase_id), db.get(Run, run_id)
                        if case == "budget":
                            assert phase is None
                        else:
                            assert phase.status == "uncertain"
                            assert phase.result["phase"]["task_scope"] == {"id": "synthetic-task", "write_paths": ["."]}
                            assert phase.result["phase"]["items"] == [{"action_id": "synthetic-action",
                                "tool_call_id": "synthetic-call", "state": "PLANNED"}]
                            assert run.status == "pending_review"
            finally:
                await listener.close()


