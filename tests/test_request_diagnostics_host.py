"""Independent host persistence checks, with loopback SDK faults only."""
import json
from copy import deepcopy

import httpx
import pytest
from sqlalchemy import select
from test_m3_host import host_for, identity, prepared
from test_request_timeouts import Scenario, configured
from test_request_timeouts import fault_server as shared_fault_server
from test_resilience_independent_review import freeze_request_config

from omniharness.m3_agents import AgentExternalCallFailed, _raise_sanitized
from omniharness.m3_host import AgentOperationNeedsReview
from omniharness.m3_worker import _review, _root_diagnostic
from omniharness.models import OperationRecord, RunBudget, RunEvent
from omniharness.provider_recovery import classify_failure
from omniharness.providers import build_chat_model
from omniharness.request_diagnostics import failure_diagnostic, safe_transport_diagnostic
from omniharness.worker import Worker, WorkerLock

CANARY = 'diagnostic-private-key-url-body-canary'


@pytest.fixture
def fault_server(monkeypatch):
    yield from shared_fault_server.__wrapped__(monkeypatch)


def evidence(factory, run_id):
    with factory() as db:
        operation = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
        return operation.status, deepcopy(operation.result), [
            {'event_type': item.event_type, 'payload': deepcopy(item.payload)} for item in events]


@pytest.mark.parametrize('native,plural', [(False, False), (True, True)])
async def test_sdk_success_trace_is_saved_with_exact_response_and_completion_event(
        environment, fault_server, native, plural):
    endpoint, scenarios, requests = fault_server
    scenarios[CANARY] = Scenario(steps=[(0, {'content': 'safe answer'})])
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    config = configured(endpoint)
    config.api_key = CANARY
    freeze_request_config(factory, run_id, config)
    worker, root = Worker(settings, engine), identity(run_id)
    async with httpx.AsyncClient(trust_env=False) as wire:
        model = build_chat_model(config, http_async_client=wire, native_streaming=native)
        with WorkerLock(engine, settings) as lock:
            worker.lock = lock
            host = host_for(worker, run_id)
            assert await host.reserve_invocation('model_calls', 'coordinator', None, root)
            response = await model.ainvoke([('user', CANARY)])
            saved = {'messages': [response.model_dump(mode='json')]} if plural else {
                'message': response.model_dump(mode='json')}
            trace = safe_transport_diagnostic(response.response_metadata['omni_transport_diagnostic'])
            assert trace['response_status_code'] == 200
            assert trace['milestones_ms']['request_start'] >= 0
            assert trace['milestones_ms']['response_headers'] >= 0
            if native:
                assert trace['milestones_ms']['first_chunk'] >= 0
                assert trace['response_started'] is True
            await host.finish_invocation('model_calls', 'coordinator', None, root, result=saved)
            assert await host.replay_invocation('model_calls', 'coordinator', None, root) == saved
    status, operation, events = evidence(factory, run_id)
    assert status == 'completed' and operation['response'] == saved
    assert operation['diagnostic'] == {'transport_diagnostic': trace}
    complete = [item for item in events if item['event_type'] == 'agent.model_completed']
    assert len(complete) == 1 and complete[0]['payload']['diagnostic'] == operation['diagnostic']
    assert CANARY not in json.dumps(events)
    assert CANARY not in json.dumps(operation['diagnostic'])
    assert len(requests) == 1


async def test_already_uncertain_record_keeps_sdk_cause_and_trace_after_worker_review(environment, fault_server):
    endpoint, scenarios, requests = fault_server
    scenarios[CANARY] = Scenario(delay=0.35)
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    config = configured(endpoint, read_seconds=0.08)
    config.api_key = CANARY
    freeze_request_config(factory, run_id, config)
    worker, root = Worker(settings, engine), identity(run_id)
    async with httpx.AsyncClient(trust_env=False) as wire:
        model = build_chat_model(config, http_async_client=wire)
        with WorkerLock(engine, settings) as lock:
            worker.lock = lock
            host = host_for(worker, run_id)
            assert await host.reserve_invocation('model_calls', 'coordinator', None, root)
            try:
                await model.ainvoke([('user', CANARY)])
            except Exception as original:
                original.args = (CANARY,)
                assert classify_failure(original) is None
                await host.mark_invocation_uncertain_with_diagnostics(
                    'model_calls', 'coordinator', None, root, error=original, transport_mode='ainvoke_default')
                before = evidence(factory, run_id)[1]['diagnostic']
                try:
                    _raise_sanitized(original)
                except AgentExternalCallFailed as sanitized:
                    diagnostic = _root_diagnostic(sanitized)
            else:
                pytest.fail('Read fault must not return a model response')
            assert before['error_code'] == 'PROVIDER_TIMEOUT'
            assert any(item['type'] == 'httpx.ReadTimeout'
                       for item in before['transport_diagnostic']['exception_chain'])
            assert any(item['type'] == 'httpx.ReadTimeout'
                       for item in diagnostic['transport_diagnostic']['exception_chain'])
            with pytest.raises(AgentOperationNeedsReview):
                await host.replay_invocation('model_calls', 'coordinator', None, root)
            _review(worker, run_id, 'MODEL_INTERRUPTED', 'Safe public interruption', diagnostic=diagnostic)
    status, operation, events = evidence(factory, run_id)
    assert status == 'uncertain' and operation['diagnostic'] == before
    pending = [item for item in events if item['event_type'] == 'run.pending_review']
    assert len(pending) == 1 and pending[0]['payload']['diagnostic'] == diagnostic
    assert CANARY not in json.dumps(events)
    assert CANARY not in json.dumps(operation['diagnostic'])
    assert len(requests) == 1
    with factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 1


async def test_known_429_failure_trace_is_saved_without_promoting_it_to_unknown(environment, fault_server):
    endpoint, scenarios, requests = fault_server
    scenarios[CANARY] = Scenario(status=429)
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    config = configured(endpoint)
    freeze_request_config(factory, run_id, config)
    worker, root = Worker(settings, engine), identity(run_id)
    async with httpx.AsyncClient(trust_env=False) as wire:
        model = build_chat_model(config, http_async_client=wire)
        with WorkerLock(engine, settings) as lock:
            worker.lock = lock
            host = host_for(worker, run_id)
            assert await host.reserve_provider_attempt('coordinator', root, root, 1)
            with pytest.raises(Exception) as caught:
                await model.ainvoke([('user', CANARY)])
            error = caught.value
            classified = classify_failure(error)
            assert classified.code == 'HTTP_429_REJECTED'
            await host.fail_provider_attempt('coordinator', root, root, failure=classified,
                retry_allowed=False, delay=0, error=error, transport_mode='ainvoke_default')
    status, operation, events = evidence(factory, run_id)
    assert status == 'failed'
    assert operation['diagnostic']['transport_diagnostic']['response_status_code'] == 429
    failed = [item for item in events if item['event_type'] == 'agent.model_failed']
    assert len(failed) == 1 and failed[0]['payload']['diagnostic'] == operation['diagnostic']
    assert CANARY not in json.dumps(events) and len(requests) == 1


@pytest.mark.parametrize('error', [httpx.ReadTimeout(CANARY), httpx.ConnectTimeout(CANARY)])
def test_diagnostic_observation_does_not_change_typed_recovery_evidence(error):
    before = classify_failure(error)
    error.omni_transport_diagnostic = {'schema_version': 1, 'response_started': False,
        'exception_chain': [{'type': 'httpx.ConnectTimeout', 'relation': 'cause'}],
        'url': CANARY, 'headers': {'Authorization': CANARY}, 'body': CANARY,
        'request_id_sha256': CANARY}
    diagnostic = failure_diagnostic(error, 'ainvoke_default')
    assert classify_failure(error) == before
    assert CANARY not in json.dumps(diagnostic)


async def test_broken_success_observer_cannot_erase_a_confirmed_model_response(environment, monkeypatch):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker, root = Worker(settings, engine), identity(run_id)
    def broken(result):
        raise RuntimeError(CANARY)
    monkeypatch.setattr('omniharness.request_diagnostics.success_diagnostic', broken)
    saved = {'message': {'content': 'confirmed response'}}
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = host_for(worker, run_id)
        await host.reserve_invocation('model_calls', 'coordinator', None, root)
        await host.finish_invocation('model_calls', 'coordinator', None, root, result=saved)
        assert await host.replay_invocation('model_calls', 'coordinator', None, root) == saved
    status, operation, events = evidence(factory, run_id)
    assert status == 'completed' and operation['response'] == saved
    assert CANARY not in json.dumps(events)


async def test_broken_failure_observer_cannot_prevent_unknown_persistence(environment, monkeypatch):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker, root = Worker(settings, engine), identity(run_id)
    def broken(error, mode):
        raise RuntimeError(CANARY)
    monkeypatch.setattr('omniharness.request_diagnostics.failure_diagnostic', broken)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = host_for(worker, run_id)
        await host.reserve_invocation('model_calls', 'coordinator', None, root)
        await host.mark_invocation_uncertain_with_diagnostics('model_calls', 'coordinator', None, root,
            error=httpx.ReadTimeout(CANARY), transport_mode='ainvoke_default')
    status, operation, events = evidence(factory, run_id)
    assert status == 'uncertain' and 'response' not in operation
    assert CANARY not in json.dumps(events)
