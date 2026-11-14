import pytest
from opensandbox.exceptions import SandboxApiException
from test_m4_api import workspace  # noqa: F401
from test_m4_reconciliation import unknown  # noqa: F401

from omniharness.m4_reconciliation import ReconciliationRequest
from omniharness.models import OperationRecord, Run, ToolOperation, WorkspaceLease, WorkspaceSnapshot
from omniharness.sandbox import SandboxConfig, operation_metadata
from omniharness.sandbox_creation_api import CreationRequest, candidate_binding
from omniharness.sandbox_creation_journal import CreationJournal
from omniharness.sandbox_creation_reconciliation import reconcile_created
from omniharness.services import ServiceError


@pytest.fixture
def candidate(environment, unknown, tmp_path):  # noqa: F811
    settings, _, factory, _ = environment
    run_id, op_id, _, body = unknown
    config = SandboxConfig(endpoint=settings.sandbox_endpoint, api_key="isolation-key-" * 3,
        image=settings.sandbox_image, snapshot_root=settings.sandbox_snapshot_root,
        instance_id=settings.instance_id, max_command_seconds=settings.sandbox_max_command_seconds)
    request = CreationRequest(run_id=run_id, operation_id=op_id, operation_fingerprint=body["fingerprint"],
        snapshot_path=config.snapshot_root + "/fixture", execd_token="x" * 32, timeout_seconds=120)
    with factory.begin() as db:
        op, run = db.get(ToolOperation, op_id), db.get(Run, run_id)
        snapshot = WorkspaceSnapshot(id="candidate-snapshot", workspace_id=run.workspace_id, run_id=run_id,
            task_id=op.task_id, policy_version=1, manifest={}, workdir=request.snapshot_path)
        db.add(snapshot)
        db.flush()
        op.snapshot_id, op.sandbox_id, op.parameters = snapshot.id, None, {"timeout_seconds": 120}
    journal = CreationJournal.initialize(tmp_path / "journal.sqlite", "e" * 64)
    binding = candidate_binding(config, request)
    assert journal.claim_dispatch(binding)
    journal.record_created(binding, "sandbox-candidate")
    return factory, config, journal, request, ReconciliationRequest(**body)


class Manager:
    def __init__(self, binding):
        self.binding, self.killed = binding, False

    async def get_sandbox_info(self, identity):
        if self.killed:
            raise SandboxApiException("not found", status_code=404)
        from types import SimpleNamespace
        return SimpleNamespace(id=identity, metadata=operation_metadata(
            self.binding.instance_id, self.binding.run_id, self.binding.operation_id))

    async def kill_sandbox(self, identity):
        self.killed = True


@pytest.mark.asyncio
async def test_missing_id_with_complete_evidence_releases_only_as_unknown(candidate):
    factory, config, journal, request, body = candidate
    manager = Manager(candidate_binding(config, request))

    async def engine(record):
        return True

    value = await reconcile_created(factory, config, journal, request, body, manager, engine)
    assert value["workspace_released"] and value["reconciliation"]["outcome"] == "unknown"
    assert value["reconciliation"]["verification"] == "UNVERIFIED" and manager.killed
    with factory() as db:
        run, op = db.get(Run, request.run_id), db.get(ToolOperation, request.operation_id)
        assert not op.slot_claimed and op.status == "reconciled"
        assert db.get(OperationRecord, op.operation_record_id).status == "reconciled"
        assert db.get(WorkspaceLease, run.workspace_id) is None
    assert (await reconcile_created(factory, config, journal, request, body, manager, engine)) == value


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_journal", "engine_present", "binding_changed"])
async def test_reconciliation_never_releases_without_complete_stable_evidence(candidate, tmp_path, case):
    factory, config, journal, request, body = candidate
    manager = Manager(candidate_binding(config, request))
    if case == "missing_journal":
        journal = CreationJournal.initialize(tmp_path / "empty.sqlite", "f" * 64)

    async def engine(record):
        if case == "binding_changed":
            with factory.begin() as db:
                db.get(ToolOperation, request.operation_id).parameters = {"timeout_seconds": 3}
        return case != "engine_present"

    with pytest.raises(ServiceError):
        await reconcile_created(factory, config, journal, request, body, manager, engine)
    with factory() as db:
        run, op = db.get(Run, request.run_id), db.get(ToolOperation, request.operation_id)
        assert op.slot_claimed and op.status == "uncertain"
        assert db.get(WorkspaceLease, run.workspace_id) is not None
    if case == "missing_journal":
        assert not manager.killed
