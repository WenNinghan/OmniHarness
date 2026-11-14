"""Summary ledger integration against an isolated dedicated PostgreSQL schema.

All model outputs here are injected local fixtures. No paid API is contacted.
"""
import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from conftest import create_chat
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select, text
from test_m3_host import host_for, prepared
from test_postgres import postgres_environment  # noqa: F401 - dedicated opt-in fixture

from omniharness.api import create_app
from omniharness.context_host import (
    DurableSummaryExecutor,
    model_fingerprint,
    prepare_agent_context,
    prepare_chat_context,
)
from omniharness.context_policy import ContextPolicy
from omniharness.context_runtime import ContextPreparationError, ContextScope, MessageSource, SummaryRequest
from omniharness.m3_agents import AgentBoundaryError, AgentBudgetExceeded
from omniharness.m3_delegation import HierarchicalDelegation
from omniharness.m3_host import AgentCancellationRequested, AgentControlPending, AgentOperationNeedsReview
from omniharness.models import ArtifactVersion, Message, OperationRecord, Run, RunBudget, RunCommand, RunEvent
from omniharness.providers import ProviderError
from omniharness.schemas import RunCommandRequest
from omniharness.security import create_pairing_code
from omniharness.services import BudgetLimits, cancel_run, ensure_budget, submit_command
from omniharness.worker import Worker, WorkerLock, WorkerOwnershipLost


@pytest.fixture
def context_pg(request):
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        code = create_pairing_code(factory)
        assert client.post("/api/v1/auth/pair", json={"code": code}).status_code == 200
        yield SimpleNamespace(settings=settings, engine=engine, factory=factory, client=client)


def chat(env, *, cap=50):
    _, _, queued = create_chat(env.client)
    run_id = queued["run_id"]
    with env.factory.begin() as db:
        run = db.get(Run, run_id)
        run.status = "running"
        ensure_budget(db, run_id, BudgetLimits(max_model_calls=cap))
        return run_id, deepcopy(run.config_snapshot)


def request(run_id, snapshot, *, role="chat", salt="1"):
    policy = ContextPolicy()
    return SummaryRequest(ContextScope(run_id, role, snapshot["id"], model_fingerprint(snapshot)),
        model_fingerprint({"id": salt}), model_fingerprint({"input": salt}), policy.version,
        model_fingerprint(asdict(policy)), (HumanMessage(content="Summarize public fixture"),),
        (MessageSource("persisted-source", "a" * 64),), (), (), 100, 16000, 4000, "fixture", True)


class LocalFactory:
    def __init__(self, output=None, callback=None):
        self.configs, self.calls = [], []
        self.output = output if output is not None else AIMessage(
            content="Public summary with source evidence.", response_metadata={"finish_reason": "stop"},
            additional_kwargs={"reasoning_content": "private-thought-do-not-store", "signature": "secret-signature"},
            usage_metadata={"input_tokens": 80, "output_tokens": 8, "total_tokens": 88})
        self.callback = callback

    def __call__(self, config):
        self.configs.append(config)
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        await asyncio.sleep(0)
        if self.callback:
            self.callback()
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


def worker_for(env, lock):
    worker = Worker(env.settings, env.engine)
    worker.lock = lock
    return worker


@pytest.mark.asyncio
async def test_completed_summary_reused_after_worker_restart_with_sources_and_public_evidence(context_pg):
    env = context_pg
    run_id, snapshot = chat(env)
    call, local = request(run_id, snapshot), LocalFactory()
    with WorkerLock(env.engine, env.settings) as owner:
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        first = await executor.execute(call)
    # A new worker resumes before any caller checkpoint was saved.
    with WorkerLock(env.engine, env.settings) as owner:
        second = await DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot,
                                               model_factory=local).execute(call)
    assert first == second and len(local.calls) == 1
    assert local.configs[0].max_output_tokens == 4000 and snapshot["max_output_tokens"] == 100000
    with env.factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 1
        row = db.get(ArtifactVersion, first.artifact_id)
        artifact = executor.store.read(row, run_id=run_id).decode()
        assert "persisted-source" in artifact and "public fixture" in artifact
        assert "private-thought" not in artifact and "secret-signature" not in artifact
        assert json.loads(artifact)["cost"] is None
        operation = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
        assert operation.status == "completed" and row.operation_id == operation.operation_id
        assert len(list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id,
            RunEvent.event_type == "context.summary_completed")))) == 1


@pytest.mark.asyncio
async def test_atomic_summary_artifact_failure_preserves_ledger_and_never_replays(context_pg, monkeypatch):
    env = context_pg
    run_id, snapshot = chat(env)
    call, local = request(run_id, snapshot), LocalFactory()
    with WorkerLock(env.engine, env.settings) as owner:
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        put = executor.store.put

        def failed_put(*args, **kwargs):
            put(*args, **kwargs)
            raise OSError("private-file-path-do-not-echo")

        monkeypatch.setattr(executor.store, "put", failed_put)
        with pytest.raises(ProviderError) as failure:
            await executor.execute(call)
        assert "private-file-path" not in str(failure.value)
        with pytest.raises(AgentOperationNeedsReview):
            await executor.execute(call)
    assert len(local.calls) == 1
    with env.factory() as db:
        assert not list(db.scalars(select(ArtifactVersion).where(ArtifactVersion.run_id == run_id)))
        assert db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id)).status == "uncertain"
        assert db.get(RunBudget, run_id).model_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [RuntimeError("upstream-private-token"), asyncio.CancelledError()])
async def test_dispatched_summary_unknown_or_cancelled_never_replays(context_pg, output):
    env = context_pg
    run_id, snapshot = chat(env)
    call, local = request(run_id, snapshot), LocalFactory(output)
    with WorkerLock(env.engine, env.settings) as owner:
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        with pytest.raises((ProviderError, asyncio.CancelledError)) as failure:
            await executor.execute(call)
        assert "upstream-private-token" not in str(failure.value)
        with pytest.raises(AgentOperationNeedsReview):
            await executor.execute(call)
    assert len(local.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    AIMessage(content="", additional_kwargs={"reasoning_content": "private"}, response_metadata={"finish_reason": "stop"}),
    AIMessage(content="truncated", response_metadata={"finish_reason": "length"}),
    AIMessage(content="unconfirmed"),
])
async def test_invalid_final_summary_is_saved_but_cannot_replace_history_or_retry(context_pg, output):
    env = context_pg
    run_id, snapshot = chat(env)
    call, local = request(run_id, snapshot), LocalFactory(output)
    with WorkerLock(env.engine, env.settings) as owner:
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        for _ in range(2):
            with pytest.raises(ContextPreparationError, match="did not complete"):
                await executor.execute(call)
    assert len(local.calls) == 1
    with env.factory() as db:
        assert db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id)).status == "completed"
        assert len(list(db.scalars(select(ArtifactVersion).where(ArtifactVersion.run_id == run_id)))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("command,error", [("pause", AgentControlPending), ("cancel", AgentCancellationRequested)])
async def test_chat_control_before_summary_spends_no_budget(context_pg, command, error):
    env = context_pg
    run_id, snapshot = chat(env)
    local = LocalFactory()
    with env.factory.begin() as db:
        if command == "cancel":
            cancel_run(db, run_id)
        else:
            submit_command(db, run_id, RunCommandRequest(command_type=command, client_request_id=command))
    with WorkerLock(env.engine, env.settings) as owner:
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        with pytest.raises(error):
            await executor.execute(request(run_id, snapshot))
    with env.factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 0
    assert not local.calls


@pytest.mark.asyncio
async def test_agent_summary_keeps_pending_requirement_and_uses_same_budget(context_pg):
    env = context_pg
    run_id = prepared(env.settings, env.engine, env.factory, env.client)
    with env.factory.begin() as db:
        snapshot = deepcopy(db.get(Run, run_id).config_snapshot["agent"]["roles"]["think"]["model"])
        command = submit_command(db, run_id, RunCommandRequest(command_type="append_requirement",
            client_request_id="supplement", content="Keep this requirement for the next task model"))
    local = LocalFactory()
    with WorkerLock(env.engine, env.settings) as owner:
        worker = worker_for(env, owner)
        executor = DurableSummaryExecutor(worker, run_id, snapshot, host=host_for(worker, run_id), model_factory=local)
        await executor.execute(request(run_id, snapshot, role="coordinator"))
        with worker.transaction() as db:
            assert db.get(RunCommand, command["command_id"]).status == "pending"
            assert db.get(RunBudget, run_id).model_calls == 1
        assert worker.reserve_model_call(run_id)
    with env.factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 2


@pytest.mark.asyncio
async def test_two_agent_summaries_compete_for_last_shared_call_without_overshoot(context_pg):
    env = context_pg
    run_id = prepared(env.settings, env.engine, env.factory, env.client)
    with env.factory.begin() as db:
        snapshots = deepcopy(db.get(Run, run_id).config_snapshot["agent"]["roles"])
        db.get(RunBudget, run_id).model_calls = 49
    local = LocalFactory()
    with WorkerLock(env.engine, env.settings) as owner:
        worker = worker_for(env, owner)
        host = host_for(worker, run_id)
        executors = [(role, DurableSummaryExecutor(worker, run_id, snapshots[key]["model"],
                      host=host, model_factory=local)) for role, key in (("coordinator", "think"), ("researcher", "plan"))]
        results = await asyncio.gather(*(executor.execute(request(run_id, executor.snapshot, role=role, salt=role))
                                         for role, executor in executors), return_exceptions=True)
        assert sum(isinstance(item, AgentBudgetExceeded) for item in results) == 1
        assert not worker.reserve_model_call(run_id)
    assert len(local.calls) == 1
    with env.factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 50


@pytest.mark.asyncio
async def test_lost_owner_cannot_save_model_result_or_replay_after_replacement(context_pg):
    env = context_pg
    run_id, snapshot = chat(env)
    call = request(run_id, snapshot)
    with WorkerLock(env.engine, env.settings) as owner:
        with owner.connection.begin():
            pid = owner.connection.scalar(text("select pg_backend_pid()"))

        def kill_owner():
            with env.engine.begin() as other:
                assert other.scalar(text("select pg_terminate_backend(:pid)"), {"pid": pid})

        local = LocalFactory(callback=kill_owner)
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        with pytest.raises(WorkerOwnershipLost):
            await executor.execute(call)
    with WorkerLock(env.engine, env.settings) as owner:
        with pytest.raises(AgentOperationNeedsReview):
            await DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local).execute(call)
    assert len(local.calls) == 1
    with env.factory() as db:
        assert not list(db.scalars(select(ArtifactVersion).where(ArtifactVersion.run_id == run_id)))
        assert db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id)).status == "running"


@pytest.mark.asyncio
async def test_replay_rejects_artifact_from_another_run(context_pg):
    env = context_pg
    first_id, first_snapshot = chat(env)
    other_id, other_snapshot = chat(env)
    local = LocalFactory()
    with WorkerLock(env.engine, env.settings) as owner:
        worker = worker_for(env, owner)
        await DurableSummaryExecutor(worker, first_id, first_snapshot, model_factory=local).execute(request(first_id, first_snapshot))
        executor = DurableSummaryExecutor(worker, other_id, other_snapshot, model_factory=local)
        await executor.execute(request(other_id, other_snapshot))
        with worker.transaction() as db:
            first = db.scalar(select(OperationRecord).where(OperationRecord.run_id == first_id))
            other = db.scalar(select(OperationRecord).where(OperationRecord.run_id == other_id))
            other.result = {**other.result, "response": first.result["response"]}
        with pytest.raises(AgentOperationNeedsReview):
            await executor.execute(request(other_id, other_snapshot))
    assert len(local.calls) == 2


@pytest.mark.asyncio
async def test_chat_preparation_uses_real_middleware_and_durable_summary_preserves_db_history(context_pg):
    env = context_pg
    run_id, snapshot = chat(env)
    with env.factory.begin() as db:
        run = db.get(Run, run_id)
        previous_runs = [Run(conversation_id=run.conversation_id, model_config_id=run.model_config_id,
            model_label=run.model_label, config_snapshot=snapshot, ordinal=index + 2,
            client_request_id=f"history-{index}", status="completed", mode="mock") for index in range(20)]
        db.add_all(previous_runs)
        db.flush()
        rows = [Message(conversation_id=run.conversation_id, run_id=old.id, role="user",
                        content=f"Source {index} " + "历史材料" * 220) for index, old in enumerate(previous_runs)]
        db.add_all(rows)
        db.flush()
        history = [{"id": row.id, "role": row.role, "content": row.content} for row in rows]
        history.append({"id": "latest-human", "role": "user", "content": "Retain this question unchanged"})
    local = LocalFactory()
    with WorkerLock(env.engine, env.settings) as owner:
        worker = worker_for(env, owner)
        result = await prepare_chat_context(worker, run_id, history, snapshot, model_factory=local)
        again = await prepare_chat_context(worker, run_id, history, snapshot, model_factory=local)
        assert result == again and result.compacted and result.messages[-1].content == history[-1]["content"]
        assert len(local.calls) == len(result.summaries) and result.input_tokens <= 16000
    with env.factory() as db:
        assert all(db.get(Message, item["id"]).content == item["content"] for item in history[:-1])
        assert db.get(RunBudget, run_id).model_calls == len(local.calls)


@pytest.mark.asyncio
async def test_snapshot_mismatch_is_rejected_before_dispatch(context_pg):
    env = context_pg
    run_id, snapshot = chat(env)
    local = LocalFactory()
    with WorkerLock(env.engine, env.settings) as owner:
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        call = request(run_id, snapshot)
        with pytest.raises(AgentBoundaryError):
            await executor.execute(replace(call, scope=replace(call.scope, model_fingerprint="wrong")))
    assert not local.calls


@pytest.mark.asyncio
async def test_agent_helper_protects_goal_commands_and_tool_pairs_with_frozen_role(context_pg):
    env = context_pg
    run_id = prepared(env.settings, env.engine, env.factory, env.client)
    with env.factory() as db:
        row = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
        goal = HumanMessage(id=row.id, content=row.content)
    command = HumanMessage(id="omni-command:retained", content="Keep the requirement unchanged")
    history = [HumanMessage(id=f"history-{i}", content="中文材料" * 250) for i in range(20)]
    call = AIMessage(id="tool-call", content="", tool_calls=[{"id": "call-1", "name": "read_artifact", "args": {}}])
    result = ToolMessage(id="tool-result", tool_call_id="call-1", content="Public complete tool result")
    task = HumanMessage(id="omni-task:execute-1", content="Exact task acceptance criteria")
    stage = HumanMessage(id="omni-stage:coordinator", content="Exact current stage constraints")
    local = LocalFactory()
    with WorkerLock(env.engine, env.settings) as owner:
        worker = worker_for(env, owner)
        worker.agent_model_factory = lambda role, config: local(config)
        host = host_for(worker, run_id)
        host.scheduler = HierarchicalDelegation()
        output = await prepare_agent_context(host, "coordinator", [history[0], goal, *history[1:], task, stage, call, result, command],
            model=FakeListChatModel(responses=["middleware model is never called directly"]),
            checkpoint_ns="agent-root", task_id="task-1")
        await host.scheduler.wait_operations()
        assert output.compacted and output.messages[0] == goal and output.messages[-1] == command
        assert [m.id for m in output.messages][-5:] == ["tool-call", "tool-result", task.id, stage.id, command.id]
        assert all(config.max_output_tokens == 4000 for config in local.configs)
        with worker.transaction() as db:
            assert db.get(RunBudget, run_id).model_calls == len(local.calls)
            for row in db.scalars(select(ArtifactVersion).where(ArtifactVersion.run_id == run_id)):
                assert row.metadata_json["scope"]["checkpoint_ns"] == "agent-root"
                assert row.metadata_json["scope"]["task_id"] == "task-1"
                assert row.metadata_json["scope"]["role"] == "coordinator"


@pytest.mark.asyncio
async def test_scheduler_barrier_waits_for_summary_and_durable_completion(context_pg):
    env = context_pg
    run_id = prepared(env.settings, env.engine, env.factory, env.client)
    with env.factory() as db:
        snapshot = deepcopy(db.get(Run, run_id).config_snapshot["agent"]["roles"]["think"]["model"])
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowFactory(LocalFactory):
        async def ainvoke(self, messages, **kwargs):
            entered.set()
            await release.wait()
            return await super().ainvoke(messages, **kwargs)

    with WorkerLock(env.engine, env.settings) as owner:
        worker = worker_for(env, owner)
        host = host_for(worker, run_id)
        host.scheduler = HierarchicalDelegation()
        executor = DurableSummaryExecutor(worker, run_id, snapshot, host=host, model_factory=SlowFactory())
        task = asyncio.create_task(executor.execute(request(run_id, snapshot, role="coordinator")))
        await asyncio.wait_for(entered.wait(), 3)
        barrier = asyncio.create_task(host.scheduler.wait_operations())
        await asyncio.sleep(0)
        assert not barrier.done()
        release.set()
        result = await asyncio.wait_for(task, 3)
        await asyncio.wait_for(barrier, 3)
        with worker.transaction() as db:
            assert db.get(ArtifactVersion, result.artifact_id) is not None
            assert db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id)).status == "completed"


@pytest.mark.asyncio
async def test_current_stage_is_protected_without_reordering_answered_previous_instructions(context_pg):
    env = context_pg
    run_id = prepared(env.settings, env.engine, env.factory, env.client)
    with env.factory() as db:
        goal = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
        messages = [HumanMessage(id=goal.id, content=goal.content),
                    HumanMessage(id="omni-stage:think", content="Only understand the task"),
                    AIMessage(id="think-answer", content="Understood"),
                    HumanMessage(id="omni-stage:plan", content="Only return a plan JSON"),
                    AIMessage(id="plan-answer", content='{"steps":[]}'),
                    HumanMessage(id="omni-stage:dispatch", content='Only return tasks JSON. Verified plan: {"steps":[]}')]
    with WorkerLock(env.engine, env.settings) as owner:
        host = host_for(worker_for(env, owner), run_id)
        host.role_bindings["dispatch"] = "think"
        output = await prepare_agent_context(host, "dispatch", messages,
            model=FakeListChatModel(responses=["must not be called"]))
    assert not output.compacted
    assert [message.id for message in output.messages] == [message.id for message in messages]
    assert [message.content for message in output.messages] == [message.content for message in messages]


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("new_requirement", [False, True])
async def test_execute_keeps_task_and_delivered_requirements_before_latest_tool_results(context_pg, compact, new_requirement):
    env = context_pg
    run_id = prepared(env.settings, env.engine, env.factory, env.client)
    with env.factory() as db:
        row = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
        goal = HumanMessage(id=row.id, content=row.content)
    task = HumanMessage(id="omni-task:execute-1", content="Read, fix, test, then report this one deliverable")
    delivered = HumanMessage(id="omni-command:delivered", content="Keep the original interface")
    pending = HumanMessage(id="omni-command:latest", content="Include the test command in the report")
    history = ([HumanMessage(id=f"history-{i}", content="中文材料" * 250) for i in range(20)] if compact else [])
    call = AIMessage(id="test-call", content="", tool_calls=[{"id": "test-1", "name": "run_command", "args": {}}])
    result = ToolMessage(id="test-result", tool_call_id="test-1", content="Actual test passed; exit 0")
    messages = [goal, *history, task, delivered, call, result, *([pending] if new_requirement else [])]
    local = LocalFactory()
    with WorkerLock(env.engine, env.settings) as owner:
        worker = worker_for(env, owner)
        worker.agent_model_factory = lambda role, config: local(config)
        host = host_for(worker, run_id)
        host.role_bindings["execute"] = "think"
        output = await prepare_agent_context(host, "execute", messages,
            model=FakeListChatModel(responses=["must not be called"]))
    assert output.compacted is compact
    assert list(output.messages[:3]) == [goal, task, delivered]
    expected_tail = [call, result, *([pending] if new_requirement else [])]
    assert list(output.messages[-len(expected_tail):]) == expected_tail
    assert sum(message.id == task.id for message in output.messages) == 1
    assert sum(message.id == delivered.id for message in output.messages) == 1
    for summary in output.summaries:
        assert not {goal.id, task.id, delivered.id, pending.id} & {source.message_id for source in summary.sources}
    # Caller-owned source order is unchanged; no tool call/result was split.
    assert messages[-(3 if new_requirement else 2):] == expected_tail


@pytest.mark.asyncio
@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 4369, "output_tokens": 2613, "total_tokens": 6982}, "complete"),
    (None, "summary_output_limit"),
    ({"input_tokens": 4369, "output_tokens": 4001, "total_tokens": 8370}, "summary_output_limit"),
])
async def test_durable_summary_distinguishes_actual_output_tokens_and_estimated_characters(context_pg, usage, expected):
    env = context_pg
    run_id, snapshot = chat(env)
    public_text = ("保留原目标、约束、已执行工具及证据引用，不得把未经验证的结论写成事实。" * 150)[:4404]
    assert len(public_text) == 4404
    local = LocalFactory(AIMessage(content=public_text, usage_metadata=usage, response_metadata={"finish_reason": "stop"}))
    with WorkerLock(env.engine, env.settings) as owner:
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        if expected == "complete":
            stored = await executor.execute(request(run_id, snapshot))
            assert stored.text == public_text
        else:
            with pytest.raises(ContextPreparationError):
                await executor.execute(request(run_id, snapshot))
        with worker_for(env, owner).transaction() as db:
            row = db.scalar(select(ArtifactVersion).where(ArtifactVersion.run_id == run_id))
            evidence = json.loads(executor.store.read(row).decode())
            assert evidence["output_status"] == expected
            assert evidence["output_counting_is_estimate"] is (usage is None)
            if usage:
                assert evidence["output_count"] == usage["output_tokens"]
            else:
                assert evidence["output_count"] >= len(public_text) > 4000
    assert len(local.calls) == 1
