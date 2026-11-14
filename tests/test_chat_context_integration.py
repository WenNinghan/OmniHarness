import asyncio

import pytest
from conftest import create_chat
from langchain_core.messages import AIMessage
from sqlalchemy import select

from omniharness.models import ArtifactVersion, Message, ModelConfig, OperationRecord, Run, RunBudget
from omniharness.providers import ModelDelta
from omniharness.worker import Worker


async def prepared(environment):
    settings, engine, factory, client = environment
    model, conversation, first = create_chat(client)

    async def old_reply(config, history):
        yield ModelDelta(text='Historical project details. ' * 70)

    await Worker(settings, engine, stream_fn=old_reply).run_once()
    for index in range(3):
        response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={
            'content': 'Another historical request. ' * 70, 'model_config_id': model['id'],
            'client_request_id': f'history-{index}',
        })
        assert response.status_code == 202
        await Worker(settings, engine, stream_fn=old_reply).run_once()
    with factory.begin() as db:
        db.get(ModelConfig, model['id']).context_policy = {'policy': {
            'input_target_tokens': 5000, 'summary_output_tokens': 400,
            'keep_recent_tokens': 200, 'window_margin_tokens': 128,
        }}
    response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={
        'content': 'KEEP-CURRENT-GOAL', 'model_config_id': model['id'], 'client_request_id': 'context-followup',
    })
    assert response.status_code == 202
    return response.json()['run_id'], first['run_id']


async def test_chat_compaction_counts_summaries_and_preserves_source_history(environment):
    settings, engine, factory, client = environment
    run_id, previous = await prepared(environment)
    histories, summaries = [], []

    class Summary:
        async def ainvoke(self, messages, **kwargs):
            summaries.append(messages)
            return AIMessage(content='Public project summary.', response_metadata={'finish_reason': 'stop'},
                             usage_metadata={'input_tokens': 80, 'output_tokens': 8, 'total_tokens': 88})

    async def stream(config, history):
        histories.append(history)
        yield ModelDelta(text='Finished from compressed history.')
        yield ModelDelta(usage={'input_tokens': 50, 'output_tokens': 10, 'total_tokens': 60})

    worker = Worker(settings, engine, stream_fn=stream)
    worker.summary_model_factory = lambda config: Summary()
    assert await worker.run_once() == run_id
    with factory() as db:
        run = db.get(Run, run_id)
        assert run.status == 'completed', (run.error_code, run.error_message)
    assert summaries and len(histories) == 1
    assert histories[0][-1] == {'role': 'user', 'content': 'KEEP-CURRENT-GOAL'}
    assert sum(len(item['content']) for item in histories[0]) < 5000
    with factory() as db:
        run = db.get(Run, run_id)
        assert run.status == 'completed', run.error_code
        assert run.usage['total_tokens'] == 60 + 88 * len(summaries)
        assert run.usage['cost_usd'] is None
        assert db.get(RunBudget, run_id).model_calls == 1 + len(summaries)
        assert len(list(db.scalars(select(ArtifactVersion).where(ArtifactVersion.run_id == run_id)))) == len(summaries)
        old = db.scalar(select(Message).where(Message.run_id == previous, Message.role == 'assistant')
                        .order_by(Message.created_at))
        assert old.content == 'Historical project details. ' * 70


async def test_cancel_during_summary_stops_main_call_and_keeps_unknown_ledger(environment):
    settings, engine, factory, client = environment
    run_id, _ = await prepared(environment)
    entered, closed = asyncio.Event(), asyncio.Event()
    main_calls = []

    class Summary:
        async def ainvoke(self, messages, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    async def stream(config, history):
        main_calls.append(history)
        yield ModelDelta(text='must not run')

    worker = Worker(settings, engine, stream_fn=stream)
    worker.summary_model_factory = lambda config: Summary()
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert client.post(f'/api/v1/runs/{run_id}/cancel').status_code == 200
        assert await asyncio.wait_for(task, 5) == run_id
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert closed.is_set() and not main_calls
    with factory() as db:
        assert db.get(Run, run_id).status == 'cancelled'
        assert db.scalar(select(OperationRecord.status).where(OperationRecord.run_id == run_id)) == 'uncertain'
        assert not list(db.scalars(select(ArtifactVersion).where(ArtifactVersion.run_id == run_id)))
