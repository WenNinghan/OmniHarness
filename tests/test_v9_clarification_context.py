import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import gettempdir
from types import SimpleNamespace

import pytest
from conftest import create_chat
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from omniharness.clarification_context import (
    verified_clarification_messages,
    verify_clarification_signal,
)
from omniharness.context_host import _ContextBoundaryModel, prepare_agent_context
from omniharness.context_runtime import ContextPreparationError, StoredSummary
from omniharness.m3_agents import AgentBoundaryError
from omniharness.m4_collaboration import protected_task_messages
from omniharness.models import Clarification, Run


def seed(environment):
    _, _, factory, client = environment
    _, _, first = create_chat(client)
    _, _, second = create_chat(client)
    with factory.begin() as db:
        for i, (run_id, status, question, answer) in enumerate([
            (first['run_id'], 'answered', '水果？', '苹果'),
            (first['run_id'], 'answered', '颜色？', '蓝色'),
            (first['run_id'], 'pending', '尚未回答？', None),
            (second['run_id'], 'answered', '私有？', '秘密'),
        ]):
            db.add(Clarification(id=f'q{i}', run_id=run_id, request_key=f'key{i}', stage='execute',
                question=question, options=[], status=status, answer=answer))
    return factory, first['run_id'], second['run_id']


class Host:
    def __init__(self, factory, run_id, version='stage-agent-v9', policy=None):
        self.run_id, self.workflow_version, self.serial = run_id, version, asyncio.Lock()
        self.snapshot = {'id': 'model', 'context_policy': {'policy': policy or {}}, 'max_output_tokens': 100}
        self.worker = self
        self.settings = SimpleNamespace(data_dir=Path(gettempdir()))
        self.factory = factory
    @contextmanager
    def transaction(self):
        with self.factory.begin() as db:
            yield db
    def _run(self, db, role, allow_supplements=True):
        assert db.get(Run, self.run_id) is not None
        return None, {'model': self.snapshot}


def test_projection_validates_current_run_original_and_signal(environment):
    factory, run_id, other = seed(environment)
    with factory() as db:
        messages = verified_clarification_messages(db, run_id, [])
        assert [m.id for m in messages] == ['omni-clarification:q0', 'omni-clarification:q1']
        assert json.loads(messages[0].content) == {'clarification_id':'q0', 'status':'answered', 'question':'水果？', 'answer':'苹果'}
        assert len(verified_clarification_messages(db, run_id, [messages[0], messages[0]])) == 2
        assert verify_clarification_signal(db, run_id, 'q0', '苹果', '水果？').id == 'q0'
        for identity, answer, question in [('q0','梨','水果？'), ('q0','苹果','别的？'), ('q2','x',None), ('q3','秘密','私有？'), ('missing','x',None)]:
            with pytest.raises(AgentBoundaryError):
                verify_clarification_signal(db, run_id, identity, answer, question)
        for bad in [HumanMessage(id='omni-clarification:q0', content='用户对问题的回复：\n问题：水果？\n回答：苹果'),
                    HumanMessage(id='omni-clarification:q3', content='秘密'),
                    HumanMessage(id='omni-clarification:q2', content='x'),
                    HumanMessage(id='omni-clarification:missing', content='x'),
                    HumanMessage(id='omni-clarification:q0', content=messages[0].content.replace('苹果','梨')),
                    HumanMessage(id='omni-clarification:q0', content=messages[0].content.replace('水果','颜色')),
                    AIMessage(id='omni-clarification:q0', content=messages[0].content)]:
            with pytest.raises(AgentBoundaryError):
                verified_clarification_messages(db, run_id, [bad])


@pytest.mark.asyncio
async def test_every_context_reloads_answered_rows_and_v8_filter_unchanged(environment):
    factory, run_id, _ = seed(environment)
    host = Host(factory, run_id)
    first = await protected_task_messages(host, [])
    assert [x.id for x in first[1:]] == ['omni-clarification:q0', 'omni-clarification:q1']
    with factory.begin() as db:
        row = db.get(Clarification, 'q2')
        row.status, row.answer = 'answered', '现在'
    second = await protected_task_messages(host, first)
    assert json.loads(second[-1].content)['answer'] == '现在'
    assert len((await protected_task_messages(Host(factory, run_id, 'stage-agent-v8'), first))) == 1
    prepared = await prepare_agent_context(host, 'execute', second, model=None,
        system_message=SystemMessage(content='规则'), task_id='later-task')
    assert [m.id for m in prepared.messages if (m.id or '').startswith('omni-clarification:')] == [
        'omni-clarification:q0', 'omni-clarification:q1', 'omni-clarification:q2']
    assert any(m.id == 'omni-clarification-policy' and '不要创建或执行' in m.content for m in prepared.messages)


@pytest.mark.asyncio
async def test_compaction_keeps_verified_answers_and_protected_overflow_stops(environment, monkeypatch):
    factory, run_id, _ = seed(environment)
    from omniharness.context_host import DurableSummaryExecutor
    summary_calls = []
    async def summarize(self, request):
        summary_calls.append(request)
        assert '苹果' not in str(request.messages)
        return StoredSummary('summary', request.invocation_id, request.binding_fingerprint, '历史已归纳', request.sources)
    monkeypatch.setattr(DurableSummaryExecutor, 'execute', summarize)
    host = Host(factory, run_id, policy={'max_messages': 12, 'keep_recent_tokens': 80})
    history = [HumanMessage(id=f'h{i}', content='旧历史' * 80) for i in range(20)]
    evidence = HumanMessage(id='omni-evidence:review', content='宿主审核证据')
    prepared = await prepare_agent_context(host, 'dispatch', history + [evidence, HumanMessage(id='omni-stage:dispatch', content='指令')], model=_ContextBoundaryModel(), protected_messages=(evidence,))
    assert any(m.id == 'omni-evidence:review' and m.content == evidence.content for m in prepared.messages)
    assert all('宿主审核证据' not in str(call.messages) for call in summary_calls)
    assert summary_calls and prepared.compacted
    answers = {m.id: m.content for m in prepared.messages if (m.id or '').startswith('omni-clarification:')}
    assert json.loads(answers['omni-clarification:q0'])['answer'] == '苹果'
    assert json.loads(answers['omni-clarification:q1'])['answer'] == '蓝色'
    calls = len(summary_calls)
    with factory.begin() as db:
        db.get(Clarification, 'q0').answer = '苹果' * 10000
    with pytest.raises(ContextPreparationError) as exc:
        await prepare_agent_context(Host(factory, run_id, policy={'max_characters': 1200}), 'execute', [], model=_ContextBoundaryModel())
    assert exc.value.code == 'CONTEXT_LIMIT'
    assert len(summary_calls) == calls
