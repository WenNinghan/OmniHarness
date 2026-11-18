"""Opt-in isolated PostgreSQL Worker interaction; no provider calls."""
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
    INTERACTIVE_COMPAT_WORKFLOW_VERSION,
    INTERACTIVE_V9_WORKFLOW_VERSION,
    INTERACTIVE_WORKFLOW_VERSION,
    M4_V8_PLUGIN_VERSIONS,
    M4_V9_PLUGIN_VERSIONS,
    workspace_role_versions,
)
from omniharness.models import Clarification, Run
from omniharness.security import create_pairing_code
from omniharness.stage_contract import STAGE_KEYS
from omniharness.worker import Worker, WorkerLock


class QuestionToolModel(BaseChatModel):
    @property
    def _llm_type(self): return "postgres-question-test"
    def bind_tools(self, tools, **kwargs): return self
    def _generate(self, messages, **kwargs):
        tools = [message for message in messages if isinstance(message, ToolMessage)]
        if not tools:
            name, args = "workspace_read", {"path": "main.py"}
        elif len(tools) == 1:
            name, args = "workspace_patch", {"path": "main.py", "content": "print(42)\n",
                "expected_sha256": json.loads(tools[-1].content)["sha256"]}
        elif len(tools) == 2:
            name, args = "ask_user", {"question": "选哪个方案？", "reason": "需要用户选择"}
        else:
            assert json.loads(tools[-1].content)["answer"] == "选甲"
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content='{"status":"completed","content":"完成","questions":[]}'))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
            {"id": f"call-{len(tools)}", "name": name, "args": args}]))])
    async def _agenerate(self, messages, **kwargs): return self._generate(messages, **kwargs)


class InteractiveStage(V6Stage):
    async def ainvoke(self, messages):
        if getattr(messages[-1], "id", None) == "omni-interaction:preflight":
            return AIMessage(content='{"status":"continue","questions":[]}')
        if (getattr(messages[-1], "id", None) == "omni-interaction:final" or
            (getattr(messages[-1], "id", None) == "omni-stage:think" and
             "请直接回答用户原始问题" in str(messages[-1].content))):
            return AIMessage(content="已综合完成。")
        if self.stage == "review":
            packets = [json.loads(m.content)["host_evidence"] for m in messages
                       if getattr(m, "id", None) == "omni-evidence:review"]
            if packets and packets[0].get("schema_version") == "host-evidence-v2":
                target = next((item for item in packets[0]["operations"] if item["tool"] == "workspace_patch"), None)
                refs = [{"operation_id": target["operation_id"], "dimension": "execution"}] if target else []
                return AIMessage(content=json.dumps({"summary": "review", "findings": [{"finding_id": "f1",
                    "location": "main.py", "severity": "INFO", "description": "test claim", "model_status": "PASS",
                    "recommendation": "inspect", "candidate_refs": refs}], "unverified": []}))
        return await super().ainvoke(messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_version", [INTERACTIVE_WORKFLOW_VERSION, INTERACTIVE_COMPAT_WORKFLOW_VERSION,
                                               INTERACTIVE_V9_WORKFLOW_VERSION])
async def test_postgres_worker_reclaims_same_run_without_repeating_patch(postgres_environment, tmp_path, monkeypatch, workflow_version):  # noqa: F811 - shared opt-in fixture
    import traceback

    import omniharness.m3_worker as worker_module
    failures = []
    original_review = worker_module._review
    def record_review(*args, **kwargs):
        typ, _value, tb = sys.exc_info()
        if typ:
            failures.append((typ.__name__, [(frame.name, frame.lineno) for frame in traceback.extract_tb(tb)]))
        return original_review(*args, **kwargs)
    monkeypatch.setattr(worker_module, "_review", record_review)
    settings, engine, factory, _schema = postgres_environment
    settings.enable_agent = True
    settings.enable_tools = True
    settings.sandbox_key_file = tmp_path / "fake-sandbox.key"
    settings.sandbox_key_file.write_text("x" * 64, encoding="utf-8")
    settings.checkpoint_schema = "cp_v7_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=settings.checkpoint_schema)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        code = create_pairing_code(factory)
        assert client.post("/api/v1/auth/pair", json={"code": code}).status_code == 200
        worker, run_id, workspace, broker, sandbox, project = prepare_v4(
            (settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            agent = {**run.config_snapshot["agent"], "workflow_version": workflow_version,
                     "strategy": "compact"}
            if workflow_version == INTERACTIVE_COMPAT_WORKFLOW_VERSION:
                agent["plugin_versions"] = M4_V8_PLUGIN_VERSIONS
            elif workflow_version == INTERACTIVE_V9_WORKFLOW_VERSION:
                agent["plugin_versions"] = M4_V9_PLUGIN_VERSIONS
            for role in STAGE_KEYS:
                agent["roles"][role] = {**agent["roles"][role], "role_version": workspace_role_versions(workflow_version)[role]}
            run.config_snapshot = {**run.config_snapshot, "agent": agent}
            run.workflow_version = workflow_version
        def configure(target):
            target.workspace_broker, target.sandbox_executor = broker, sandbox
            target.agent_model_factory = lambda role, _config: QuestionToolModel() if role == "execute" else InteractiveStage(role)
        configure(worker)
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            await run_agent(worker, run_id)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            assert run.status == "awaiting_user", (run.error_code, failures)
            question = db.scalar(select(Clarification).where(Clarification.run_id == run_id))
            assert question.question == "选哪个方案？"
            answer_clarification(db, run_id, question.id, "reply-1", "选甲")
        assert broker.calls.count("patch") == 1
        replacement = Worker(settings, engine)
        configure(replacement)
        with WorkerLock(engine, settings) as owner:
            replacement.lock = owner
            assert replacement.claim() == run_id
            await run_agent(replacement, run_id)
        with factory() as db:
            result_run = db.get(Run, run_id)
            assert result_run.status == "completed", (result_run.error_code, result_run.error_message, failures)
        assert broker.calls.count("patch") == 1
        assert (project / "main.py").read_text() == "print(42)\n"
