"""Interactive interrupt timing: fail closed on missing evidence or side effects."""

import copy
import time
from types import SimpleNamespace

import pytest
from conftest import create_chat
from sqlalchemy import select

from omniharness.clarification_service import create_clarification
from omniharness.interrupt_timing import recovered_seconds
from omniharness.m3_worker import _recover_checkpointed_question, _wait_for_user
from omniharness.models import OperationRecord, Run, RunBudget, RunEvent, ToolOperation
from omniharness.services import add_event, budget_limits, ensure_budget
from omniharness.worker import Worker

BOOT = "3c61fd2b-b6cc-49b2-af62-a35d3cb84afd"
NONCE = "a" * 32


def _setup(environment, monkeypatch):
    from omniharness import interrupt_timing

    monkeypatch.setattr(interrupt_timing, "current_boot_id", lambda: BOOT)
    settings, engine, factory, client = environment
    _, _, created = create_chat(client)
    run_id = created["run_id"]
    now = time.monotonic_ns()
    segment = {"schema": 1, "nonce": NONCE, "boot_id": BOOT, "start_ns": now - 3_000_000_000}
    marker = {"schema": 1, "nonce": NONCE, "boot_id": BOOT, "end_ns": now - 1_000_000_000}
    with factory.begin() as db:
        run = db.get(Run, run_id)
        run.workflow_version, run.status = "stage-agent-v7", "running"
        ensure_budget(db, run_id, budget_limits(settings))
        budget = db.get(RunBudget, run_id)
        budget.active_seconds, budget.active_segment_open = 7, True
        question = create_clarification(db, run, "test", "task", "请选择？")
        question_id = question.id
        add_event(db, run, "budget.active_segment_started", segment)
    value = {
        "type": "user_clarification",
        "run_id": run_id,
        "clarification_id": question_id,
        "timing": marker,
    }
    state = SimpleNamespace(interrupts=[SimpleNamespace(value=value)], next=["node"])
    return Worker(settings, engine), factory, run_id, state


def test_valid_checkpoint_recovers_once_without_wait_time(environment, monkeypatch):
    worker, factory, run_id, state = _setup(environment, monkeypatch)
    assert _recover_checkpointed_question(worker, run_id, state) is True
    assert _recover_checkpointed_question(worker, run_id, state) is False
    with factory() as db:
        budget, run = db.get(RunBudget, run_id), db.get(Run, run_id)
        assert budget.active_seconds == 9 and not budget.active_segment_open
        assert run.status == "awaiting_user"
        events = list(
            db.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "run.awaiting_user")
            )
        )
        assert len(events) == 1 and events[0].payload["active_seconds_settled"] == 2


@pytest.mark.parametrize(
    "scenario", ["valid", "missing_marker", "wrong_start", "noninteractive", "multi_interrupt"]
)
def test_normal_wait_uses_proven_marker_otherwise_full_live_time(environment, monkeypatch, scenario):
    worker, factory, run_id, state = _setup(environment, monkeypatch)
    with factory.begin() as db:
        event = db.scalar(
            select(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.event_type == "budget.active_segment_started"
            )
        )
        started = event.payload["start_ns"] / 1_000_000_000
        if scenario == "noninteractive":
            db.get(Run, run_id).workflow_version = "stage-agent-v6"
    if scenario == "missing_marker":
        del state.interrupts[0].value["timing"]
    if scenario == "wrong_start":
        started -= 10
    if scenario == "multi_interrupt":
        state.interrupts.append(copy.deepcopy(state.interrupts[0]))
    assert _wait_for_user(worker, run_id, started=started, checkpoint_state=state) is False
    with factory() as db:
        budget = db.get(RunBudget, run_id)
        assert db.get(Run, run_id).status == "awaiting_user"
        assert not budget.active_segment_open
        if scenario == "valid":
            # Exactly the same segment start/end as crash recovery above.
            assert budget.active_seconds == 9
        else:
            assert budget.active_seconds >= 10


@pytest.mark.parametrize(
    "problem",
    [
        "nonce",
        "boot",
        "rollback",
        "future",
        "other_run",
        "question",
        "no_checkpoint",
        "multi_interrupt",
        "old_version",
        "running_operation",
        "uncertain_tool",
        "active_slot",
        "cancel_requested",
    ],
)
def test_bad_or_ambiguous_evidence_fails_closed(environment, monkeypatch, problem):
    worker, factory, run_id, state = _setup(environment, monkeypatch)
    value = state.interrupts[0].value
    if problem == "nonce":
        value["timing"]["nonce"] = "b" * 32
    if problem == "boot":
        value["timing"]["boot_id"] = "0" * 36
    if problem == "rollback":
        value["timing"]["end_ns"] = 1
    if problem == "future":
        value["timing"]["end_ns"] = time.monotonic_ns() + 10**12
    if problem == "other_run":
        value["run_id"] = "another"
    if problem == "question":
        value["clarification_id"] = "missing"
    if problem == "no_checkpoint":
        state.next = []
    if problem == "multi_interrupt":
        state.interrupts.append(copy.deepcopy(state.interrupts[0]))
    with factory.begin() as db:
        if problem == "old_version":
            db.get(Run, run_id).workflow_version = "stage-agent-v6"
        if problem == "cancel_requested":
            db.get(Run, run_id).status = "cancel_requested"
        if problem == "running_operation":
            db.add(
                OperationRecord(
                    operation_id="op",
                    run_id=run_id,
                    node_id="model",
                    parameters_hash="b" * 64,
                    status="running",
                )
            )
        if problem in {"uncertain_tool", "active_slot"}:
            db.add(
                ToolOperation(
                    id="tool",
                    run_id=run_id,
                    task_id="task",
                    tool_call_id="call",
                    tool_name="workspace_patch",
                    tool_version="v1",
                    parameters={},
                    binding_fingerprint="c" * 64,
                    status="uncertain" if problem == "uncertain_tool" else "completed",
                    slot_claimed=problem == "active_slot",
                )
            )
    assert _recover_checkpointed_question(worker, run_id, state) is False
    with factory() as db:
        assert db.get(RunBudget, run_id).active_segment_open
        assert db.get(RunBudget, run_id).active_seconds == 7


def test_timing_requires_same_boot_and_strict_integer_types():
    segment = {"schema": 1, "nonce": NONCE, "boot_id": BOOT, "start_ns": 1}
    marker = {"schema": 1, "nonce": NONCE, "boot_id": BOOT, "end_ns": 1_000_000_002}
    assert recovered_seconds(segment, marker, now_ns=2_000_000_000, boot_id=BOOT) == 2
    assert recovered_seconds(segment, marker, now_ns=2_000_000_000, boot_id=None) is None
    assert (
        recovered_seconds({**segment, "start_ns": True}, marker, now_ns=2_000_000_000, boot_id=BOOT) is None
    )
