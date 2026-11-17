"""Admission diagnostics remain useful without persisting request content."""
import pytest
from conftest import create_chat
from sqlalchemy import select

from omniharness.context_host import record_context_rejection
from omniharness.context_runtime import ContextPreparationError
from omniharness.models import RunEvent
from omniharness.worker import Worker, WorkerOwnershipLost


def test_context_rejection_saves_counts_without_error_or_request_body(environment):
    settings, engine, factory, client = environment
    _, _, result = create_chat(client)
    identity = result["run_id"]
    diagnostic = {"reason": "input_tokens", "observed": 64001, "limit": 64000, "window_known": False}
    error = ContextPreparationError("CONTEXT_LIMIT", "SECRET_REQUEST_AND_PROVIDER_BODY", diagnostic=diagnostic)
    worker = Worker(settings, engine)
    record_context_rejection(worker, identity, error)
    error.diagnostic = {**diagnostic, "request_body": "SECRET_REQUEST_AND_PROVIDER_BODY"}
    record_context_rejection(worker, identity, error)
    with factory() as db:
        events = list(db.scalars(select(RunEvent).where(
            RunEvent.run_id == identity, RunEvent.event_type == "context.admission_rejected")))
        assert [event.payload for event in events] == [{"mode": "mock", **diagnostic}]


def test_context_rejection_cannot_bypass_lost_worker_ownership(environment):
    settings, engine, factory, client = environment
    _, _, result = create_chat(client)
    identity = result["run_id"]
    worker = Worker(settings, engine)

    class LostLock:
        def session(self, sessions):
            raise WorkerOwnershipLost("lost before diagnostic write")

    worker.lock = LostLock()
    error = ContextPreparationError("CONTEXT_LIMIT", "safe", diagnostic={
        "reason": "message_count", "observed": 501, "limit": 500, "window_known": False})
    with pytest.raises(WorkerOwnershipLost):
        record_context_rejection(worker, identity, error)
    with factory() as db:
        assert db.scalar(select(RunEvent.id).where(
            RunEvent.run_id == identity, RunEvent.event_type == "context.admission_rejected")) is None
