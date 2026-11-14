"""Concurrent explicit reconciliation against the isolated real PostgreSQL DB."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from opensandbox.exceptions import SandboxApiException
from test_m4_permissions_postgres import seed
from test_postgres import postgres_environment  # noqa: F401

from omniharness.m4_reconciliation import ReconciliationRequest, reconcile_tool
from omniharness.m4_services import acquire_workspace_lease
from omniharness.models import OperationRecord, Run, RunEvent, ToolOperation, WorkspaceLease


def test_concurrent_ack_retires_once_and_releases_despite_unknown_model(postgres_environment, monkeypatch):  # noqa: F811
    settings, _, factory, _ = postgres_environment
    settings.sandbox_image = "sha256:" + "1" * 64
    settings.sandbox_key_file = settings.secret_dir / "sandbox-reconcile.key"
    settings.sandbox_key_file.write_text("isolated-controller-key-" * 3)
    run_id, op_id = seed(factory, 1)[0]
    with factory.begin() as db:
        run, operation = db.get(Run, run_id), db.get(ToolOperation, op_id)
        assert acquire_workspace_lease(db, run_id)
        run.status = "cancelled"
        run.config_snapshot = {"workspace": {"id": run.workspace_id, "sandbox": {
            "endpoint": settings.sandbox_endpoint, "image": settings.sandbox_image,
            "snapshot_root": settings.sandbox_snapshot_root, "instance_id": settings.instance_id,
            "max_command_seconds": settings.sandbox_max_command_seconds}}}
        for suffix, kind in (("tool", "tool_calls"), ("model", "model_calls")):
            db.add(OperationRecord(operation_id=suffix, run_id=run_id, node_id="agent:execute:" + kind,
                                   parameters_hash="b" * 64, status="uncertain"))
        db.flush()
        operation.operation_record_id = "tool"
        operation.status, operation.sandbox_id, operation.slot_claimed = "uncertain", "test-sandbox", True

    proof_barrier = Barrier(2)
    class Manager:
        async def get_sandbox_info(self, identity):
            assert identity == "test-sandbox"
            proof_barrier.wait(timeout=10)
            raise SandboxApiException(message="not found", status_code=404)
        async def close(self):
            pass
    async def create(**kwargs):
        return Manager()
    monkeypatch.setattr("omniharness.m4_reconciliation.SandboxManager.create", create)
    body = ReconciliationRequest(fingerprint="a" * 64, acknowledge_unverified=True, client_request_id="same-ack")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(asyncio.run, reconcile_tool(factory, settings, op_id, body)) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0] == results[1] and results[0]["workspace_released"]
    with factory() as db:
        run = db.get(Run, run_id)
        assert db.get(WorkspaceLease, run.workspace_id) is None
        assert db.get(OperationRecord, "model").status == "uncertain"
        assert db.get(OperationRecord, "tool").status == "reconciled"
        assert db.query(RunEvent).filter_by(run_id=run_id, event_type="tool.reconciled").count() == 1
