"""Actual PostgreSQL completion transactions, using only the isolated test DB."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select
from test_command_exit_evidence import setup_command
from test_m4_permissions_postgres import seed
from test_postgres import postgres_environment  # noqa: F401

from omniharness.m3_worker import _complete
from omniharness.models import Message, Run, RunEvent
from omniharness.worker import Worker, WorkerLock


def prepare(settings, engine, factory, *, already_streamed):
    run_id, operation_id = seed(factory, 1)[0]
    _, _, _, artifact_id = setup_command(factory, run_id=run_id, operation_id=operation_id, exit_code=1)
    answer = f"| 检查 | 退出码 | 结果 |\n|---|---|---|\n| 扩展检查 | 0 | 工件 `{artifact_id}`，该次 exit 1 |"
    with factory.begin() as db:
        run = db.get(Run, run_id)
        run.status, run.workflow_version = "running", "stage-agent-v4"
        db.add(Message(id=str(uuid4()), conversation_id=run.conversation_id, run_id=run_id,
                       role="assistant", status="running", content=answer if already_streamed else "",
                       output_metadata={"output_status": "streaming"} if already_streamed else {}))
    return Worker(settings, engine), run_id, operation_id, artifact_id, answer


def verify(factory, run_id, operation_id, artifact_id, answer, *, expected_deltas):
    with factory() as db:
        run = db.get(Run, run_id)
        message = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence_number)))
        terminal = [event for event in events if event.event_type == "run.completed"]
        deltas = [event for event in events if event.event_type == "message.delta"]
        assert run.status == message.status == "completed"
        assert message.content == answer
        audit = message.output_metadata["command_result_audit"]
        assert audit == {"status": "conflict", "truncated": False, "conflicts": [{
            "artifact_id": artifact_id, "operation_id": operation_id,
            "claimed_exit_code": 0, "actual_exit_code": 1, "line": 3}]}
        assert len(terminal) == 1 and terminal[0].payload["output_metadata"]["command_result_audit"] == audit
        assert len(deltas) == expected_deltas
        if deltas:
            assert deltas[0].payload["delta"] == answer


@pytest.mark.parametrize("already_streamed", [False, True])
def test_postgres_worker_completion_preserves_answer_and_persists_conflict(postgres_environment, already_streamed):  # noqa: F811
    settings, engine, factory, _ = postgres_environment
    worker, run_id, operation_id, artifact_id, answer = prepare(settings, engine, factory, already_streamed=already_streamed)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        _complete(worker, run_id, answer, started=None, source_message=AIMessage(content=answer))
    verify(factory, run_id, operation_id, artifact_id, answer, expected_deltas=0 if already_streamed else 1)


def test_postgres_concurrent_completion_has_one_terminal_event(postgres_environment):  # noqa: F811
    settings, engine, factory, _ = postgres_environment
    _, run_id, operation_id, artifact_id, answer = prepare(settings, engine, factory, already_streamed=False)
    barrier = Barrier(2)

    def complete(_):
        # Two independent direct-call Workers exercise the Run row lock under
        # duplicate completion. Production normally permits only one owner.
        worker = Worker(settings, engine)
        barrier.wait(timeout=10)
        _complete(worker, run_id, answer, started=None, source_message=AIMessage(content=answer))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(complete, range(2)))
    verify(factory, run_id, operation_id, artifact_id, answer, expected_deltas=1)
