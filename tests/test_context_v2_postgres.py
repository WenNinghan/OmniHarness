"""Dedicated PostgreSQL; every model response is an offline injected fixture."""
import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
import test_context_postgres as context_fixtures
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select
from test_context_postgres import (
    LocalFactory,
    chat,
    request,
    worker_for,
)
from test_context_runtime import request as context_input
from test_m3_host import host_for, prepared
from test_postgres import postgres_environment  # noqa: F401

from omniharness.context_host import DurableSummaryExecutor, model_fingerprint
from omniharness.context_policy import ContextPolicyV2
from omniharness.context_runtime import ContextPreparationError, ContextScope, prepare_context
from omniharness.m3_agents import AgentBudgetExceeded
from omniharness.m3_host import AgentOperationNeedsReview
from omniharness.models import ArtifactVersion, OperationRecord, Run, RunBudget, RunEvent
from omniharness.providers import ProviderError, summary_capability_binding
from omniharness.worker import WorkerLock

context_pg = context_fixtures.context_pg


def configured(env, *, cap=50, capability=None, policy=None):
    run_id, snapshot = chat(env, cap=cap)
    policy = policy or ContextPolicyV2()
    snapshot["context_policy"] = {"policy": asdict(policy)}
    if capability is not None:
        snapshot.update(adapter_type="openai-compatible", base_url="https://fixture.invalid/v1")
        snapshot["capabilities"] = {"summary_non_thinking": {
            "schema_version": 1, "binding": summary_capability_binding(snapshot),
            "status": capability, "parameter_accepted": True, "non_thinking_observed": True,
            "observation_basis": "reported_zero_reasoning",
        }}
    with env.factory.begin() as db:
        db.get(Run, run_id).config_snapshot = snapshot
    return run_id, snapshot


def call_for(run_id, snapshot, *, salt="first"):
    policy = ContextPolicyV2(**snapshot["context_policy"]["policy"])
    return replace(request(run_id, snapshot, salt=salt), policy_version=policy.version,
                   policy_fingerprint=model_fingerprint(asdict(policy)))


def response(*, finish="stop", tokens=20, text="Concise public continuation note."):
    return AIMessage(content=text, response_metadata={"finish_reason": finish},
        additional_kwargs={"reasoning_content": "PRIVATE NEVER PERSIST"},
        usage_metadata=None if tokens is None else {
            "input_tokens": 100, "output_tokens": tokens, "total_tokens": 100 + tokens})


class SequenceFactory(LocalFactory):
    def __init__(self, *outputs):
        super().__init__()
        self.outputs = list(outputs)

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        index = len(self.calls) - 1
        await asyncio.sleep(0)
        output = self.outputs[index]
        if isinstance(output, BaseException):
            raise output
        return output


@pytest.mark.asyncio
async def test_recovery_is_two_durable_calls_and_restart_replays_both_without_new_cost(context_pg):
    env = context_pg
    run_id, snapshot = configured(env, capability="supported")
    call = call_for(run_id, snapshot)
    local = SequenceFactory(response(finish="length", tokens=4000), response(tokens=5000))
    with WorkerLock(env.engine, env.settings) as owner:
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        first = await executor.execute(call)
    with WorkerLock(env.engine, env.settings) as owner:
        second = await DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local).execute(call)
    assert first == second and len(local.calls) == 2
    assert first.recovery_of == call.invocation_id and first.request_binding == call.binding_fingerprint
    assert first.invocation_id != call.invocation_id and first.output_limit == 8000
    assert [cfg.max_output_tokens for cfg in local.configs] == [4000, 8000]
    assert all(cfg.parameters["thinking"] == {"type": "disabled"} for cfg in local.configs)
    assert snapshot["max_output_tokens"] == 100000 and snapshot["parameters"] == {}
    with env.factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 2
        rows = list(db.scalars(select(ArtifactVersion).where(ArtifactVersion.run_id == run_id)))
        assert len(rows) == 2
        evidence = [json.loads(executor.store.read(row).decode()) for row in rows]
        assert {item["attempt"] for item in evidence} == {0, 1}
        assert "PRIVATE" not in str(evidence)
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id,
            RunEvent.event_type == "context.summary_recovery_reserved")))
        assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("finish,tokens", [("length", None), ("length", 3999), (None, 4000), ("stop", 4001)])
async def test_unknown_or_non_cap_failure_cannot_authorize_recovery(context_pg, finish, tokens):
    env = context_pg
    run_id, snapshot = configured(env)
    local = SequenceFactory(response(finish=finish, tokens=tokens))
    with WorkerLock(env.engine, env.settings) as owner:
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        for _ in range(2):
            with pytest.raises(ContextPreparationError):
                await executor.execute(call_for(run_id, snapshot))
    assert len(local.calls) == 1
    with env.factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 1


@pytest.mark.asyncio
async def test_recovery_also_truncated_stops_and_reentry_never_creates_third_attempt(context_pg):
    env = context_pg
    run_id, snapshot = configured(env)
    local = SequenceFactory(response(finish="length", tokens=4000), response(finish="length", tokens=8000))
    with WorkerLock(env.engine, env.settings) as owner:
        for _ in range(2):
            executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
            with pytest.raises(ContextPreparationError, match="one permitted"):
                await executor.execute(call_for(run_id, snapshot))
    assert len(local.calls) == 2


@pytest.mark.asyncio
async def test_unknown_recovery_result_is_never_repeated(context_pg):
    env = context_pg
    run_id, snapshot = configured(env)
    local = SequenceFactory(response(finish="length", tokens=4000), TimeoutError("PRIVATE"))
    with WorkerLock(env.engine, env.settings) as owner:
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        with pytest.raises(ProviderError):
            await executor.execute(call_for(run_id, snapshot))
        with pytest.raises(AgentOperationNeedsReview):
            await executor.execute(call_for(run_id, snapshot))
    assert len(local.calls) == 2
    with env.factory() as db:
        assert {row.status for row in db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id))} == {
            "completed", "uncertain"}


@pytest.mark.asyncio
async def test_two_summary_hosts_compete_for_only_one_recovery_slot_per_run(context_pg):
    env = context_pg
    run_id, snapshot = configured(env)
    locals_ = [SequenceFactory(response(finish="length", tokens=4000), response()) for _ in range(2)]
    with WorkerLock(env.engine, env.settings) as owner:
        results = await asyncio.gather(*[
            DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local).execute(
                call_for(run_id, snapshot, salt=str(index))) for index, local in enumerate(locals_)],
            return_exceptions=True)
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    errors = [item for item in results if isinstance(item, ContextPreparationError)]
    assert len(errors) == 1 and errors[0].code == "SUMMARY_RECOVERY_USED"
    assert sum(len(local.calls) for local in locals_) == 3
    with env.factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 3
        assert len(list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id,
            RunEvent.event_type == "context.summary_recovery_reserved")))) == 1


@pytest.mark.asyncio
async def test_recovery_respects_shared_budget_and_unknown_capability_inherits_parameters(context_pg):
    env = context_pg
    run_id, snapshot = configured(env, cap=1, capability="unknown")
    local = SequenceFactory(response(finish="length", tokens=4000))
    with WorkerLock(env.engine, env.settings) as owner:
        executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
        with pytest.raises(AgentBudgetExceeded):
            await executor.execute(call_for(run_id, snapshot))
    assert len(local.calls) == 1 and all(cfg.parameters == {} for cfg in local.configs)
    with env.factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 1
        assert not list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id,
            RunEvent.event_type == "context.summary_recovery_reserved")))


@pytest.mark.asyncio
async def test_stale_verified_capability_cannot_disable_thinking(context_pg):
    env = context_pg
    run_id, snapshot = configured(env, capability="supported")
    snapshot["parameters"] = {"temperature": .2}
    with env.factory.begin() as db:
        db.get(Run, run_id).config_snapshot = snapshot
    local = SequenceFactory(response())
    with WorkerLock(env.engine, env.settings) as owner:
        await DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local).execute(
            call_for(run_id, snapshot))
    assert local.configs[0].parameters == {"temperature": .2}


@pytest.mark.asyncio
async def test_agent_host_recovery_uses_same_fenced_budget_and_artifact_protocol(context_pg):
    env = context_pg
    run_id = prepared(env.settings, env.engine, env.factory, env.client)
    with env.factory.begin() as db:
        run = db.get(Run, run_id)
        frozen = deepcopy(run.config_snapshot)
        snapshot = frozen["agent"]["roles"]["think"]["model"]
        snapshot["context_policy"] = {"policy": asdict(ContextPolicyV2())}
        run.config_snapshot = frozen
    call = call_for(run_id, snapshot)
    call = replace(call, scope=replace(call.scope, role="coordinator"))
    local = SequenceFactory(response(finish="length", tokens=4000), response())
    with WorkerLock(env.engine, env.settings) as owner:
        worker = worker_for(env, owner)
        host = host_for(worker, run_id)
        first = await DurableSummaryExecutor(worker, run_id, snapshot, host=host, model_factory=local).execute(call)
        second = await DurableSummaryExecutor(worker, run_id, snapshot, host=host, model_factory=local).execute(call)
    assert first == second and len(local.calls) == 2
    with env.factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 2
        assert len(list(db.scalars(select(ArtifactVersion).where(ArtifactVersion.run_id == run_id)))) == 2


@pytest.mark.asyncio
async def test_no_gain_evidence_is_reused_after_worker_restart_without_another_paid_summary(context_pg):
    env = context_pg
    run_id, snapshot = configured(env)
    value = replace(context_input(count=12, policy=ContextPolicyV2()),
        scope=ContextScope(run_id, "chat", snapshot["id"], model_fingerprint(snapshot), "node-1", "call-1"))
    local = SequenceFactory(response(text="字" * 12000, tokens=3000))
    results = []
    for node in ("node-1", "node-2"):
        with WorkerLock(env.engine, env.settings) as owner:
            executor = DurableSummaryExecutor(worker_for(env, owner), run_id, snapshot, model_factory=local)
            results.append(await prepare_context(replace(value, scope=replace(value.scope, checkpoint_ns=node)),
                model=FakeListChatModel(responses=["must not dispatch"]), summary_executor=executor))
    assert all(not result.compacted for result in results)
    assert all(result.decisions == ("insufficient_compression_gain",) for result in results)
    assert results[0].summary_refs == results[1].summary_refs and len(local.calls) == 1
    with env.factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 1
