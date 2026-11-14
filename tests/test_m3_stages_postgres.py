"""Dedicated database tests; no production services or external models are used."""
import asyncio
import os
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.engine import make_url
from test_postgres import postgres_environment  # noqa: F401

import omniharness.m3_worker as agent_worker
import omniharness.services as services
from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema
from omniharness.models import AgentRun, Message, Run, RunBudget, RunEvent
from omniharness.schemas import RunCommandRequest
from omniharness.security import create_pairing_code
from omniharness.services import submit_command
from omniharness.stage_contract import STAGE_KEYS, STAGE_WORKFLOW_VERSION
from omniharness.worker import Worker, WorkerLock


@pytest.fixture
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


@pytest.mark.asyncio
async def test_real_worker_uses_five_frozen_model_selections(request, monkeypatch):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    raw = os.environ["OMNI_TEST_POSTGRES_URL"]
    if make_url(raw).password is not None:
        pytest.fail("Use a password-free test URL and PGPASSWORD", pytrace=False)
    settings.enable_agent = True
    settings.checkpoint_schema = f"stages_{uuid4().hex}"
    await initialize_checkpoint_schema(raw, schema=settings.checkpoint_schema)
    monkeypatch.setattr(services, "AGENT_RUNTIME_WIRED", True)
    observed = []
    observed_inputs = []
    errors = []
    original_review = agent_worker._review
    def capture_review(*args, **kwargs):
        import traceback
        errors.append("".join(traceback.format_exception(*sys.exc_info())))
        return original_review(*args, **kwargs)
    monkeypatch.setattr(agent_worker, "_review", capture_review)
    replies = {"think": "理解任务", "plan": '{"steps":[{"id":"s1","task":"回答1","acceptance":"清楚"},{"id":"s2","task":"回答2","acceptance":"清楚"},{"id":"s3","task":"汇总","acceptance":"清楚","depends_on":["s1","s2"]}]}',
               "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"回答1"},{"id":"t2","step_id":"s2","instruction":"回答2"},{"id":"t3","step_id":"s3","instruction":"汇总","depends_on":["t1","t2"]}]}',
               "execute": "答案 42", "review": '{"summary":"尚未通过外部工具验证","findings":[],"unverified":["未运行测试"]}'}

    class Model:
        def __init__(self, stage, config):
            self.stage, self.model_id = stage, config.model_name
        async def ainvoke(self, messages):
            observed.append((self.stage, self.model_id))
            observed_inputs.append((self.stage, [m.content for m in messages]))
            return AIMessage(content=replies[self.stage])

    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        selected = {}
        for stage in STAGE_KEYS:
            created = client.post("/api/v1/model-configs", json={"name": f"model-{stage}",
                "adapter_type": "openai-compatible", "model_name": f"model-{stage}",
                "base_url": "http://127.0.0.1:11434/v1"})
            assert created.status_code == 201
            selected[stage] = created.json()["id"]
        current = client.get("/api/v1/stage-bindings").json()
        response = client.put("/api/v1/stage-bindings", json={"expected_version": current["version"],
            "bindings": [{"stage": stage, "model_config_id": selected[stage]} for stage in STAGE_KEYS]})
        assert response.status_code == 200, response.text
        conversation = client.post("/api/v1/conversations", json={}).json()
        response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={
            "content": "做一个小任务", "model_config_id": selected["think"],
            "client_request_id": "five-stage-test", "workflow_type": "agent"})
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        worker = Worker(settings, engine)
        worker.agent_model_factory = Model
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert worker.claim() == run_id
            with factory.begin() as db:
                submit_command(db, run_id, RunCommandRequest(command_type="pause", client_request_id="pause"))
            await worker.process(run_id)
            with factory() as db:
                assert db.get(Run, run_id).status == "paused"
                assert not observed
            with factory.begin() as db:
                submit_command(db, run_id, RunCommandRequest(command_type="append_requirement",
                    client_request_id="more", content="请额外说明局限"))
                submit_command(db, run_id, RunCommandRequest(command_type="resume", client_request_id="resume"))
            assert await worker.run_once() == run_id
        with factory() as db:
            run, budget = db.get(Run, run_id), db.get(RunBudget, run_id)
            assert run.workflow_version == STAGE_WORKFLOW_VERSION
            assert (run.status, run.error_code) == ("completed", None), (observed, errors)
            assert budget.model_calls == 7
            assert observed[:3] == [(stage, f"model-{stage}") for stage in ("think", "plan", "dispatch")]
            assert observed[3:6] == [("execute", "model-execute")] * 3
            assert observed[-1] == ("review", "model-review")
            assert any("请额外说明局限" in item for item in observed_inputs[0][1])
            nodes = list(db.scalars(select(AgentRun).where(AgentRun.run_id == run_id)))
            assert {node.role: node.model_config_id for node in nodes} == selected
            assert all(node.status == "completed" for node in nodes)
            assert len(list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id,
                                RunEvent.event_type == "agent.model_completed")))) == 7
            results = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id,
                                RunEvent.event_type == "agent.stage_result")))
            keys = [(event.payload["stage"], event.payload["task_id"]) for event in results]
            assert len(keys) == len(set(keys)) == 8
            assert set(keys) == {(stage, None) for stage in STAGE_KEYS} | {
                ("execute", "t1"), ("execute", "t2"), ("execute", "t3")}
            answer = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
            assert "答案 42" in answer.content and "独立审查" in answer.content


@pytest.mark.asyncio
async def test_parallel_failure_is_persisted_without_replaying_any_call(request, monkeypatch):
    from test_m3_product_runtime import configured

    from omniharness.models import OperationRecord

    stage_freeze = services.freeze_stage_run_config
    settings, engine, factory, client, worker, run_id, _ = await configured(request, monkeypatch)
    monkeypatch.setattr(services, "freeze_stage_run_config", stage_freeze)
    # The shared helper explicitly builds a legacy Run; re-freeze this fresh,
    # isolated test Run as the stage workflow before it has been claimed.
    with factory.begin() as db:
        run = db.get(Run, run_id)
        db.delete(db.get(RunBudget, run_id))
        db.flush()
        run.workflow_version, run.thread_id = None, None
        services.freeze_stage_run_config(db, run, settings)
    calls = []
    errors = []
    original_review = agent_worker._review
    def capture_review(*args, **kwargs):
        import traceback
        errors.append("".join(traceback.format_exception(*sys.exc_info())))
        return original_review(*args, **kwargs)
    monkeypatch.setattr(agent_worker, "_review", capture_review)
    replies = {"think": "ok",
        "plan": '{"steps":[{"id":"s1","task":"a","acceptance":"ok"},{"id":"s2","task":"b","acceptance":"ok"}]}',
        "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"a"},{"id":"t2","step_id":"s2","instruction":"b"}]}'}
    class Model:
        def __init__(self, stage, _config):
            self.stage = stage
        async def ainvoke(self, messages):
            if self.stage != "execute":
                calls.append(self.stage)
                return AIMessage(content=replies[self.stage])
            task_id = "t1" if '"id": "t1"' in messages[-1].content else "t2"
            calls.append(task_id)
            if task_id == "t2":
                await asyncio.sleep(.05)
                raise RuntimeError("synthetic test failure")
            return AIMessage(content="child one completed")
    worker.agent_model_factory = Model
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert await worker.run_once() == run_id
            assert await worker.run_once() is None
        with factory() as db:
            run = db.get(Run, run_id)
            assert run.status == "pending_review"
            assert calls, (run.error_code, errors)
            assert calls.count("t1") == calls.count("t2") == 1
            assert "review" not in calls
            operations = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id)))
            assert any(op.status == "uncertain" for op in operations)
            events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id,
                                   RunEvent.event_type == "agent.stage_result")))
            assert any(event.payload.get("task_id") == "t1" for event in events)
            assert not any(event.payload.get("task_id") == "t2" for event in events)
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_worker_executes_v1_stage_snapshot_with_legacy_review(request, monkeypatch):
    from copy import deepcopy

    from test_m3_product_runtime import configured

    from omniharness.stage_contract import STAGE_V1_VERSIONS, STAGE_V1_WORKFLOW_VERSION

    stage_freeze = services.freeze_stage_run_config
    settings, engine, factory, client, worker, run_id, _ = await configured(request, monkeypatch)
    monkeypatch.setattr(services, "freeze_stage_run_config", stage_freeze)
    with factory.begin() as db:
        run = db.get(Run, run_id)
        db.delete(db.get(RunBudget, run_id))
        db.flush()
        run.workflow_version, run.thread_id = None, None
        services.freeze_stage_run_config(db, run, settings)
        snapshot = deepcopy(run.config_snapshot)
        snapshot["agent"]["workflow_version"] = STAGE_V1_WORKFLOW_VERSION
        for stage in STAGE_KEYS:
            snapshot["agent"]["roles"][stage]["role_version"] = STAGE_V1_VERSIONS[stage]
        run.config_snapshot = snapshot
        run.workflow_version = STAGE_V1_WORKFLOW_VERSION
    replies = {"think": "ok", "plan": '{"steps":[{"id":"s1","task":"a","acceptance":"ok"}]}',
               "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"a"}]}',
               "execute": "answer", "review": "old format review"}
    class Model:
        def __init__(self, stage, _config):
            self.stage = stage
        async def ainvoke(self, messages):
            return AIMessage(content=replies[self.stage])
    worker.agent_model_factory = Model
    try:
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            assert await worker.run_once() == run_id
        with factory() as db:
            run = db.get(Run, run_id)
            assert run.status == "completed"
            answer = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
            assert "old format review" in answer.content
    finally:
        client.__exit__(None, None, None)
