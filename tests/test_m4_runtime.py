"""Real installed agent graphs with local deterministic models and isolated files."""
import asyncio
import json
from contextlib import contextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from test_m3_agent_config import model

from omniharness.artifacts import ArtifactStore
from omniharness.m3_agents import AgentBoundaryError, AgentExternalCallFailed
from omniharness.m3_delegation import HierarchicalDelegation
from omniharness.m3_stages import build_stage_graph
from omniharness.m4_contract import M4_PLUGIN_VERSIONS, M4_STAGE_VERSIONS, M4_WORKFLOW_VERSION
from omniharness.m4_runtime import ExecutionBoundary, M4AgentHost, approval_resume_map, build_execution_agent
from omniharness.m4_services import acquire_workspace_lease
from omniharness.models import (
    Approval,
    ArtifactVersion,
    ModelConfig,
    OperationRecord,
    Run,
    RunBudget,
    RunCommand,
    RunEvent,
    ToolOperation,
    Workspace,
    WorkspacePolicy,
)
from omniharness.sandbox import CommandResult
from omniharness.schemas import SendMessageRequest
from omniharness.services import enqueue, freeze_stage_run_config
from omniharness.stage_contract import STAGE_KEYS, STAGE_ROLE_BINDINGS
from omniharness.worker import Worker, WorkerLock, WorkerOwnershipLost
from omniharness.workspace_io import DictWorkspaceRegistry, WorkspaceError, WorkspaceGrant, WorkspaceIO


class ToolModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "m4-local-tool-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, **kwargs):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            name, arguments = "workspace_read", {"path": "main.py"}
        elif len(results) == 1:
            original = json.loads(results[-1].content)
            name, arguments = "workspace_patch", {"path": "main.py", "content": "print(42)\n",
                                                    "expected_sha256": original["sha256"]}
        elif len(results) == 2:
            name, arguments = "run_command", {"command": "python main.py"}
        else:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="文件已修改；测试证据已保存。"))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
            "id": f"call-{len(results)}", "name": name, "args": arguments,
        }]))])

    async def _agenerate(self, messages, **kwargs):
        return self._generate(messages, **kwargs)


class StageModel(BaseChatModel):
    stage: str

    def __init__(self, stage):
        super().__init__(stage=stage)

    @property
    def _llm_type(self):
        return "m4-local-stage-test"

    def _generate(self, messages, **kwargs):
        raise NotImplementedError

    async def ainvoke(self, messages):
        replies = {"think": "修改指定项目并验证。",
                   "plan": '{"steps":[{"id":"s1","task":"修复","acceptance":"输出42"}]}',
                   "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"修复并测试"}]}',
                   "review": '{"summary":"依据记录审查","findings":[],"unverified":[]}'}
        return AIMessage(content=replies[self.stage])


class LocalBroker:
    def __init__(self, io):
        self.io, self.calls = io, []

    async def request(self, workspace_id, version, action, **payload):
        self.calls.append(action)
        return getattr(self.io, action.replace("-", "_"))(workspace_id, "local", version, **payload)


class LocalSandbox:
    def __init__(self):
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs["command"])
        await kwargs["created_sink"]("sandbox-test")
        await kwargs["started_sink"]("sandbox-test", "command-test")
        await kwargs["log_sink"]("stdout", "local fake command result\n")
        await kwargs["reclaimed_sink"]("sandbox-test")
        return CommandResult("sandbox-test", "command-test", 0, "local fake command result\n", "", 26, False)


def prepare(environment, tmp_path, *, approval=False):
    settings, engine, factory, client = environment
    settings.sandbox_image = "sha256:" + "a" * 64
    chosen = model(client, "m4", url="http://localhost:11434/v1", key=None)
    conversation = client.post("/api/v1/conversations", json={}).json()
    project = tmp_path / "isolated-project"
    project.mkdir()
    (project / "main.py").write_text("print(0)\n")
    grant = WorkspaceGrant(str(uuid4()), "local", str(project), write_allow=(".",))
    registry = DictWorkspaceRegistry([grant])
    io = WorkspaceIO(registry, tmp_path / "workspace-data", protected_roots=(tmp_path / "framework",),
                     require_write_lease=lambda *_: None)
    grant = replace(grant, expected_identity=io.probe(grant.workspace_id, "local", 1)["identity"])
    registry.grants[grant.workspace_id] = grant
    with factory.begin() as db:
        db.get(ModelConfig, chosen["id"]).capabilities = {"tools": {"status": "supported"}}
        result = enqueue(db, conversation["id"], SendMessageRequest(
            content="修复并测试", model_config_id=chosen["id"], client_request_id="m4-test"))
        run = db.get(Run, result["run_id"])
        run.config_snapshot = {**run.config_snapshot, "workflow_type": "agent"}
        freeze_stage_run_config(db, run, settings)
        snapshot = run.config_snapshot
        snapshot["agent"]["workflow_version"] = M4_WORKFLOW_VERSION
        snapshot["agent"]["plugin_versions"] = M4_PLUGIN_VERSIONS
        for stage in STAGE_KEYS:
            snapshot["agent"]["roles"][stage]["role_version"] = M4_STAGE_VERSIONS[stage]
        workspace = {"id": grant.workspace_id, "policy_version": 1, "grant": asdict(grant),
                     "broker_endpoint": settings.workspace_broker_url,
                     "tool_policy": {"command_approval": approval}, "sandbox": {
                         "endpoint": settings.sandbox_endpoint, "image": settings.sandbox_image, "instance_id": "local",
                         "snapshot_root": settings.sandbox_snapshot_root, "max_command_seconds": 600}}
        run.config_snapshot = {**snapshot, "workspace": workspace}
        db.add(Workspace(id=grant.workspace_id, name="test", host_path=str(project), probe={}))
        db.flush()
        run.workflow_version, run.workspace_id = M4_WORKFLOW_VERSION, grant.workspace_id
        db.add(WorkspacePolicy(workspace_id=grant.workspace_id, version=1, grant=asdict(grant), tool_policy={}))
        run.status = "running"
        acquire_workspace_lease(db, run.id)
        run_id = run.id
    return Worker(settings, engine), run_id, workspace, LocalBroker(io), LocalSandbox(), project


def graph_for(worker, run_id, workspace, broker, sandbox, saver, executor_model=None, strategy="staged"):
    host = M4AgentHost(worker, run_id, M4_WORKFLOW_VERSION, role_versions=M4_STAGE_VERSIONS,
                       plugin_versions=M4_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
    scheduler = HierarchicalDelegation()
    executor = build_execution_agent(model=executor_model or ToolModel(), host=host, workspace=workspace, broker=broker,
        sandbox=sandbox, artifacts=ArtifactStore(worker.settings.data_dir / "artifacts"), scheduler=scheduler)
    graph = build_stage_graph(models={stage: StageModel(stage) for stage in STAGE_KEYS}, hooks=host,
                               checkpointer=saver, workflow_version=M4_WORKFLOW_VERSION, execution_agent=executor,
                               execution_strategy=strategy)
    return graph, scheduler


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", [False, True])
async def test_real_graph_reads_patches_snapshots_and_resumes_without_replay(environment, tmp_path, approval):
    worker, run_id, workspace, broker, sandbox, project = prepare(environment, tmp_path, approval=approval)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver)
        try:
            result = await graph.ainvoke({"messages": [("user", "修复并测试")]}, config, durability="sync")
        finally:
            await scheduler.aclose()
        if approval:
            assert result.get("__interrupt__")
            assert sandbox.calls == []
            with worker.transaction() as db:
                assert db.get(RunBudget, run_id).tool_calls == 2
                decision = db.scalar(select(Approval))
                decision.status = "approved"
            # Reconstruct all runtime objects, retaining only durable graph/DB state.
            graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver)
            state = await graph.aget_state(config)
            with worker.transaction() as db:
                signals = approval_resume_map(db, run_id, state.interrupts)
            try:
                result = await graph.ainvoke(Command(resume=signals), config, durability="sync")
            finally:
                await scheduler.aclose()
        assert not result.get("__interrupt__")
        assert (project / "main.py").read_text() == "print(42)\n"
        assert broker.calls.count("patch") == 1
        assert sandbox.calls == ["python main.py"]
        with worker.transaction() as db:
            assert db.get(RunBudget, run_id).tool_calls == 3
            operations = list(db.scalars(select(ToolOperation)))
            assert all(row.status == "completed" and not row.slot_claimed for row in operations)
            assert all(row.result["artifacts"] for row in operations)
            assert db.get(RunBudget, run_id).model_calls == 8


class BatchToolModel(ToolModel):
    def _generate(self, messages, **kwargs):
        if any(isinstance(message, ToolMessage) for message in messages):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="三次读取完成。"))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
            {"id": f"read-{index}", "name": "workspace_read", "args": {"path": "main.py"}}
            for index in range(3)
        ]))])


@pytest.mark.asyncio
async def test_parallel_tool_batch_obeys_two_durable_slots(environment, tmp_path):
    worker, run_id, workspace, broker, sandbox, _ = prepare(environment, tmp_path)
    active, maximum = 0, 0
    original = broker.request

    async def slow_request(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.08)
            return await original(*args, **kwargs)
        finally:
            active -= 1
    broker.request = slow_request
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, InMemorySaver(), BatchToolModel())
        try:
            await graph.ainvoke({"messages": [("user", "读取项目")]},
                                {"configurable": {"thread_id": run_id}}, durability="sync")
        finally:
            await scheduler.aclose()
    assert maximum == 2 and active == 0
    with worker.sessions() as db:
        assert db.get(RunBudget, run_id).tool_calls == 3
        assert not any(row.slot_claimed for row in db.scalars(select(ToolOperation)))


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["model_calls", "tool_calls"])
async def test_saved_evidence_recovers_after_graph_write_loss(environment, tmp_path, monkeypatch, boundary):
    worker, run_id, workspace, broker, sandbox, _ = prepare(environment, tmp_path)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
    finish = M4AgentHost.finish_invocation
    failed = False

    async def save_then_fail(self, kind, role, tool_name, identity, **kwargs):
        nonlocal failed
        result = await finish(self, kind, role, tool_name, identity, **kwargs)
        if not failed and kind == boundary and (role == "execute" if boundary == "model_calls" else tool_name == "workspace_patch"):
            failed = True
            raise AgentBoundaryError("Simulated checkpoint handoff loss after durable completion")
        return result
    monkeypatch.setattr(M4AgentHost, "finish_invocation", save_then_fail)
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver)
        try:
            with pytest.raises(AgentBoundaryError):
                await graph.ainvoke({"messages": [("user", "修复项目")]}, config, durability="sync")
        finally:
            await scheduler.aclose()
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver)
        try:
            result = await graph.ainvoke(None, config, durability="sync")
        finally:
            await scheduler.aclose()
        assert not result.get("__interrupt__")
    assert broker.calls.count("patch") == 1
    with worker.sessions() as db:
        assert db.get(RunBudget, run_id).model_calls == 8
        assert db.get(RunBudget, run_id).tool_calls == 3


@pytest.mark.asyncio
async def test_unknown_patch_never_replays_and_keeps_claim(environment, tmp_path):
    worker, run_id, workspace, broker, sandbox, project = prepare(environment, tmp_path)
    original = broker.request
    async def lose_patch_result(workspace_id, version, action, **payload):
        result = await original(workspace_id, version, action, **payload)
        if action == "patch":
            raise WorkspaceError("native_result_unknown", "Simulated native response loss")
        return result
    broker.request = lose_patch_result
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run_id}}
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        for payload in ({"messages": [("user", "修复项目")]}, None):
            graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver)
            try:
                with pytest.raises(AgentBoundaryError):
                    await graph.ainvoke(payload, config, durability="sync")
            finally:
                await scheduler.aclose()
    assert broker.calls.count("patch") == 1
    assert (project / "main.py").read_text() == "print(42)\n"
    with worker.sessions() as db:
        row = db.scalar(select(ToolOperation).where(ToolOperation.tool_name == "workspace_patch"))
        assert row.status == "uncertain" and row.slot_claimed


@pytest.mark.asyncio
async def test_stage_context_compacts_long_history_with_shared_budget(environment, tmp_path):
    worker, run_id, workspace, broker, sandbox, _ = prepare(environment, tmp_path)
    observed_limits = []
    class SummaryModel:
        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content="旧对话讨论了项目背景，当前任务仍以受保护的原目标为准。",
                             response_metadata={"finish_reason": "stop"},
                             usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    def factory(role, config):
        observed_limits.append(config.max_output_tokens)
        return SummaryModel()
    worker.agent_model_factory = factory
    history = [("user" if index % 2 == 0 else "assistant", f"历史记录{index}：项目此前的讨论。") for index in range(110)]
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, InMemorySaver())
        try:
            await graph.ainvoke({"messages": history}, {"configurable": {"thread_id": run_id}}, durability="sync")
        finally:
            await scheduler.aclose()
    assert observed_limits and set(observed_limits) == {4000}
    with worker.sessions() as db:
        summaries = list(db.scalars(select(ArtifactVersion).where(ArtifactVersion.kind == "context_summary")))
        assert len(summaries) == len(observed_limits)
        assert db.get(RunBudget, run_id).model_calls == 8 + len(summaries)
        usage = db.get(Run, run_id).usage
        assert usage["observed_tokens"]["total_tokens"] == 120 * len(summaries)
        assert usage["total_tokens"] is None and not usage["usage_complete"]


@pytest.mark.asyncio
async def test_single_agent_uses_same_tools_without_extra_stage_calls(environment, tmp_path):
    worker, run_id, workspace, broker, sandbox, _ = prepare(environment, tmp_path)
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, InMemorySaver(), strategy="single")
        try:
            result = await graph.ainvoke({"messages": [("user", "修复项目")]},
                                        {"configurable": {"thread_id": run_id}}, durability="sync")
        finally:
            await scheduler.aclose()
    assert set(result["stage_outputs"]) == {"execute"}
    assert broker.calls.count("patch") == len(sandbox.calls) == 1
    with worker.sessions() as db:
        assert db.get(RunBudget, run_id).model_calls == 4
        assert db.get(RunBudget, run_id).tool_calls == 3


@pytest.mark.asyncio
async def test_dispatch_receives_paired_history_and_protected_validated_plan(environment, tmp_path, monkeypatch):
    worker, run_id, workspace, broker, sandbox, _ = prepare(environment, tmp_path)
    original = StageModel.ainvoke
    observed = []

    async def inspect(self, messages):
        if self.stage == "dispatch":
            observed.append(messages)
            assert messages[-1].id == "omni-stage:dispatch"
            plan_instruction = next(index for index, message in enumerate(messages) if message.id == "omni-stage:plan")
            plan_answer = next(index for index, message in enumerate(messages)
                               if isinstance(message, AIMessage) and '"steps"' in str(message.content))
            assert plan_instruction < plan_answer < len(messages) - 1
            plan = json.loads(messages[-1].content.split("下面是已校验的完整计划：\n", 1)[1])
            assert plan["steps"] == [{"id": "s1", "task": "修复", "acceptance": "输出42", "depends_on": []}]
        return await original(self, messages)

    monkeypatch.setattr(StageModel, "ainvoke", inspect)
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, InMemorySaver())
        try:
            await graph.ainvoke({"messages": [("user", "修复项目")]},
                                {"configurable": {"thread_id": run_id}}, durability="sync")
        finally:
            await scheduler.aclose()
    assert len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("supplement", [False, True])
async def test_execute_keeps_task_before_progress_and_does_not_reissue_delivered_requirements(
        environment, tmp_path, supplement):
    worker, run_id, workspace, broker, sandbox, _ = prepare(environment, tmp_path)
    original = broker.request
    observed, command_id = [], None

    async def read_then_supplement(workspace_id, version, action, **payload):
        nonlocal command_id
        result = await original(workspace_id, version, action, **payload)
        if supplement and action == "read" and command_id is None:
            response = environment[3].post(f"/api/v1/runs/{run_id}/commands", json={
                "command_type": "append_requirement", "client_request_id": "after-read",
                "content": "请保持输出42的原目标，读取后继续修改和测试。"})
            assert response.status_code == 202, response.text
            with worker.sessions() as db:
                command_id = db.scalar(select(RunCommand.id).where(RunCommand.run_id == run_id))
        return result

    class OrderedToolModel(ToolModel):
        def _generate(self, messages, **kwargs):
            observed.append(list(messages))
            tasks = [index for index, message in enumerate(messages)
                     if isinstance(message, HumanMessage) and (message.id or "").startswith("omni-task:")]
            assert len(tasks) == 1
            results = [index for index, message in enumerate(messages) if isinstance(message, ToolMessage)]
            if results:
                assert tasks[0] < results[0]
                commands = [message for message in messages if isinstance(message, HumanMessage)
                            and (message.id or "").startswith("omni-command:")]
                if supplement:
                    assert len(commands) == 1
                    if len(results) == 1:  # A genuinely new requirement is delivered once.
                        assert messages[-1] is commands[0]
                    else:
                        assert messages.index(commands[0]) < results[0]
                        assert isinstance(messages[-1], ToolMessage)
                else:
                    assert isinstance(messages[-1], ToolMessage)
            return super()._generate(messages, **kwargs)

    broker.request = read_then_supplement
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, InMemorySaver(), OrderedToolModel())
        try:
            await graph.ainvoke({"messages": [("user", "修复项目")]},
                                {"configurable": {"thread_id": run_id}}, durability="sync")
        finally:
            await scheduler.aclose()
    assert len(observed) == 4
    assert broker.calls.count("read") == broker.calls.count("patch") == len(sandbox.calls) == 1
    if supplement:
        with worker.sessions() as db:
            command = db.get(RunCommand, command_id)
            assert command.status == "applied" and command.payload["delivery"] == "prepared_for_model"
            events = db.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "run.command"))
            assert sum(event.payload.get("command_id") == command_id and
                       event.payload.get("delivery") == "prepared_for_model" for event in events) == 1


class MixedToolModel(BatchToolModel):
    def _generate(self, messages, **kwargs):
        if any(isinstance(message, ToolMessage) for message in messages):
            return super()._generate(messages, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
            {"id": "slow-read", "name": "workspace_read", "args": {"path": "main.py"}},
            {"id": "approved-command", "name": "run_command", "args": {"command": "python main.py"}},
        ]))])


@pytest.mark.asyncio
async def test_approval_waits_for_sibling_operation_to_save_evidence(environment, tmp_path):
    worker, run_id, workspace, broker, sandbox, _ = prepare(environment, tmp_path, approval=True)
    original = broker.request
    finished = False
    async def slow_read(workspace_id, version, action, **payload):
        nonlocal finished
        if action == "read":
            await asyncio.sleep(0.1)
        result = await original(workspace_id, version, action, **payload)
        if action == "read":
            finished = True
        return result
    broker.request = slow_read
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, InMemorySaver(), MixedToolModel())
        try:
            result = await graph.ainvoke({"messages": [("user", "读取并测试")]},
                                        {"configurable": {"thread_id": run_id}}, durability="sync")
            assert result.get("__interrupt__") and finished and not scheduler._operations
        finally:
            await scheduler.aclose()
    assert sandbox.calls == []
    with worker.sessions() as db:
        read = db.scalar(select(ToolOperation).where(ToolOperation.tool_name == "workspace_read"))
        assert read.status == "completed" and not read.slot_claimed
        assert db.get(RunBudget, run_id).tool_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "category", "raised"), [
    (TimeoutError("private-canary"), "timeout", AgentExternalCallFailed),
    (ConnectionError("private-canary"), "connection", AgentExternalCallFailed),
    (httpx.HTTPStatusError("private-canary", request=httpx.Request("GET", "https://example.invalid"),
                           response=httpx.Response(429)), "http_status", AgentExternalCallFailed),
    (SQLAlchemyError("private-canary"), "database", AgentExternalCallFailed),
    (ValueError("private-canary"), "unknown", AgentExternalCallFailed),
    (WorkerOwnershipLost("private-canary"), "unknown", WorkerOwnershipLost),
])
async def test_execute_failure_records_sanitized_diagnostic_and_never_replays(
        environment, tmp_path, error, category, raised):
    worker, run_id, workspace, broker, sandbox, _ = prepare(environment, tmp_path)
    calls = []

    class FailingModel(ToolModel):
        async def _agenerate(self, messages, **kwargs):
            calls.append(1)
            raise error

    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver,
                                     FailingModel(), strategy="single")
        try:
            with pytest.raises(raised) as captured:
                await graph.ainvoke({"messages": [("user", "读取项目")]}, config, durability="sync")
        finally:
            await scheduler.aclose()
        assert "private-canary" not in str(captured.value)
        with worker.sessions() as db:
            event = db.scalar(select(RunEvent).where(RunEvent.run_id == run_id,
                                                     RunEvent.event_type == "agent.model_uncertain"))
            row = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id,
                                                          OperationRecord.node_id == "agent:execute:model_calls"))
            assert event is not None and row is not None
            assert row.status == "uncertain" and row.error_code == "RESULT_UNKNOWN"
            assert event.payload["diagnostic"]["exception_category"] == category
            if category == "http_status":
                assert event.payload["diagnostic"]["http_status"] == 429
            assert row.result["diagnostic"] == event.payload["diagnostic"]
            assert "private-canary" not in json.dumps(event.payload)
            assert "private-canary" not in json.dumps(row.result)
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver,
                                     FailingModel(), strategy="single")
        try:
            with pytest.raises(AgentBoundaryError):
                await graph.ainvoke(None, config, durability="sync")
        finally:
            await scheduler.aclose()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_execute_result_save_failure_is_not_recorded_as_model_success(environment, tmp_path, monkeypatch):
    worker, run_id, workspace, broker, sandbox, _ = prepare(environment, tmp_path)
    finish = M4AgentHost.finish_invocation

    async def fail_before_save(self, kind, role, tool_name, identity, **kwargs):
        if kind == "model_calls" and role == "execute":
            raise SQLAlchemyError("database-private-canary")
        return await finish(self, kind, role, tool_name, identity, **kwargs)

    monkeypatch.setattr(M4AgentHost, "finish_invocation", fail_before_save)
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, InMemorySaver(), strategy="single")
        try:
            with pytest.raises(AgentExternalCallFailed) as captured:
                await graph.ainvoke({"messages": [("user", "读取项目")]},
                                    {"configurable": {"thread_id": run_id}}, durability="sync")
        finally:
            await scheduler.aclose()
    assert "database-private-canary" not in str(captured.value)
    with worker.sessions() as db:
        op = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id,
                                                     OperationRecord.node_id == "agent:execute:model_calls"))
        assert op.status == "uncertain"
        assert op.result["diagnostic"]["exception_category"] == "database"
        assert "database-private-canary" not in json.dumps(op.result)
        assert db.scalar(select(RunEvent).where(RunEvent.run_id == run_id,
                                               RunEvent.event_type == "agent.model_completed")) is None


@pytest.mark.asyncio
async def test_lost_owner_cannot_persist_failure_diagnostic(environment, tmp_path):
    worker, run_id, workspace, broker, sandbox, _ = prepare(environment, tmp_path)

    class LostOwnerModel(ToolModel):
        async def _agenerate(self, messages, **kwargs):
            worker.lock.lost = True
            raise WorkerOwnershipLost("private-canary")

    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, InMemorySaver(),
                                     LostOwnerModel(), strategy="single")
        try:
            with pytest.raises(WorkerOwnershipLost) as captured:
                await graph.ainvoke({"messages": [("user", "读取项目")]},
                                    {"configurable": {"thread_id": run_id}}, durability="sync")
        finally:
            await scheduler.aclose()
        assert "private-canary" not in str(captured.value)
        with worker.sessions() as db:
            op = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id,
                                                         OperationRecord.node_id == "agent:execute:model_calls"))
            assert op is not None and op.status != "completed"
            assert not op.result or "diagnostic" not in op.result
            assert db.scalar(select(RunEvent).where(RunEvent.run_id == run_id,
                                                   RunEvent.event_type == "agent.model_uncertain")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", [GraphBubbleUp(), asyncio.CancelledError(),
                                    WorkerOwnershipLost("private-canary")])
@pytest.mark.parametrize("diagnostic_fails", [False, True])
async def test_execute_diagnostics_never_suppress_control_or_lost_owner(signal, diagnostic_fails):
    class FakeWorker:
        @contextmanager
        def transaction(self):
            yield None

    class FakeHost:
        workflow_version = M4_WORKFLOW_VERSION
        run_id = "control-test"
        worker = FakeWorker()
        serial = asyncio.Lock()
        diagnostics = []

        async def check_control(self, *args):
            pass

        def _run(self, *args, **kwargs):
            return None, {"model": {"max_output_tokens": 1000}}

        async def replay_invocation(self, *args):
            return None

        async def reserve_invocation(self, *args, **kwargs):
            return True

        async def mark_invocation_uncertain_with_diagnostics(self, *args, **kwargs):
            self.diagnostics.append(kwargs)
            if diagnostic_fails:
                raise RuntimeError("diagnostic private-canary")

    class Scheduler:
        async def run_tracked(self, function):
            return await function()

    host = FakeHost()
    boundary = ExecutionBoundary(host=host, gateway=None, scheduler=Scheduler())
    request = ModelRequest(model=ToolModel(), messages=[HumanMessage(content="test")],
                           runtime=SimpleNamespace(execution_info=SimpleNamespace(
                               checkpoint_ns="", task_id="task", thread_id="control-test")))

    async def fail(_request):
        raise signal

    with pytest.raises(type(signal)) as captured:
        await boundary.awrap_model_call(request, fail)
    assert len(host.diagnostics) == 1
    assert host.diagnostics[0]["error"] is signal
    assert host.diagnostics[0]["transport_mode"] == "ainvoke_default"
    if isinstance(signal, WorkerOwnershipLost):
        assert "private-canary" not in str(captured.value)
    else:
        assert captured.value is signal
