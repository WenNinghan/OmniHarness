"""Recovery fault injection in a fresh schema of the dedicated test database."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from test_m3_host import host_for, identity, prepared
from test_postgres import postgres_environment  # noqa: F401
from test_provider_recovery import call, http_error, policy
from test_provider_recovery import (
    test_child_link_and_reservation_rollback_together as check_atomic_reservation,
)
from test_provider_recovery import (
    test_running_child_blocks_reentry as check_running_child,
)
from test_provider_recovery import (
    test_two_requests_two_reservations_and_crash_reentry_exactly_replays_child as check_exact_replay,
)

from omniharness.api import create_app
from omniharness.models import OperationRecord, RunBudget
from omniharness.security import create_pairing_code
from omniharness.worker import Worker, WorkerLock, WorkerOwnershipLost


@pytest.fixture
def recovery_postgres(request):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        code = create_pairing_code(factory)
        assert client.post("/api/v1/auth/pair", json={"code": code}).status_code == 200
        yield settings, engine, factory, client


@pytest.mark.asyncio
async def test_postgres_completed_child_is_exactly_replayed_after_graph_crash(recovery_postgres):
    await check_exact_replay(recovery_postgres)


@pytest.mark.asyncio
async def test_postgres_child_budget_and_parent_link_are_atomic(recovery_postgres):
    await check_atomic_reservation(recovery_postgres)


@pytest.mark.asyncio
async def test_postgres_running_child_is_not_dispatched_twice(recovery_postgres):
    await check_running_child(recovery_postgres)


@pytest.mark.asyncio
async def test_postgres_lost_owner_during_backoff_cannot_reserve_child(recovery_postgres, monkeypatch):
    settings, engine, factory, client = recovery_postgres
    run_id = prepared(settings, engine, factory, client)
    worker, calls = Worker(settings, engine), []
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        with lock.connection.begin():
            owner_pid = lock.connection.scalar(text("SELECT pg_backend_pid()"))
        host = host_for(worker, run_id)
        async def lose_owner(seconds):
            assert not host.serial.locked()
            with engine.begin() as connection:
                assert connection.scalar(text("SELECT current_database()")) == "omniharness_test"
                assert connection.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": owner_pid}) is True
        monkeypatch.setattr("omniharness.provider_recovery.asyncio.sleep", lose_owner)
        async def dispatch(attempt):
            calls.append(attempt.invocation_id)
            raise http_error(429)
        with pytest.raises(WorkerOwnershipLost):
            await call(host, identity(run_id), dispatch, policy(delay=0.1))
    assert len(calls) == 1
    with factory() as db:
        row = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
        assert row.status == "failed"
        assert "child_invocation_id" not in row.result["provider_recovery"]
        assert db.get(RunBudget, run_id).model_calls == 1
