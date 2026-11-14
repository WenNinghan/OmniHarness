"""Message channels survive persistence, failures, cancellation and replay."""

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from conftest import create_chat
from langchain_core.messages import AIMessage
from sqlalchemy import select

from omniharness.cli import migrate
from omniharness.m3_worker import _complete_checkpoint, _finish
from omniharness.models import Message, Run, RunEvent
from omniharness.providers import ModelDelta, ProviderError
from omniharness.worker import Worker


def stored_events(factory, identity):
    with factory() as db:
        return list(db.scalars(select(RunEvent).where(RunEvent.run_id == identity)
                               .order_by(RunEvent.sequence_number)))


async def test_channels_persist_independently_and_only_answer_enters_history(environment):
    settings, engine, factory, client = environment
    model, conversation, first = create_chat(client)
    histories = []

    async def stream(config, history):
        histories.append(history)
        yield ModelDelta(reasoning="provider reasoning ")
        yield ModelDelta(text="Final ", reasoning="continued")
        yield ModelDelta(text="answer.")
        yield ModelDelta(usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30, "cost": None},
                         finish_reason="stop", output_status="complete")

    worker = Worker(settings, engine, stream_fn=stream)
    await worker.run_once()
    path = f"/api/v1/conversations/{conversation['id']}/messages"
    message = client.get(path).json()[-1]
    assert message["content"] == "Final answer."
    assert message["reasoning_content"] == "provider reasoning continued"
    assert message["output_metadata"] == {"finish_reason": "stop", "output_status": "complete"}
    assert message["error_code"] is None and message["status"] == "completed"
    emitted = stored_events(factory, first["run_id"])
    assert "".join(e.payload["delta"] for e in emitted if e.event_type == "message.delta") == message["content"]
    assert "".join(e.payload["delta"] for e in emitted if e.event_type == "message.reasoning_delta") == message["reasoning_content"]
    assert [e.sequence_number for e in emitted] == list(range(1, len(emitted) + 1))
    assert emitted[-1].payload["output_metadata"]["output_status"] == "complete"
    client.post(path, json={"content": "Follow up", "model_config_id": model["id"], "client_request_id": "second"})
    await worker.run_once()
    assert histories[-1][1] == {"role": "assistant", "content": "Final answer."}
    assert all("reasoning" not in item["content"] for item in histories[-1])


async def test_truncation_keeps_both_channels_and_historical_message_error(environment):
    settings, engine, factory, client = environment
    model, conversation, first = create_chat(client)
    seen = []

    async def truncated(config, history):
        yield ModelDelta(reasoning="thinking fragment", text="partial answer")
        yield ModelDelta(finish_reason="length", output_status="truncated")
        raise ProviderError("OUTPUT_TRUNCATED", "输出已达到上限。")

    await Worker(settings, engine, stream_fn=truncated).run_once()
    path = f"/api/v1/conversations/{conversation['id']}/messages"
    first_message = client.get(path).json()[-1]
    assert first_message["status"] == "failed"
    assert first_message["content"] == "partial answer"
    assert first_message["reasoning_content"] == "thinking fragment"
    assert first_message["error_code"] == "OUTPUT_TRUNCATED"
    assert first_message["output_metadata"]["output_status"] == "truncated"
    assert "run.completed" not in [e.event_type for e in stored_events(factory, first["run_id"])]

    async def complete(config, history):
        seen.extend(history)
        yield ModelDelta(text="New complete answer", finish_reason="stop", output_status="complete")

    client.post(path, json={"content": "Follow up", "model_config_id": model["id"], "client_request_id": "second"})
    await Worker(settings, engine, stream_fn=complete).run_once()
    messages = client.get(path).json()
    assert messages[1]["error_code"] == "OUTPUT_TRUNCATED"
    assert messages[0]["error_code"] is None
    assert messages[-1]["error_code"] is None
    assert not any(item["role"] == "assistant" for item in seen)


async def test_reasoning_only_cancellation_flushes_small_buffer_and_closes_stream(environment):
    settings, engine, _, client = environment
    _, conversation, queued = create_chat(client)
    waiting, closed = asyncio.Event(), asyncio.Event()

    async def stream(config, history):
        try:
            yield ModelDelta(reasoning="small reasoning buffer")
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(Worker(settings, engine, stream_fn=stream).run_once())
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        client.post(f"/api/v1/runs/{queued['run_id']}/cancel")
        await asyncio.wait_for(task, 3)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    message = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()[-1]
    assert closed.is_set()
    assert message["content"] == ""
    assert message["reasoning_content"] == "small reasoning buffer"
    assert message["status"] == "cancelled"
    assert message["output_metadata"]["output_status"] == "cancelled"


async def test_reasoning_only_pause_retains_usage_and_channels(environment):
    settings, engine, _, client = environment
    _, conversation, queued = create_chat(client)
    waiting = asyncio.Event()

    async def stream(config, history):
        yield ModelDelta(reasoning="unfinished thought", usage={"output_tokens": 5, "cost": None})
        waiting.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(Worker(settings, engine, stream_fn=stream).run_once())
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        response = client.post(f"/api/v1/runs/{queued['run_id']}/commands", json={
            "command_type": "pause", "client_request_id": "pause-reasoning",
        })
        assert response.status_code == 202
        await asyncio.wait_for(task, 3)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    message = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()[-1]
    run = client.get(f"/api/v1/runs/{queued['run_id']}").json()
    assert message["status"] == "paused" and message["reasoning_content"] == "unfinished thought"
    assert message["output_metadata"]["output_status"] == "paused"
    assert run["usage"]["output_tokens"] == 5


async def test_stream_error_after_buffered_reasoning_retains_content_and_redacts_exception(environment):
    settings, engine, _, client = environment
    _, conversation, queued = create_chat(client)

    async def stream(config, history):
        yield ModelDelta(reasoning="private provider thought", text="partial")
        raise RuntimeError("Bearer secret-not-for-display")

    await Worker(settings, engine, stream_fn=stream).run_once()
    response = client.get(f"/api/v1/conversations/{conversation['id']}/messages")
    message = response.json()[-1]
    assert message["content"] == "partial" and message["reasoning_content"] == "private provider thought"
    assert message["status"] == "failed" and message["error_code"] == "EXECUTION_ERROR"
    assert "secret-not-for-display" not in response.text
    assert "secret-not-for-display" not in client.get(f"/api/v1/runs/{queued['run_id']}/events").text


@pytest.mark.parametrize("reasoning,code", [("thought without answer", "REASONING_ONLY"), ("", "EMPTY_RESPONSE")])
async def test_worker_never_completes_a_stream_without_an_answer(environment, reasoning, code):
    settings, engine, _, client = environment
    _, conversation, _ = create_chat(client)

    async def stream(config, history):
        yield ModelDelta(reasoning=reasoning)

    await Worker(settings, engine, stream_fn=stream).run_once()
    message = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()[-1]
    assert message["status"] == "failed" and message["error_code"] == code
    assert message["reasoning_content"] == reasoning and message["content"] == ""


def test_checkpoint_final_output_checks_finish_reason_and_retains_partial(environment):
    settings, engine, _, client = environment
    _, conversation, queued = create_chat(client)
    worker = Worker(settings, engine)
    assert worker.claim() == queued["run_id"]
    output = AIMessage(content="unfinished final", additional_kwargs={"reasoning_content": "provider thought"},
                       response_metadata={"finish_reason": "length", "omni_require_finish": True})
    with pytest.raises(ProviderError) as error:
        _complete_checkpoint(worker, queued["run_id"], {"messages": [output]}, started=None)
    # A recovered committed checkpoint must not append the same partial answer.
    with pytest.raises(ProviderError):
        _complete_checkpoint(worker, queued["run_id"], {"messages": [output]}, started=None)
    _finish(worker, queued["run_id"], "failed", error.value.code, error.value.public_message)
    message = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()[-1]
    assert message["status"] == "failed" and message["error_code"] == "OUTPUT_TRUNCATED"
    assert message["content"] == "unfinished final" and message["reasoning_content"] == "provider thought"
    assert message["output_metadata"] == {"finish_reason": "length", "output_status": "truncated"}


def test_complete_checkpoint_preserves_reasoning_with_idempotent_publication(environment):
    settings, engine, factory, client = environment
    _, conversation, queued = create_chat(client)
    worker = Worker(settings, engine)
    assert worker.claim() == queued["run_id"]
    output = AIMessage(content="Final answer", additional_kwargs={"reasoning_content": "provider thought"},
                       response_metadata={"finish_reason": "stop", "omni_require_finish": True})
    _complete_checkpoint(worker, queued["run_id"], {"messages": [output]}, started=None)
    _complete_checkpoint(worker, queued["run_id"], {"messages": [output]}, started=None)
    message = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()[-1]
    assert message["status"] == "completed" and message["content"] == "Final answer"
    assert message["reasoning_content"] == "provider thought"
    emitted = stored_events(factory, queued["run_id"])
    assert sum(e.event_type == "message.reasoning_delta" for e in emitted) == 1
    assert sum(e.event_type == "run.completed" for e in emitted) == 1


def test_upgrade_preserves_legacy_answer_without_fabricating_channels(environment):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    with factory.begin() as db:
        message = db.scalar(select(Message).where(Message.run_id == queued["run_id"], Message.role == "assistant"))
        message.content, message.status = "legacy answer", "completed"
        db.get(Run, queued["run_id"]).status = "completed"
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "src/omniharness/migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.downgrade(config, "0007")
    migrate(settings)
    migrate(settings)
    with factory() as db:
        message = db.scalar(select(Message).where(Message.run_id == queued["run_id"], Message.role == "assistant"))
        assert message.content == "legacy answer" and message.status == "completed"
        assert message.reasoning_content == "" and message.output_metadata == {}
