"""Isolated lifecycle checks; no saver authorization or tool dispatch."""

import asyncio
from types import SimpleNamespace

import pytest

from omniharness.correction_phase import _set_phase_review
from omniharness.v10_correction_saver import V10CorrectionSaver


@pytest.mark.asyncio
async def test_cancelled_close_revokes_grant_and_marks_review_after_write_drains(monkeypatch):
    saver = object.__new__(V10CorrectionSaver)
    saver.lock = asyncio.Lock()
    saver._write_idle = asyncio.Event()
    saver._active_writes = 1
    saver._writes_closed = False
    saver._phase_token = object()
    saver.ticket = SimpleNamespace(thread_id="run")
    saver._grant = SimpleNamespace(phase_id="phase")
    saver._worker = object()
    invalidated = []
    saver._listener = SimpleNamespace(claim_failed=invalidated.append)
    marked = asyncio.Event()
    monkeypatch.setattr("omniharness.v10_saver_guard.close_correction_writer", lambda *args: None)
    monkeypatch.setattr("omniharness.v10_correction_saver._mark_phase_review", lambda *args: marked.set())
    closer = asyncio.create_task(saver.close_writes())
    await asyncio.sleep(0)
    assert saver._writes_closed
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    assert invalidated == ["run"] and not marked.is_set()
    saver._active_writes = 0
    saver._write_idle.set()
    await asyncio.wait_for(marked.wait(), timeout=1)


@pytest.mark.parametrize("status", ["cancel_requested", "cancelled", "pause_requested", "paused", "completed", "failed", "interrupted"])
def test_uncertain_phase_cleanup_preserves_control_and_terminal_status(status, monkeypatch):
    expired = []
    monkeypatch.setattr("omniharness.m4_services.expire_approvals", lambda db, identity: expired.append(identity))
    run = SimpleNamespace(id="run", status=status)
    phase = SimpleNamespace(status="running", error_code=None, finished_at=None)
    _set_phase_review(object(), run, phase)
    assert run.status == status
    assert phase.status == "uncertain" and phase.error_code == "PHASE_UNKNOWN"
    assert expired == (["run"] if status in {"pause_requested", "paused"} else [])


def test_phase_review_expires_approval_in_same_transaction(monkeypatch):
    events = []
    monkeypatch.setattr("omniharness.m4_services.expire_approvals", lambda db, identity: events.append(("expire", identity)))
    monkeypatch.setattr("omniharness.correction_phase.add_event", lambda *args: events.append(("run", args[2])))
    db = SimpleNamespace(scalar=lambda _: None)
    run = SimpleNamespace(id="run", status="running", error_code=None, error_message=None)
    phase = SimpleNamespace(status="running", error_code=None, finished_at=None)
    _set_phase_review(db, run, phase)
    assert run.status == "pending_review" and phase.status == "uncertain"
    assert events == [("expire", "run"), ("run", "run.pending_review")]


@pytest.mark.asyncio
async def test_delayed_cleanup_does_not_reverse_cancelled_run(monkeypatch):
    saver = object.__new__(V10CorrectionSaver)
    saver.lock, saver._write_idle = asyncio.Lock(), asyncio.Event()
    saver._active_writes, saver._writes_closed, saver._phase_token = 1, False, object()
    saver.ticket, saver._grant, saver._worker = (SimpleNamespace(thread_id="run"),
                                                SimpleNamespace(phase_id="phase"), object())
    saver._listener = SimpleNamespace(claim_failed=lambda _: None)
    run = SimpleNamespace(status="cancel_requested")
    phase = SimpleNamespace(status="running", error_code=None, finished_at=None)
    marked = asyncio.Event()

    def delayed_mark(*_):
        _set_phase_review(object(), run, phase)
        marked.set()

    monkeypatch.setattr("omniharness.v10_saver_guard.close_correction_writer", lambda *args: None)
    monkeypatch.setattr("omniharness.v10_correction_saver._mark_phase_review", delayed_mark)
    closer = asyncio.create_task(saver.close_writes())
    await asyncio.sleep(0)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    run.status = "cancelled"
    saver._active_writes = 0
    saver._write_idle.set()
    await asyncio.wait_for(marked.wait(), timeout=1)
    assert run.status == "cancelled" and phase.status == "uncertain"
