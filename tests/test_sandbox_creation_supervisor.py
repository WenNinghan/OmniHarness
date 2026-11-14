import asyncio

import pytest

from omniharness.sandbox_creation_journal import CreationBinding, CreationJournal
from omniharness.sandbox_creation_supervisor import CreationAlreadyDispatched, CreationSupervisor

JOURNAL_ID = "f" * 64


@pytest.fixture
def binding():
    return CreationBinding("isolated", "run-1", "a" * 64, "b" * 64, "c" * 64)


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_abandon_late_creation_receipt(tmp_path, binding):
    path = tmp_path / "journal.sqlite"
    journal = CreationJournal.initialize(path, JOURNAL_ID)
    supervisor = CreationSupervisor(journal)
    dispatched, complete = asyncio.Event(), asyncio.Event()
    calls = []

    async def delayed():
        calls.append("create")
        dispatched.set()
        await complete.wait()
        return "sandbox-1"

    caller = asyncio.create_task(supervisor.create(binding, delayed))
    await dispatched.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert journal.lookup(binding).state == "DISPATCHED"
    with pytest.raises(CreationAlreadyDispatched):
        await supervisor.create(binding, delayed)
    complete.set()
    await supervisor.drain()
    assert CreationJournal(path, JOURNAL_ID).lookup(binding).sandbox_id == "sandbox-1"
    assert calls == ["create"]


@pytest.mark.asyncio
async def test_lost_response_stays_unknown_and_cannot_be_replayed(tmp_path, binding):
    journal = CreationJournal.initialize(tmp_path / "journal.sqlite", JOURNAL_ID)
    supervisor = CreationSupervisor(journal)
    calls = []

    async def lost():
        calls.append("create")
        raise ConnectionError("response lost after possible remote dispatch")

    with pytest.raises(ConnectionError):
        await supervisor.create(binding, lost)
    assert journal.lookup(binding).state == "DISPATCHED"
    assert journal.lookup(binding).sandbox_id is None
    with pytest.raises(CreationAlreadyDispatched):
        await supervisor.create(binding, lost)
    assert calls == ["create"]


@pytest.mark.asyncio
async def test_journal_failure_blocks_dispatch(binding):
    class BrokenJournal:
        def claim_dispatch(self, _binding):
            raise OSError("journal write failed")

    called = []

    async def create():
        called.append(True)
        return "sandbox-1"

    with pytest.raises(OSError):
        await CreationSupervisor(BrokenJournal()).create(binding, create)
    assert called == []


@pytest.mark.asyncio
async def test_persisted_claim_cannot_be_consumed_after_restart_or_twice(tmp_path, binding):
    journal = CreationJournal.initialize(tmp_path / "journal.sqlite", JOURNAL_ID)
    first, restarted = CreationSupervisor(journal), CreationSupervisor(journal)
    called = []

    async def create():
        called.append(True)
        return "sandbox-1"

    assert first.claim(binding)
    with pytest.raises(CreationAlreadyDispatched):
        await restarted.create_claimed(binding, create)
    await first.create_claimed(binding, create)
    with pytest.raises(CreationAlreadyDispatched):
        await first.create_claimed(binding, create)
    assert called == [True]
