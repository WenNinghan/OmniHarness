import asyncio
import json

import pytest
from conftest import create_chat
from sqlalchemy import select

from omniharness.models import Message, Run, RunEvent
from omniharness.providers import ModelDelta
from omniharness.worker import Worker, WorkerLock, WorkerOwnershipLost


def events(factory, run_id):
    with factory() as db:
        return [row.event_type for row in db.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence_number)
        )]


def test_sealed_ticket_cleanup_requires_committed_terminal_state(environment, monkeypatch):
    from omniharness import m4_services
    from omniharness.m3_worker import _release_terminal_run

    settings, engine, factory, client = environment
    identities = [create_chat(client)[2]["run_id"] for _ in range(4)]
    with factory.begin() as db:
        for identity, status in zip(identities, ("completed", "cancelled", "awaiting_user", "pending_review"), strict=True):
            db.get(Run, identity).status = status
    worker = Worker(settings, engine)
    worker._v10_sealed_tickets = dict.fromkeys(identities, object())
    for identity in identities:
        _release_terminal_run(worker, identity)
    assert set(worker._v10_sealed_tickets) == set(identities[2:])
    with factory.begin() as db:
        db.get(Run, identities[2]).status = "completed"
    def fail_transaction(*_args):
        raise RuntimeError("synthetic transaction failure")
    monkeypatch.setattr(m4_services, "release_workspace_lease", fail_transaction)
    with pytest.raises(RuntimeError):
        _release_terminal_run(worker, identities[2])
    assert identities[2] in worker._v10_sealed_tickets
    monkeypatch.setattr(m4_services, "release_workspace_lease", lambda *_args: False)
    _release_terminal_run(worker, identities[2])
    assert identities[2] in worker._v10_sealed_tickets


async def test_correction_cleanup_waits_through_repeated_cancellation():
    from omniharness.m3_worker import _cancel_correction_task

    entered, release = asyncio.Event(), asyncio.Event()
    async def model():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            entered.set()
            await release.wait()
    task = asyncio.create_task(model())
    await asyncio.sleep(0)
    cleanup = asyncio.create_task(_cancel_correction_task(task))
    await asyncio.wait_for(entered.wait(), timeout=2)
    cleanup.cancel()
    await asyncio.sleep(0)
    cleanup.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert task.done()


async def test_queue_order_keeps_future_messages_out_of_provider_history(environment):
    settings, engine, _, client = environment
    model, conversation, first = create_chat(client)
    path = f"/api/v1/conversations/{conversation['id']}/messages"
    second = client.post(path, json={
        "content": "Follow up", "model_config_id": model["id"], "client_request_id": "request-2",
    }).json()
    histories = []

    async def stream(config, history):
        histories.append(history)
        yield ModelDelta(text=f"Answer {len(histories)}")
        yield ModelDelta(usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5, "cost": None})

    worker = Worker(settings, engine, stream_fn=stream)
    assert await worker.run_once() == first["run_id"]
    assert await worker.run_once() == second["run_id"]
    assert await worker.run_once() is None
    assert histories == [
        [{"role": "user", "content": "Explain a checkpoint"}],
        [
            {"role": "user", "content": "Explain a checkpoint"},
            {"role": "assistant", "content": "Answer 1"},
            {"role": "user", "content": "Follow up"},
        ],
    ]
    messages = client.get(path).json()
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert [message["content"] for message in messages] == [
        "Explain a checkpoint", "Answer 1", "Follow up", "Answer 2",
    ]
    assert all(message["status"] == "completed" for message in messages)
    run = client.get(f"/api/v1/runs/{second['run_id']}").json()
    assert run["usage"]["cost"] is None


async def test_live_run_uses_frozen_model_and_credential_snapshot(environment):
    settings, engine, _, client = environment
    old_key, new_key = "test-only-old-key", "test-only-new-key"
    model, _, queued = create_chat(client, adapter="openai", api_key=old_key)
    response = client.put(f"/api/v1/model-configs/{model['id']}", json={
        "name": "Changed", "adapter_type": "openai", "model_name": "new-model", "api_key": new_key,
    })
    assert response.status_code == 200
    received = []

    async def stream(config, history):
        received.append((config.model_name, config.api_key))
        yield ModelDelta(text="Completed with original snapshot")

    await Worker(settings, engine, stream_fn=stream).run_once()
    assert received == [("demo", old_key)]
    response = client.get(f"/api/v1/runs/{queued['run_id']}")
    assert response.json()["status"] == "completed"
    assert response.json()["mode"] == "live"
    assert old_key not in response.text and new_key not in response.text


async def test_disabling_provider_blocks_previously_queued_request(environment):
    settings, engine, factory, client = environment
    model, _, queued = create_chat(client, adapter="openai", api_key="test-only-no-call-key")
    assert client.delete(f"/api/v1/model-configs/{model['id']}").status_code == 204
    calls = []

    async def stream(config, history):
        calls.append(config.id)
        yield ModelDelta(text="This must never run")

    assert await Worker(settings, engine, stream_fn=stream).run_once() == queued["run_id"]
    assert calls == []
    run = client.get(f"/api/v1/runs/{queued['run_id']}").json()
    assert run["status"] == "failed" and run["error_code"] == "MODEL_DISABLED"
    assert events(factory, queued["run_id"]) == ["run.created", "run.started", "run.failed"]


async def test_cancel_inflight_closes_stream_and_keeps_persisted_text(environment):
    settings, engine, factory, client = environment
    _, conversation, queued = create_chat(client)
    paused, closed = asyncio.Event(), asyncio.Event()
    prefix = "Saved partial response. " * 12

    async def stream(config, history):
        try:
            yield ModelDelta(text=prefix)
            paused.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    worker_task = asyncio.create_task(Worker(settings, engine, stream_fn=stream).run_once())
    try:
        await asyncio.wait_for(paused.wait(), timeout=3)
        result = client.post(f"/api/v1/runs/{queued['run_id']}/cancel")
        assert result.json()["status"] == "cancel_requested"
        assert await asyncio.wait_for(worker_task, timeout=3) == queued["run_id"]
    finally:
        if not worker_task.done():
            worker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker_task
    assert closed.is_set()
    run = client.get(f"/api/v1/runs/{queued['run_id']}").json()
    assert run["status"] == "cancelled"
    messages = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
    assert messages[-1]["content"] == prefix
    assert messages[-1]["status"] == "cancelled"
    assert events(factory, queued["run_id"])[-1] == "run.cancelled"
    assert "run.completed" not in events(factory, queued["run_id"])


def test_worker_recovery_marks_interrupted_without_replaying_or_losing_text(environment):
    settings, engine, factory, client = environment
    model, conversation, queued = create_chat(client)
    second = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={
        "content": "Remain queued", "model_config_id": model["id"], "client_request_id": "request-2",
    }).json()
    original_worker = Worker(settings, engine)
    assert original_worker.claim() == queued["run_id"]
    original_worker.append(queued["run_id"], "Already persisted")
    recovered_worker = Worker(settings, engine)
    recovered_worker.recover()
    recovered_worker.recover()
    with factory() as db:
        run = db.get(Run, queued["run_id"])
        assert run.status == "interrupted" and run.error_code == "WORKER_RESTARTED"
        assert db.get(Run, second["run_id"]).status == "queued"
        assistant = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "assistant"))
        assert assistant.content == "Already persisted" and assistant.status == "interrupted"
    assert events(factory, queued["run_id"]).count("run.interrupted") == 1


async def test_unexpected_provider_exception_is_redacted_from_run_and_events(environment):
    settings, engine, _, client = environment
    _, _, queued = create_chat(client)
    secret = "test-only-secret-in-upstream-exception"

    async def stream(config, history):
        raise RuntimeError(f"Authorization: Bearer {secret}")
        yield ModelDelta()  # pragma: no cover

    await Worker(settings, engine, stream_fn=stream).run_once()
    path = f"/api/v1/runs/{queued['run_id']}"
    run_response = client.get(path)
    assert run_response.json()["status"] == "failed"
    assert run_response.json()["error_code"] == "EXECUTION_ERROR"
    assert secret not in run_response.text
    assert secret not in client.get(f"{path}/events").text


async def test_mock_run_and_persisted_events_are_explicitly_marked(environment):
    settings, engine, _, client = environment
    _, conversation, queued = create_chat(client)
    await Worker(settings, engine).run_once()
    run = client.get(f"/api/v1/runs/{queued['run_id']}").json()
    assert run["status"] == "completed" and run["mode"] == "mock"
    response = client.get(f"/api/v1/runs/{queued['run_id']}/events")
    emitted = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert all(event["payload"]["mode"] == "mock" for event in emitted)
    assistant = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()[-1]
    assert "模拟" in assistant["content"]
    assert run["usage"] is None or run["usage"].get("cost") is None


def test_second_local_worker_is_rejected(environment):
    settings, engine, _, _ = environment
    with WorkerLock(engine, settings):
        with pytest.raises(RuntimeError, match="Another local development worker"):
            with WorkerLock(engine, settings):
                pass


def test_closed_local_worker_lock_cannot_claim_or_update(environment):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    with WorkerLock(engine, settings) as lock:
        worker = Worker(settings, engine)
        worker.lock = lock
        lock.file.close()
        with pytest.raises(WorkerOwnershipLost):
            worker.claim()
        with pytest.raises(WorkerOwnershipLost):
            worker.finish(queued["run_id"], "failed", "STALE", "must not write")
        with factory() as db:
            assert db.get(Run, queued["run_id"]).status == "queued"
