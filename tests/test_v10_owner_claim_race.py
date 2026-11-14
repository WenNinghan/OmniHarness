"""State-machine interleavings; no real owner qualification or side effects."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from omniharness.v10_owner_runtime import V10OwnerListener


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["confirm_claim", "consume_owner_presence"])
@pytest.mark.parametrize("blocked_check", [1, 2])
@pytest.mark.parametrize("invalidation", ["taint", "owner_lost", "listener_replaced"])
async def test_change_during_liveness_wait_cannot_restore_qualification(
    monkeypatch, operation, blocked_check, invalidation
):
    origin = {"run_id": "run"}
    run = SimpleNamespace(id="run", status="running", started_at=True, workspace_id="workspace",
                          config_snapshot={"v10_owner_origin": origin})
    user = object()

    class DB:
        def __init__(self):
            self.values = iter((run, user))

        def scalar(self, _):
            return next(self.values)

    @contextmanager
    def transaction():
        yield DB()

    owner = SimpleNamespace(lost=False, connection=object())
    worker = SimpleNamespace(lock=owner, settings=object(), transaction=transaction)
    listener = object.__new__(V10OwnerListener)
    listener.worker, listener.connection, listener.identity, listener.alive = worker, object(), {}, True
    listener.pending, listener.committing, listener.accepted = {}, {}, {}
    listener.consumed, listener.tainted, listener._correction_receipts = set(), set(), {}
    listener._correction_phases = {}
    worker._v10_owner_listener = listener
    if operation == "confirm_claim":
        listener.committing["run"] = origin
    else:
        listener.accepted["run"] = origin

    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def live():
        nonlocal calls
        calls += 1
        if calls == blocked_check:
            entered.set()
            await release.wait()
        return True

    monkeypatch.setattr(listener, "_live", live)
    monkeypatch.setattr("omniharness.m4_services.validate_workspace_lease", lambda *args: True)
    monkeypatch.setattr("omniharness.v10_owner_auth.verify_run_origin", lambda *args, **kwargs: True)
    task = asyncio.create_task(getattr(listener, operation)("run"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    if invalidation == "taint":
        listener.claim_failed("run")
    elif invalidation == "owner_lost":
        owner.lost = True
    else:
        worker._v10_owner_listener = object()
    release.set()
    assert await task is False
    assert "run" in listener.tainted and "run" not in listener.accepted

