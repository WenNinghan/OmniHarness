"""Real process-crash recovery with isolated Postgres, fake model and fake broker."""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_m4_collaboration import prepare_v4
from test_m4_runtime import LocalSandbox
from test_postgres import postgres_environment  # noqa: F401
from test_v7_postgres_interaction import InteractiveStage, QuestionToolModel

from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema
from omniharness.clarification_service import answer_clarification
from omniharness.db import make_engine
from omniharness.m3_worker import _checkpoint_url, run_agent
from omniharness.m4_contract import (
    INTERACTIVE_COMPAT_WORKFLOW_VERSION,
    INTERACTIVE_V9_WORKFLOW_VERSION,
    INTERACTIVE_WORKFLOW_VERSION,
    M4_V8_PLUGIN_VERSIONS,
    M4_V9_PLUGIN_VERSIONS,
    workspace_role_versions,
)
from omniharness.models import Clarification, Run, RunBudget, ToolOperation
from omniharness.security import create_pairing_code
from omniharness.settings import Settings
from omniharness.stage_contract import STAGE_KEYS
from omniharness.worker import Worker, WorkerLock
from omniharness.workspace_io import DictWorkspaceRegistry, WorkspaceGrant, WorkspaceIO

BOOT = '3c61fd2b-b6cc-49b2-af62-a35d3cb84afd'


class DurableCountingBroker:
    def __init__(self, io, log):
        self.io, self.log = io, Path(log)

    async def request(self, workspace_id, version, action, **payload):
        if action == 'patch':
            with self.log.open('a', encoding='utf-8') as stream:
                stream.write('patch\n')
                stream.flush()
                os.fsync(stream.fileno())
        return getattr(self.io, action.replace('-', '_'))(workspace_id, 'local', version, **payload)


def configure_worker(worker, workspace, project_parent):
    grant = WorkspaceGrant(**workspace['grant'])
    registry = DictWorkspaceRegistry([grant])
    io = WorkspaceIO(registry, Path(project_parent) / 'workspace-data',
                     protected_roots=(Path(project_parent) / 'framework',),
                     require_write_lease=lambda *_: None)
    worker.workspace_broker = DurableCountingBroker(io, Path(project_parent) / 'patch-count.txt')
    worker.sandbox_executor = LocalSandbox()
    worker.agent_model_factory = lambda role, _config: QuestionToolModel() if role == 'execute' else InteractiveStage(role)


def crash_worker(settings_data, workspace, project_parent, run_id, crash_before_checkpoint):
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    from omniharness import interrupt_timing, m3_worker
    interrupt_timing.current_boot_id = lambda: BOOT
    if crash_before_checkpoint:
        # The question exists, but its interrupt has not yet been handed to LangGraph.
        interrupt_timing.user_interrupt = lambda *args, **kwargs: os._exit(93)
    # This point is reached only after graph.aget_state confirms a durable interrupt.
    m3_worker._wait_for_user = lambda *args, **kwargs: os._exit(91)
    settings = Settings(_env_file=None, **settings_data)
    engine = make_engine(settings)
    worker = Worker(settings, engine)
    configure_worker(worker, workspace, project_parent)
    async def work():
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            await run_agent(worker, run_id)
    asyncio.run(work())
    os._exit(92)


@pytest.fixture(scope='module')
def event_loop_policy():
    return asyncio.WindowsSelectorEventLoopPolicy() if sys.platform == 'win32' else asyncio.get_event_loop_policy()


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['safe', 'before_checkpoint', 'boot_change', 'uncertain_tool'])
@pytest.mark.parametrize('workflow_version', [INTERACTIVE_WORKFLOW_VERSION, INTERACTIVE_COMPAT_WORKFLOW_VERSION,
                                              INTERACTIVE_V9_WORKFLOW_VERSION])
async def test_real_kill_after_checkpoint_recovers_question_once(postgres_environment, tmp_path, monkeypatch, scenario, workflow_version):  # noqa: F811 - shared opt-in fixture
    from omniharness import interrupt_timing
    monkeypatch.setattr(interrupt_timing, 'current_boot_id', lambda: BOOT)
    settings, engine, factory, _schema = postgres_environment
    settings.enable_agent = settings.enable_tools = True
    settings.sandbox_key_file = tmp_path / 'fake-sandbox.key'
    settings.sandbox_key_file.write_text('x' * 64, encoding='utf-8')
    settings.checkpoint_schema = 'cp_crash_' + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=settings.checkpoint_schema)
    with TestClient(create_app(settings, engine), headers={'X-Omni-Request': '1'}) as client:
        assert client.post('/api/v1/auth/pair', json={'code': create_pairing_code(factory)}).status_code == 200
        _worker, run_id, workspace, _broker, _sandbox, project = prepare_v4(
            (settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            agent = {**run.config_snapshot['agent'], 'workflow_version': workflow_version,
                     'strategy': 'compact'}
            if workflow_version == INTERACTIVE_COMPAT_WORKFLOW_VERSION:
                agent['plugin_versions'] = M4_V8_PLUGIN_VERSIONS
            elif workflow_version == INTERACTIVE_V9_WORKFLOW_VERSION:
                agent['plugin_versions'] = M4_V9_PLUGIN_VERSIONS
            for role in STAGE_KEYS:
                agent['roles'][role] = {**agent['roles'][role], 'role_version': workspace_role_versions(workflow_version)[role]}
            run.config_snapshot = {**run.config_snapshot, 'agent': agent}
            run.workflow_version = workflow_version
        ctx = multiprocessing.get_context('spawn')
        process = ctx.Process(target=crash_worker, args=(settings.model_dump(mode='json'), workspace, str(tmp_path), run_id,
                                                        scenario == 'before_checkpoint'))
        process.start()
        process.join(90)
        if process.is_alive():
            process.terminate()
            process.join(10)
            pytest.fail('crash-test child did not reach persisted checkpoint')
        if process.exitcode != (93 if scenario == 'before_checkpoint' else 91):
            with factory() as db:
                interrupted_run = db.get(Run, run_id)
                pytest.fail(f'crash child exited {process.exitcode}; run={interrupted_run.status}, '
                            f'error={interrupted_run.error_code}, message={interrupted_run.error_message}')
        assert (tmp_path / 'patch-count.txt').read_text().splitlines() == ['patch']
        with factory() as db:
            assert db.get(Run, run_id).status == 'running'
            assert db.get(RunBudget, run_id).active_segment_open
            assert db.scalar(select(Clarification).where(Clarification.run_id == run_id)).status == 'pending'
        if scenario == 'boot_change':
            monkeypatch.setattr(interrupt_timing, 'current_boot_id', lambda: '5c61fd2b-b6cc-49b2-af62-a35d3cb84afd')
        if scenario == 'uncertain_tool':
            with factory.begin() as db:
                op = db.scalar(select(ToolOperation).where(ToolOperation.run_id == run_id,
                                                           ToolOperation.tool_name == 'workspace_patch'))
                op.status = 'uncertain'
        replacement = Worker(settings, engine)
        configure_worker(replacement, workspace, tmp_path)
        with WorkerLock(engine, settings) as owner:
            replacement.lock = owner
            # Only this owner may reconcile the checkpoint and settle the
            # segment; a second worker cannot race the transaction.
            with pytest.raises(RuntimeError, match='Another OmniHarness worker'):
                with WorkerLock(engine, settings):
                    pytest.fail('second worker acquired the execution lock')
            await run_agent(replacement, run_id)
        if scenario != 'safe':
            with factory() as db:
                run = db.get(Run, run_id)
                assert (run.status, run.error_code) == ('pending_review', 'ACTIVE_TIME_UNCERTAIN')
                assert db.get(RunBudget, run_id).active_segment_open
            assert (tmp_path / 'patch-count.txt').read_text().splitlines() == ['patch']
            return
        with factory.begin() as db:
            run = db.get(Run, run_id)
            assert run.status == 'awaiting_user', (run.error_code, run.error_message)
            budget = db.get(RunBudget, run_id)
            assert not budget.active_segment_open
            q = db.scalar(select(Clarification).where(Clarification.run_id == run_id))
            answer_clarification(db, run_id, q.id, 'answer-after-crash', '选甲')
        with WorkerLock(engine, settings) as owner:
            replacement.lock = owner
            assert replacement.claim() == run_id
            await run_agent(replacement, run_id)
        with factory() as db:
            result_run = db.get(Run, run_id)
            assert result_run.status == 'completed', (result_run.error_code, result_run.error_message)
        assert (tmp_path / 'patch-count.txt').read_text().splitlines() == ['patch']
        assert (project / 'main.py').read_text() == 'print(42)\n'
