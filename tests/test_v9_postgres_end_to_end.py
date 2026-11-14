"""V9 clarification propagation through real Worker/checkpoints without paid models."""
import asyncio
import json
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import select
from test_m4_collaboration import prepare_v4
from test_postgres import postgres_environment  # noqa: F401
from test_v6_handoff_smoke import V6Stage

from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema
from omniharness.clarification_service import answer_clarification
from omniharness.m3_worker import _checkpoint_url, run_agent
from omniharness.m4_contract import (
    INTERACTIVE_V9_WORKFLOW_VERSION,
    M4_V9_PLUGIN_VERSIONS,
    workspace_role_versions,
)
from omniharness.models import ArtifactVersion, Clarification, Run
from omniharness.security import create_pairing_code
from omniharness.stage_contract import STAGE_KEYS
from omniharness.worker import Worker, WorkerLock


@pytest.fixture(scope='module')
def event_loop_policy():
    return asyncio.WindowsSelectorEventLoopPolicy() if sys.platform == 'win32' else asyncio.get_event_loop_policy()


@pytest.mark.asyncio
async def test_v9_preflight_and_tool_answers_reach_later_stages_after_worker_restart(postgres_environment, tmp_path):  # noqa: F811 - shared opt-in fixture
    settings, engine, factory, _ = postgres_environment
    settings.enable_agent = settings.enable_tools = True
    settings.sandbox_key_file = tmp_path / 'fake-sandbox.key'
    settings.sandbox_key_file.write_text('x' * 64, encoding='utf-8')
    settings.checkpoint_schema = 'cp_v9_' + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=settings.checkpoint_schema)
    observed = []

    def capture(kind, messages):
        text = '\n'.join(str(getattr(m, 'content', '')) for m in messages)
        observed.append((kind, text))
        return text

    class Stage(V6Stage):
        async def ainvoke(self, messages):
            last_id = getattr(messages[-1], 'id', None)
            final = last_id == 'omni-interaction:final' or (last_id == 'omni-stage:think' and
                    '请直接回答用户原始问题' in str(messages[-1].content))
            kind = 'preflight' if last_id == 'omni-interaction:preflight' else 'final' if final else self.stage
            text = capture(kind, messages)
            if kind == 'preflight':
                return AIMessage(content=json.dumps({'status': 'needs_user', 'questions': [{'question': '最喜欢的水果？'}]}, ensure_ascii=False))
            if kind in {'dispatch', 'review', 'final'}:
                assert '最喜欢的水果' in text and '苹果' in text, (kind, text)
            if kind in {'review', 'final'}:
                assert '最喜欢的颜色' in text and '蓝色' in text, (kind, text)
            if kind == 'final':
                return AIMessage(content='已综合水果、颜色与实际执行结果。')
            if kind == 'review':
                packets = [json.loads(m.content)['host_evidence'] for m in messages
                           if getattr(m, 'id', None) == 'omni-evidence:review']
                target = next((op for op in (packets[0]['operations'] if packets else [])
                               if op['tool'] == 'workspace_patch'), None)
                refs = ([{'operation_id': target['operation_id'], 'dimension': 'execution',
                          'expected_sha256': target['sha256']},
                         {'operation_id': target['operation_id'], 'dimension': 'execution'}] if target else [])
                return AIMessage(content=json.dumps({'summary': 'review', 'findings': [
                    {'finding_id': 'f1', 'location': 'main.py', 'severity': 'INFO', 'description': 'recorded work',
                     'model_status': 'PASS', 'recommendation': 'inspect', 'candidate_refs': refs}], 'unverified': []}))
            return await super().ainvoke(messages)

    class Execution(BaseChatModel):
        @property
        def _llm_type(self): return 'v9-offline-propagation-test'
        def bind_tools(self, tools, **kwargs): return self
        def _generate(self, messages, **kwargs):
            text = capture('execute', messages)
            assert '最喜欢的水果' in text and '苹果' in text
            tools = [m for m in messages if isinstance(m, ToolMessage)]
            if not tools:
                name, args = 'workspace_read', {'path': 'main.py'}
            elif len(tools) == 1:
                name, args = 'workspace_patch', {'path': 'main.py', 'content': 'print(42)\n',
                    'expected_sha256': json.loads(tools[-1].content)['sha256']}
            elif len(tools) == 2:
                name, args = 'ask_user', {'question': '最喜欢的颜色？', 'reason': '测试子图回答传递'}
            else:
                assert json.loads(tools[-1].content)['answer'] == '蓝色'
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content='{"status":"completed","content":"已完成","questions":[]}'))])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[
                {'id': f'call-{len(tools)}', 'name': name, 'args': args}]))])
        async def _agenerate(self, messages, **kwargs): return self._generate(messages, **kwargs)

    with TestClient(create_app(settings, engine), headers={'X-Omni-Request': '1'}) as client:
        assert client.post('/api/v1/auth/pair', json={'code': create_pairing_code(factory)}).status_code == 200
        worker, run_id, workspace, broker, sandbox, project = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            agent = {**run.config_snapshot['agent'], 'workflow_version': INTERACTIVE_V9_WORKFLOW_VERSION,
                     'strategy': 'staged', 'plugin_versions': M4_V9_PLUGIN_VERSIONS}
            for role in STAGE_KEYS:
                agent['roles'][role] = {**agent['roles'][role], 'role_version': workspace_role_versions(INTERACTIVE_V9_WORKFLOW_VERSION)[role]}
            run.config_snapshot = {**run.config_snapshot, 'agent': agent}
            run.workflow_version = INTERACTIVE_V9_WORKFLOW_VERSION

        def configure(target):
            target.workspace_broker, target.sandbox_executor = broker, sandbox
            target.agent_model_factory = lambda role, _config: Execution() if role == 'execute' else Stage(role)

        async def resume(target, expected_question, reply, request_id):
            configure(target)
            with WorkerLock(engine, settings) as owner:
                target.lock = owner
                if request_id != 'initial':
                    assert target.claim() == run_id
                await run_agent(target, run_id)
            if expected_question:
                with factory.begin() as db:
                    run = db.get(Run, run_id)
                    assert run.status == 'awaiting_user', (run.status, run.error_code, run.error_message)
                    question = db.scalar(select(Clarification).where(Clarification.run_id == run_id,
                                         Clarification.status == 'pending'))
                    assert question.question == expected_question
                    answer_clarification(db, run_id, question.id, request_id, reply)

        await resume(worker, '最喜欢的水果？', '苹果', 'initial')
        await resume(Worker(settings, engine), '最喜欢的颜色？', '蓝色', 'second')
        await resume(Worker(settings, engine), None, None, 'final')
        with factory() as db:
            run = db.get(Run, run_id)
            assert run.status == 'completed', (run.status, run.error_code, run.error_message)
            assert len(list(db.scalars(select(Clarification).where(Clarification.run_id == run_id)))) == 2
            report = db.scalar(select(ArtifactVersion).where(ArtifactVersion.run_id == run_id,
                               ArtifactVersion.kind == 'review_reference_report_v2'))
            assert report is not None
            report_id = report.id
        response = client.get(f'/api/v1/runs/{run_id}/artifacts/{report_id}/content')
        assert response.status_code == 200 and response.json()['workflow_version'] == 'stage-agent-v9'
        refs = response.json()['findings'][0]['references']
        assert refs[0]['reference_status'] == 'UNVERIFIED'
        assert refs[0]['reason'] == 'EXECUTION_DIGEST_WITHOUT_ARTIFACT_REFERENCE'
        from omniharness.review_contract import CandidateReferenceV9
        recovered = CandidateReferenceV9.model_validate(refs[0]['candidate'])
        assert recovered.is_unbound_execution_digest()
        assert set(refs[0]['candidate']) == {'operation_id', 'dimension', 'expected_sha256'}
        assert refs[1]['reference_status'] == 'VALIDATED'
        assert client.get(f'/api/v1/runs/{uuid4()}/artifacts/{report_id}/content').status_code == 404
        assert {'dispatch', 'execute', 'review', 'final'} <= {kind for kind, _ in observed}
        assert broker.calls.count('patch') == 1
        assert (project / 'main.py').read_text() == 'print(42)\n'
