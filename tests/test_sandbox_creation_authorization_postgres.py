"""Authorization holds DB locks while recording the candidate dispatch claim."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from types import SimpleNamespace

from test_m4_permissions_postgres import seed
from test_postgres import postgres_environment  # noqa: F401

from omniharness.m4_services import acquire_workspace_lease
from omniharness.models import OperationRecord, Run, ToolOperation, WorkspacePolicy, WorkspaceSnapshot
from omniharness.sandbox import SandboxConfig
from omniharness.sandbox_creation_authorization import authorize_creation
from omniharness.sandbox_creation_journal import CreationBinding, CreationJournal


def test_cancel_waits_for_locked_authorization_and_claim(postgres_environment, tmp_path):  # noqa: F811
    factory = postgres_environment[2]
    run_id, op_id = seed(factory, 1)[0]
    config = SandboxConfig(endpoint="http://localhost:19080", api_key="controller-" * 5,
        image="sha256:" + "a" * 64, snapshot_root="/snapshots", instance_id="test")
    request = SimpleNamespace(run_id=run_id, operation_id=op_id, operation_fingerprint="a" * 64,
                              snapshot_path="/snapshots/test", timeout_seconds=120)
    with factory.begin() as db:
        run, op = db.get(Run, run_id), db.get(ToolOperation, op_id)
        assert acquire_workspace_lease(db, run_id)
        grant, policy = {"instance_id": "test"}, {"command_approval": False, "tools": ["run_command"]}
        db.add(WorkspacePolicy(workspace_id=run.workspace_id, version=1, grant=grant, tool_policy=policy))
        run.config_snapshot = {"workspace": {"id": run.workspace_id, "policy_version": 1,
            "grant": grant, "tool_policy": policy, "sandbox": {"endpoint": config.endpoint,
                "image": config.image, "snapshot_root": config.snapshot_root, "instance_id": config.instance_id,
                "max_command_seconds": config.max_command_seconds}}}
        snapshot = WorkspaceSnapshot(id="test-snapshot", workspace_id=run.workspace_id, run_id=run_id,
            task_id=op.task_id, policy_version=1, manifest={}, workdir=request.snapshot_path)
        ledger = OperationRecord(operation_id="test-ledger", run_id=run_id, node_id="agent:execute:tool_calls",
                                 parameters_hash="b" * 64, status="running")
        db.add_all([snapshot, ledger])
        db.flush()
        op.snapshot_id, op.operation_record_id = snapshot.id, ledger.operation_id
        op.status, op.slot_claimed, op.parameters = "dispatched", True, {"timeout_seconds": 120}

    journal = CreationJournal.initialize(tmp_path / "journal.sqlite", "e" * 64)
    binding = CreationBinding("test", run_id, op_id, "b" * 64, "c" * 64)
    locked, release, cancel_started = Event(), Event(), Event()

    def claim():
        result = journal.claim_dispatch(binding)
        locked.set()
        assert release.wait(timeout=10)
        return result

    def authorize():
        return authorize_creation(factory, config, request, claim)

    def cancel():
        cancel_started.set()
        with factory.begin() as db:
            db.get(Run, run_id).status = "cancelled"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(authorize)
        assert locked.wait(timeout=10)
        second = pool.submit(cancel)
        assert cancel_started.wait(timeout=10)
        try:
            second.result(timeout=0.5)
            raise AssertionError("Cancellation committed during the locked claim")
        except TimeoutError:
            pass
        finally:
            release.set()
        assert first.result(timeout=10) is True
        second.result(timeout=10)
    assert journal.lookup(binding).state == "DISPATCHED"
