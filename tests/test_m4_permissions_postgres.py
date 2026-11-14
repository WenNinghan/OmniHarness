"""M4 locking tests on the isolated PostgreSQL fixture, never production."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from sqlalchemy import select
from test_postgres import postgres_environment  # noqa: F401

from omniharness.m4_services import acquire_workspace_lease, claim_tool_slot, ensure_tool_request
from omniharness.models import Conversation, ModelConfig, Provider, Run, ToolOperation, Workspace


def seed(factory, count):
    with factory.begin() as db:
        provider = Provider(adapter_type="mock")
        workspace = Workspace(name="test", host_path="G:/isolated-test", enabled=True, probe={})
        db.add_all([provider, workspace])
        db.flush()
        model = ModelConfig(provider_id=provider.id, name="test", model_name="demo")
        db.add(model)
        db.flush()
        result = []
        for _ in range(count):
            conversation = Conversation()
            db.add(conversation)
            db.flush()
            run = Run(conversation_id=conversation.id, model_config_id=model.id, model_label="test",
                      ordinal=1, client_request_id=str(uuid4()), status="running", mode="mock",
                      workspace_id=workspace.id, config_snapshot={})
            db.add(run)
            db.flush()
            op_id = uuid4().hex * 2
            ensure_tool_request(db, run_id=run.id, operation_id=op_id, task_id="task", tool_call_id="call",
                tool_name="run_command", tool_version="v1", parameters={}, binding_fingerprint="a" * 64,
                requires_approval=False)
            result.append((run.id, op_id))
        return result


def test_parallel_claims_enforce_two_instance_slots(postgres_environment):  # noqa: F811
    factory = postgres_environment[2]
    pairs = seed(factory, 6)
    barrier = Barrier(len(pairs))

    def claim(pair):
        barrier.wait(timeout=10)
        with factory.begin() as db:
            return claim_tool_slot(db, *pair)

    with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
        results = list(pool.map(claim, pairs))
    assert sum(results) == 2
    with factory() as db:
        assert len(list(db.scalars(select(ToolOperation).where(ToolOperation.slot_claimed)))) == 2


def test_workspace_lease_single_writer_across_concurrent_runs(postgres_environment):  # noqa: F811
    factory = postgres_environment[2]
    pairs = seed(factory, 5)
    barrier = Barrier(len(pairs))

    def claim(pair):
        barrier.wait(timeout=10)
        with factory.begin() as db:
            return acquire_workspace_lease(db, pair[0])

    with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
        results = list(pool.map(claim, pairs))
    assert sum(results) == 1
