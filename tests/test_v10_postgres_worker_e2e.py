"""Isolated PostgreSQL, real Worker/checkpoints and deterministic offline models."""
# ruff: noqa: F811
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import select
from test_m4_collaboration import prepare_v4
from test_postgres import postgres_environment  # noqa: F401
from test_v7_postgres_interaction import InteractiveStage

from omniharness.action_contract import validate_explicit_actions
from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema
from omniharness.clarification_service import answer_clarification
from omniharness.m3_worker import _checkpoint_url, run_agent
from omniharness.m4_contract import (
    INTERACTIVE_V10_WORKFLOW_VERSION,
    M4_V10_PLUGIN_VERSIONS,
    workspace_role_versions,
)
from omniharness.models import Clarification, Run, ToolOperation
from omniharness.security import create_pairing_code
from omniharness.stage_contract import STAGE_KEYS
from omniharness.worker import Worker, WorkerLock


class PreflightQuestion(InteractiveStage):
    async def ainvoke(self, messages):
        if getattr(messages[-1], "id", None) == "omni-interaction:preflight":
            return AIMessage(content=json.dumps({"status": "needs_user", "questions": [{"question": "确认继续？"}]}, ensure_ascii=False))
        return await super().ainvoke(messages)


class NoToolsModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "v10-offline-no-tools"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content='{"status":"completed","content":"执行者声称完成","questions":[]}',
            response_metadata={"finish_reason": "stop"}))])

    async def _agenerate(self, messages, **kwargs):
        return self._generate(messages, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(("sealed", "same_owner"), [(False, False), (True, False), (True, True)])
async def test_v10_worker_restart_preserves_unverified_missing_actions(postgres_environment, tmp_path, sealed, same_owner, monkeypatch):
    import traceback

    from omniharness import m3_worker as worker_module
    captured = []
    original_review = worker_module._review
    def capture_review(*args, **kwargs):
        captured.append(traceback.format_exc())
        return original_review(*args, **kwargs)
    monkeypatch.setattr(worker_module, "_review", capture_review)
    settings, engine, factory, _ = postgres_environment
    settings.enable_agent = settings.enable_tools = True
    settings.sandbox_key_file = tmp_path / "fake-sandbox.key"
    settings.sandbox_key_file.write_text("x" * 64, encoding="utf-8")
    settings.checkpoint_schema = "cp_v10_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=settings.checkpoint_schema)

    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, workspace, broker, sandbox, project = prepare_v4((settings, engine, factory, client), tmp_path)
        requirements = [{"kind": "workspace_file_write", "path": "result.txt"},
                        {"kind": "sandbox_command", "argv": ["pytest", "-q"], "cwd": "."}]
        with factory.begin() as db:
            run = db.get(Run, run_id)
            agent = {**run.config_snapshot["agent"], "workflow_version": INTERACTIVE_V10_WORKFLOW_VERSION,
                     "strategy": "staged", "plugin_versions": M4_V10_PLUGIN_VERSIONS}
            if sealed:
                from omniharness.v10_sealed_saver import SEALED_PLUGIN_VERSIONS, create_seal_origin
                agent["plugin_versions"] = SEALED_PLUGIN_VERSIONS
            for role in STAGE_KEYS:
                agent["roles"][role] = {**agent["roles"][role],
                                        "role_version": workspace_role_versions(INTERACTIVE_V10_WORKFLOW_VERSION)[role]}
            envelope = validate_explicit_actions(requirements, run_id=run_id, workspace_id=run.workspace_id)
            run.config_snapshot = {**run.config_snapshot, "agent": agent, "action_contract": {
                "request": {"requirements": requirements, "auto_correction_enabled": False},
                "explicit": envelope, "version": 1, "revisions": [], "auto_correction_enabled": False}}
            if sealed:
                run.config_snapshot = {**run.config_snapshot,
                    "v10_checkpoint_seal_origin": create_seal_origin(settings, run.id, run.workspace_id)}
            run.workflow_version = INTERACTIVE_V10_WORKFLOW_VERSION

        def configure(target):
            target.workspace_broker, target.sandbox_executor = broker, sandbox
            target.agent_model_factory = lambda role, _config: NoToolsModel() if role == "execute" else PreflightQuestion(role)

        configure(worker)
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            await run_agent(worker, run_id)
            if sealed:
                assert run_id in worker._v10_sealed_tickets
            if same_owner:
                with factory.begin() as db:
                    run = db.get(Run, run_id)
                    assert run.status == "awaiting_user", (run.status, run.error_code, run.error_message)
                    question = db.scalar(select(Clarification).where(
                        Clarification.run_id == run_id, Clarification.status == "pending"))
                    assert question is not None
                    answer_clarification(db, run_id, question.id, "resume-1", "继续")
                assert worker.claim() == run_id
                await run_agent(worker, run_id)
                assert run_id not in worker._v10_sealed_tickets
        if not same_owner:
            with factory.begin() as db:
                run = db.get(Run, run_id)
                assert run.status == "awaiting_user", (run.status, run.error_code, run.error_message)
                question = db.scalar(select(Clarification).where(Clarification.run_id == run_id, Clarification.status == "pending"))
                assert question is not None
                answer_clarification(db, run_id, question.id, "resume-1", "继续")
            replacement = Worker(settings, engine)
            configure(replacement)
            with WorkerLock(engine, settings) as owner:
                replacement.lock = owner
                assert replacement.claim() == run_id
                await run_agent(replacement, run_id)

        with factory() as db:
            run = db.get(Run, run_id)
            if sealed and not same_owner:
                assert run.status == "pending_review", (run.status, run.error_code, run.error_message)
                assert not list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
                assert not (project / "result.txt").exists()
                assert "patch" not in broker.calls and not sandbox.calls
                return
            if run.status != "completed" and captured:
                print(captured[-1])
            assert run.status == "completed", (run.status, run.error_code, run.error_message, captured)
            assert {item.get("task_id") for item in run.outcome["v10_actions"]["actions"]} == {"t1"}
            assert list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id))) == []
        first = client.get(f"/api/v1/runs/{run_id}")
        second = client.get(f"/api/v1/runs/{run_id}")
        assert first.status_code == second.status_code == 200
        assert first.json()["action_outcome"] == second.json()["action_outcome"]
        assert first.json()["action_outcome"]["status"] == "UNVERIFIED"
        assert len(first.json()["action_outcome"]["actions"]) == 2
        assert all(item["status"] == "UNVERIFIED" for item in first.json()["action_outcome"]["actions"])
        assert first.json()["action_contract"]["auto_correction_enabled"] is False
        assert not (project / "result.txt").exists()
        assert "patch" not in broker.calls and not sandbox.calls
