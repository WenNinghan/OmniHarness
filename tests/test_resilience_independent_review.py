"""Independent real-SDK recovery review; all HTTP traffic remains loopback."""
from copy import deepcopy

import httpx
import pytest
from sqlalchemy import select
from test_m3_host import host_for, identity, prepared
from test_request_timeouts import Scenario, configured
from test_request_timeouts import fault_server as shared_fault_server

from omniharness.m3_agents import AgentExternalCallFailed
from omniharness.m3_host import AgentOperationNeedsReview
from omniharness.models import OperationRecord, Run, RunBudget, RunBudgetReservation
from omniharness.provider_recovery import attempt_identity, run_provider_call
from omniharness.providers import build_chat_model
from omniharness.request_policy import parse_request_policy
from omniharness.worker import Worker, WorkerLock


@pytest.fixture
def fault_server(monkeypatch):
    yield from shared_fault_server.__wrapped__(monkeypatch)


def freeze_request_config(factory, run_id, config):
    # This test-only freeze aligns the host binding with the exact SDK config;
    # no editable production model or provider is used.
    with factory.begin() as db:
        run = db.get(Run, run_id)
        snapshot = deepcopy(run.config_snapshot)
        model = snapshot["agent"]["roles"]["think"]["model"]
        model.update(base_url=config.base_url, model_name=config.model_name,
                     parameters=config.parameters, timeout_seconds=config.timeout_seconds)
        run.config_snapshot = snapshot


async def invoke(host, root, policy, dispatch):
    return await run_provider_call(host=host, role="coordinator", root=root, policy=policy,
                                   dispatch=dispatch, on_reserved_factory=lambda _: None)


@pytest.mark.parametrize("native", [False, True])
async def test_real_sdk_429_creates_two_http_requests_two_reservations_and_durable_child(
        environment, fault_server, native):
    endpoint, scenarios, requests = fault_server
    scenario = scenarios["recover"] = Scenario(status=429, steps=[(0, {"content": "exact wire response"})])
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    config = configured(endpoint)
    config.parameters["omni_request_policy"]["recovery"] = {
        "max_attempts": 2, "backoff_seconds": 0, "max_backoff_seconds": 1}
    policy = parse_request_policy(config.parameters)
    freeze_request_config(factory, run_id, config)
    root, worker = identity(run_id), Worker(settings, engine)
    seen = []
    async with httpx.AsyncClient(trust_env=False) as wire:
        model = build_chat_model(config, http_async_client=wire, native_streaming=native)

        async def dispatch(attempt):
            seen.append(attempt.invocation_id)
            try:
                response = await model.ainvoke([("user", "recover")])
                return {"message": response.model_dump(mode="json")}
            finally:
                scenario.status = 200

        with WorkerLock(engine, settings) as lock:
            worker.lock = lock
            saved, actual = await invoke(host_for(worker, run_id), root, policy, dispatch)
            assert actual == attempt_identity(root, 2)
            assert saved["message"]["content"] == ("exact wire response" if native else "OK")
            # The graph can crash before checkpointing this returned response.
            # A fresh host must replay the durable child, without another HTTP.
            replayed, replay_identity = await invoke(host_for(worker, run_id), root, policy, dispatch)
            assert (replayed, replay_identity) == (saved, actual)
    assert len(requests) == 2 and seen == [root.invocation_id, actual.invocation_id]
    with factory() as db:
        rows = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id)))
        first = next(row for row in rows if row.status == "failed")
        child = next(row for row in rows if row.status == "completed")
        assert first.result["provider_recovery"]["failure_class"] == "HTTP_429_REJECTED"
        assert first.result["provider_recovery"]["child_invocation_id"] == actual.invocation_id
        assert child.result["provider_recovery"]["parent_invocation_id"] == root.invocation_id
        assert db.get(RunBudget, run_id).model_calls == 2
        assert len(list(db.scalars(select(RunBudgetReservation).where(
            RunBudgetReservation.run_id == run_id)))) == 2


async def test_real_sdk_read_timeout_is_one_http_one_unknown_and_never_replayed(environment, fault_server):
    endpoint, scenarios, requests = fault_server
    scenarios["uncertain"] = Scenario(delay=0.35)
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    config = configured(endpoint, read_seconds=0.08)
    config.parameters["omni_request_policy"]["recovery"] = {"max_attempts": 2, "backoff_seconds": 0}
    freeze_request_config(factory, run_id, config)
    policy = parse_request_policy(config.parameters)
    root, worker = identity(run_id), Worker(settings, engine)
    async with httpx.AsyncClient(trust_env=False) as wire:
        model = build_chat_model(config, http_async_client=wire)

        async def dispatch(attempt):
            message = await model.ainvoke([("user", "uncertain")])
            return {"message": message.model_dump(mode="json")}

        with WorkerLock(engine, settings) as lock:
            worker.lock = lock
            with pytest.raises(AgentExternalCallFailed):
                await invoke(host_for(worker, run_id), root, policy, dispatch)
            with pytest.raises(AgentOperationNeedsReview):
                await invoke(host_for(worker, run_id), root, policy, dispatch)
    assert len(requests) == 1
    with factory() as db:
        row = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
        assert row.status == "uncertain"
        assert row.result["diagnostic"]["error_code"] == "PROVIDER_TIMEOUT"
        assert "child_invocation_id" not in row.result["provider_recovery"]
        assert db.get(RunBudget, run_id).model_calls == 1
