"""M3 command slice: idempotency, state machine, safe pause and session fairness."""
import asyncio

import pytest
from conftest import create_chat
from fastapi.testclient import TestClient
from sqlalchemy import select

from omniharness.api import create_app
from omniharness.models import Message, Run, RunCommand, RunEvent
from omniharness.providers import ModelDelta
from omniharness.worker import Worker


def command_path(run_id):
    return f"/api/v1/runs/{run_id}/commands"


def submit(client, run_id, command_type, request_id, content=None):
    body = {"command_type": command_type, "client_request_id": request_id}
    if content is not None:
        body["content"] = content
    return client.post(command_path(run_id), json=body)


def test_command_submission_only_records_intent(environment):
    _, _, _, client = environment
    _, _, queued = create_chat(client)
    response = submit(client, queued["run_id"], "pause", "p1")
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.json()["run_status"] == "queued"
    assert client.get(f"/api/v1/runs/{queued['run_id']}").json()["status"] == "queued"


def test_commands_are_idempotent_and_numbered_per_run(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    path = command_path(queued["run_id"])
    first = submit(client, queued["run_id"], "pause", "cmd-1")
    again = submit(client, queued["run_id"], "pause", "cmd-1")
    assert again.json()["command_id"] == first.json()["command_id"]
    assert again.json()["sequence_number"] == 1
    conflict = client.post(path, json={"command_type": "resume", "client_request_id": "cmd-1"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "REQUEST_ID_CONFLICT"
    second = submit(client, queued["run_id"], "append_requirement", "cmd-2", "请补上反例")
    third = submit(client, queued["run_id"], "resume", "cmd-3")
    assert [item.json()["sequence_number"] for item in (first, second, third)] == [1, 2, 3]
    assert [item["command_type"] for item in client.get(path).json()] == [
        "pause", "append_requirement", "resume",
    ]
    with factory() as db:
        rows = list(db.scalars(select(RunCommand).where(RunCommand.run_id == queued["run_id"])))
        assert len(rows) == 3


def test_command_endpoint_requires_session_origin_and_request_header(environment):
    settings, engine, _, client = environment
    _, _, queued = create_chat(client)
    path = command_path(queued["run_id"])
    body = {"command_type": "pause", "client_request_id": "x"}
    anonymous = TestClient(create_app(settings, engine))
    assert anonymous.post(path, json=body, headers={"X-Omni-Request": "1"}).status_code == 401
    assert client.post(path, json=body, headers={"X-Omni-Request": "0"}).status_code == 403
    assert client.post(path, json=body, headers={"Origin": "https://example.invalid"}).status_code == 403
    assert client.post(path, json={"command_type": "pause", "client_request_id": "y",
                                   "content": "not allowed"}).status_code == 422
    assert client.post(path, json={"command_type": "append_requirement",
                                   "client_request_id": "z"}).status_code == 422


def test_queued_pause_and_resume_are_safe_without_checkpoint(environment):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    worker = Worker(settings, engine)
    submit(client, queued["run_id"], "pause", "p1")
    worker.consume_commands()
    with factory() as db:
        run = db.get(Run, queued["run_id"])
        assert run.status == "paused" and run.started_at is None
    submit(client, queued["run_id"], "resume", "r1")
    worker.consume_commands()
    with factory() as db:
        assert db.get(Run, queued["run_id"]).status == "queued"
        resume = db.scalar(select(RunCommand).where(RunCommand.client_request_id == "r1"))
        assert resume.status == "applied"


async def test_pause_during_stream_keeps_text_and_refuses_replay(environment):
    settings, engine, factory, client = environment
    _, conversation, queued = create_chat(client)
    reached, closed = asyncio.Event(), asyncio.Event()
    prefix = "Partial answer. " * 10

    async def stream(config, history):
        try:
            yield ModelDelta(text=prefix)
            reached.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    worker = Worker(settings, engine, stream_fn=stream)
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(reached.wait(), timeout=3)
        assert submit(client, queued["run_id"], "pause", "p1").status_code == 202
        assert await asyncio.wait_for(task, timeout=5) == queued["run_id"]
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert closed.is_set()
    assert client.get(f"/api/v1/runs/{queued['run_id']}").json()["status"] == "paused"
    messages = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
    assert messages[-1]["content"] == prefix
    assert messages[-1]["status"] == "paused"
    with factory() as db:
        types = [row.event_type for row in db.scalars(select(RunEvent).where(
            RunEvent.run_id == queued["run_id"]).order_by(RunEvent.sequence_number))]
    assert "run.paused" in types and "run.command" in types and "run.completed" not in types

    calls = []

    async def replay(config, history):
        calls.append(history)
        yield ModelDelta(text="must never run")

    submit(client, queued["run_id"], "resume", "r1")
    Worker(settings, engine, stream_fn=replay).consume_commands()
    assert calls == []
    with factory() as db:
        resume = db.scalar(select(RunCommand).where(RunCommand.client_request_id == "r1"))
        assert resume.status == "rejected"
        events = list(db.scalars(select(RunEvent).where(
            RunEvent.run_id == queued["run_id"], RunEvent.event_type == "run.command"
        ).order_by(RunEvent.sequence_number)))
        assert events[-1].payload["reason"] == "CHECKPOINT_REQUIRED"
        assert db.get(Run, queued["run_id"]).status == "paused"


def test_append_requirement_is_stored_as_command_not_message(environment):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    response = submit(client, queued["run_id"], "append_requirement", "a1", "补充：请给出反例")
    assert response.status_code == 202
    Worker(settings, engine).consume_commands()
    with factory() as db:
        messages = list(db.scalars(select(Message).where(Message.run_id == queued["run_id"])))
        assert len(messages) == 2
        event = db.scalar(select(RunEvent).where(
            RunEvent.run_id == queued["run_id"], RunEvent.event_type == "run.command"))
        assert event.payload["content"] == "补充：请给出反例"
        assert event.payload["result"] == "applied"


async def test_commands_on_terminal_run_are_rejected_without_state_change(environment):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    await Worker(settings, engine).run_once()
    response = submit(client, queued["run_id"], "pause", "p1")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RUN_TERMINAL"
    Worker(settings, engine).consume_commands()
    with factory() as db:
        assert db.get(Run, queued["run_id"]).status == "completed"
        row = db.scalar(select(RunCommand).where(RunCommand.client_request_id == "p1"))
        assert row is None


def test_pending_commands_settle_before_cancel_terminal_event(environment):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    run_id = queued["run_id"]
    worker = Worker(settings, engine)
    assert submit(client, run_id, "pause", "p1").status_code == 202
    worker.consume_commands()
    pending = submit(client, run_id, "append_requirement", "extra", "extra input").json()
    assert submit(client, run_id, "resume", "r1").status_code == 202
    assert client.post(f"/api/v1/runs/{run_id}/cancel").json()["status"] == "cancelled"
    commands = client.get(command_path(run_id)).json()
    assert [item["status"] for item in commands] == ["applied", "rejected", "rejected"]
    duplicate = submit(client, run_id, "append_requirement", "extra", "extra input")
    assert duplicate.status_code == 202 and duplicate.json()["command_id"] == pending["command_id"]
    assert submit(client, run_id, "resume", "new").status_code == 409
    worker.consume_commands()
    with factory() as db:
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)
                                 .order_by(RunEvent.sequence_number)))
        assert [item.event_type for item in events][-3:] == [
            "run.command", "run.command", "run.cancelled",
        ]
        assert all(item.payload["result"] == "rejected" for item in events[-3:-1])
    sse = client.get(f"/api/v1/runs/{run_id}/events").text
    assert '"command_id": "' + pending["command_id"] + '"' in sse
    assert sse.rfind('"event_type": "run.cancelled"') > sse.rfind('"event_type": "run.command"')


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_worker_terminalizes_pending_commands_before_final_event(environment, status):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    run_id = queued["run_id"]
    worker = Worker(settings, engine)
    assert worker.claim() == run_id
    assert submit(client, run_id, "pause", "pending").status_code == 202
    worker.finish(run_id, status)
    worker.consume_commands()
    with factory() as db:
        cmd = db.scalar(select(RunCommand).where(RunCommand.client_request_id == "pending"))
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)
                                 .order_by(RunEvent.sequence_number)))
        assert cmd.status == "rejected"
        assert [item.event_type for item in events][-2:] == ["run.command", f"run.{status}"]


async def test_paused_run_blocks_own_conversation_but_not_others(environment):
    settings, engine, _, client = environment
    model, conversation, first = create_chat(client)
    second = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={
        "content": "Second", "model_config_id": model["id"], "client_request_id": "request-2",
    }).json()
    other = client.post("/api/v1/conversations", json={}).json()
    third = client.post(f"/api/v1/conversations/{other['id']}/messages", json={
        "content": "Other", "model_config_id": model["id"], "client_request_id": "request-3",
    }).json()
    submit(client, first["run_id"], "pause", "p1")
    Worker(settings, engine).consume_commands()
    worker = Worker(settings, engine)
    assert await worker.run_once() == third["run_id"]
    assert await worker.run_once() is None
    assert client.get(f"/api/v1/runs/{second['run_id']}").json()["status"] == "queued"


def test_recovery_keeps_pause_requested_as_paused(environment):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    assert Worker(settings, engine).claim() == queued["run_id"]
    submit(client, queued["run_id"], "pause", "p1")
    Worker(settings, engine).consume_commands()
    with factory() as db:
        assert db.get(Run, queued["run_id"]).status == "pause_requested"
    Worker(settings, engine).recover()
    with factory() as db:
        run = db.get(Run, queued["run_id"])
        assert run.status == "paused" and run.error_code is None


async def test_cancel_paused_run_releases_its_conversation_and_is_idempotent(environment):
    settings, engine, factory, client = environment
    model, conversation, first = create_chat(client)
    second = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={
        "content": "Next", "model_config_id": model["id"], "client_request_id": "next",
    }).json()
    worker = Worker(settings, engine)
    submit(client, first["run_id"], "pause", "pause-first")
    worker.consume_commands()
    assert worker.claim() is None
    path = f"/api/v1/runs/{first['run_id']}/cancel"
    assert client.post(path).json()["status"] == "cancelled"
    assert client.post(path).json()["status"] == "cancelled"
    with factory() as db:
        run = db.get(Run, first["run_id"])
        assistant = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "assistant"))
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run.id,
                                                       RunEvent.event_type == "run.cancelled")))
        assert run.finished_at is not None and assistant.status == "cancelled"
        assert len(events) == 1
    assert await worker.run_once() == second["run_id"]


async def test_cancel_between_pause_check_and_transition_finishes_and_preserves_stream(environment):
    settings, engine, factory, client = environment
    _, conversation, queued = create_chat(client)
    reached, closed = asyncio.Event(), asyncio.Event()
    prefix = "Already generated. " * 15
    calls = []

    async def stream(config, history):
        calls.append(1)
        try:
            yield ModelDelta(text=prefix)
            reached.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    worker = Worker(settings, engine, stream_fn=stream)
    original = worker.mark_paused

    def cancel_at_boundary(identity, **kwargs):
        # This hook runs after the worker observed pause_requested and before
        # it locks the Run to mark paused.
        assert client.post(f"/api/v1/runs/{identity}/cancel").json()["status"] == "cancel_requested"
        return original(identity, **kwargs)

    worker.mark_paused = cancel_at_boundary
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(reached.wait(), timeout=3)
        assert submit(client, queued["run_id"], "pause", "pause-running").status_code == 202
        assert await asyncio.wait_for(task, timeout=5) == queued["run_id"]
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert closed.is_set() and calls == [1]
    messages = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
    assert messages[-1]["content"] == prefix and messages[-1]["status"] == "cancelled"
    with factory() as db:
        run = db.get(Run, queued["run_id"])
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run.id,
                                                       RunEvent.event_type == "run.cancelled")))
        assert run.status == "cancelled" and run.finished_at is not None and len(events) == 1


def test_recovery_handles_cancel_racing_pause_transition(environment):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    worker = Worker(settings, engine)
    assert worker.claim() == queued["run_id"]
    submit(client, queued["run_id"], "pause", "pause-before-restart")
    worker.consume_commands()
    original = worker.mark_paused

    def cancel_during_recovery(identity, **kwargs):
        assert client.post(f"/api/v1/runs/{identity}/cancel").json()["status"] == "cancel_requested"
        return original(identity, **kwargs)

    worker.mark_paused = cancel_during_recovery
    worker.recover()
    with factory() as db:
        run = db.get(Run, queued["run_id"])
        assert run.status == "cancelled" and run.finished_at is not None


async def test_resume_between_pause_check_and_transition_continues_without_duplicate_text(environment):
    settings, engine, _, client = environment
    _, conversation, queued = create_chat(client)
    reached, release = asyncio.Event(), asyncio.Event()

    async def stream(config, history):
        yield ModelDelta(text="first ")
        reached.set()
        await release.wait()
        yield ModelDelta(text="second")

    worker = Worker(settings, engine, stream_fn=stream)
    original = worker.mark_paused

    def resume_at_boundary(identity, **kwargs):
        assert submit(client, identity, "resume", "resume-race").status_code == 202
        worker.consume_commands()
        release.set()
        return original(identity, **kwargs)

    worker.mark_paused = resume_at_boundary
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(reached.wait(), timeout=3)
        assert submit(client, queued["run_id"], "pause", "pause-race").status_code == 202
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    messages = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
    assert messages[-1]["content"] == "first second" and messages[-1]["status"] == "completed"
