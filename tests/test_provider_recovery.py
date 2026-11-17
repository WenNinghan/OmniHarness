"""Accounted recovery against real fenced hosts; no external model requests."""
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from test_m3_host import host_for, identity, prepared

from omniharness.m3_agents import AgentBudgetExceeded, AgentExternalCallFailed
from omniharness.m3_host import AgentCancellationRequested, AgentOperationNeedsReview
from omniharness.models import OperationRecord, Run, RunBudget, RunBudgetReservation
from omniharness.provider_recovery import attempt_identity, classify_failure, run_provider_call
from omniharness.worker import Worker, WorkerLock


def policy(*, delay=0, maximum=1):
    return SimpleNamespace(recovery=SimpleNamespace(max_attempts=2, backoff_seconds=delay,
                                                    max_backoff_seconds=maximum))


def http_error(status, headers=None):
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    return httpx.HTTPStatusError("private-error-canary", request=request,
                                response=httpx.Response(status, request=request, headers=headers))


async def call(host, root, dispatch, configuration=None, callback=None):
    return await run_provider_call(host=host, role="coordinator", root=root,
        policy=configuration or policy(), dispatch=dispatch,
        on_reserved_factory=callback or (lambda _: None))


@pytest.mark.parametrize("error,code,retry", [
    (httpx.PoolTimeout("secret"), "POOL_NOT_DISPATCHED", True),
    (httpx.ConnectTimeout("secret"), "CONNECT_NOT_DISPATCHED", True),
    (httpx.ConnectError("secret"), "CONNECT_NOT_DISPATCHED", True),
    (http_error(429), "HTTP_429_REJECTED", True),
    (http_error(403), "HTTP_REQUEST_REJECTED", False),
    (http_error(502), None, None),
    (httpx.ReadTimeout("secret"), None, None),
    (ConnectionError("secret"), None, None),
])
def test_only_typed_proven_failures_are_classified(error, code, retry):
    result = classify_failure(error)
    assert ((result.code, result.retryable) if result else (None, None)) == (code, retry)


def test_cause_chain_and_partial_progress_are_conservative():
    wrapped = RuntimeError("secret")
    wrapped.__cause__ = httpx.ConnectError("secret")
    assert classify_failure(wrapped).retryable
    wrapped.omni_response_started = True
    assert classify_failure(wrapped) is None
    partial = http_error(429)
    partial.omni_response_started = True
    assert classify_failure(partial) is None
    assert attempt_identity(identity("run"), 2).checkpoint_ns == ""
    root = replace(identity("run"), checkpoint_ns="task|tools:one", task_id="graph-task")
    child = attempt_identity(root, 2)
    assert (child.checkpoint_ns, child.task_id) == (root.checkpoint_ns, root.task_id)
    assert child.invocation_id != root.invocation_id


@pytest.mark.asyncio
async def test_two_requests_two_reservations_and_crash_reentry_exactly_replays_child(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker, count, callbacks = Worker(settings, engine), 0, []
    root, expected = identity(run_id), {"message": {"content": "exact persisted response"}}
    async def dispatch(attempt):
        nonlocal count
        count += 1
        if count == 1:
            raise http_error(429)
        return expected
    def callback(attempt):
        def capture(db, operation):
            callbacks.append(attempt.invocation_id)
        return capture
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        result, actual = await call(host_for(worker, run_id), root, dispatch, callback=callback)
        assert result == expected and actual == attempt_identity(root, 2)
        # Simulate graph checkpoint not being committed after child completion.
        replay, replay_identity = await call(host_for(worker, run_id), root, dispatch, callback=callback)
        assert (replay, replay_identity, count) == (expected, actual, 2)
    assert callbacks == [root.invocation_id, actual.invocation_id]
    with factory() as db:
        rows = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id)))
        first = next(row for row in rows if row.status == "failed")
        child = next(row for row in rows if row.status == "completed")
        assert first.result["provider_recovery"]["child_invocation_id"] == actual.invocation_id
        assert child.result["provider_recovery"]["parent_invocation_id"] == root.invocation_id
        assert len(list(db.scalars(select(RunBudgetReservation).where(
            RunBudgetReservation.run_id == run_id)))) == 2
        assert db.get(RunBudget, run_id).model_calls == 2
        assert "private-error-canary" not in json.dumps([row.result for row in rows])


@pytest.mark.parametrize("error,status", [(http_error(403), "failed"),
    (http_error(502), "uncertain"), (httpx.ReadTimeout("secret"), "uncertain")])
@pytest.mark.asyncio
async def test_rejected_or_ambiguous_requests_never_retry(environment, error, status):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker, count = Worker(settings, engine), 0
    async def dispatch(attempt):
        nonlocal count
        count += 1
        raise error
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = host_for(worker, run_id)
        with pytest.raises(AgentExternalCallFailed):
            await call(host, identity(run_id), dispatch)
        with pytest.raises(AgentOperationNeedsReview):
            await call(host, identity(run_id), dispatch)
    assert count == 1
    with factory() as db:
        assert db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id)).status == status
        assert db.get(RunBudget, run_id).model_calls == 1


@pytest.mark.asyncio
async def test_retry_after_above_allowed_wait_is_not_shortened(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker, count = Worker(settings, engine), 0
    async def dispatch(attempt):
        nonlocal count
        count += 1
        raise http_error(429, {"Retry-After": "30"})
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        with pytest.raises(AgentExternalCallFailed):
            await call(host_for(worker, run_id), identity(run_id), dispatch, policy(maximum=0.01))
    assert count == 1
    with factory() as db:
        row = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
        assert row.result["provider_recovery"]["retry_allowed"] is False


@pytest.mark.asyncio
async def test_exhausted_budget_does_not_link_or_dispatch_child(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    with factory.begin() as db:
        db.get(RunBudget, run_id).max_model_calls = 1
    worker, count = Worker(settings, engine), 0
    async def dispatch(attempt):
        nonlocal count
        count += 1
        raise http_error(429)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        with pytest.raises(AgentBudgetExceeded):
            await call(host_for(worker, run_id), identity(run_id), dispatch)
    assert count == 1
    with factory() as db:
        row = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
        assert "child_invocation_id" not in row.result["provider_recovery"]
        assert db.get(RunBudget, run_id).model_calls == 1


@pytest.mark.asyncio
async def test_backoff_releases_locks_and_rechecks_cancel_before_dispatch(environment, monkeypatch):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker, count = Worker(settings, engine), 0
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = host_for(worker, run_id)
        async def sleep(seconds):
            assert not host.serial.locked()
            assert seconds > 0
            with factory.begin() as db:
                db.get(Run, run_id).status = "cancel_requested"
        monkeypatch.setattr("omniharness.provider_recovery.asyncio.sleep", sleep)
        async def dispatch(attempt):
            nonlocal count
            count += 1
            raise http_error(429)
        with pytest.raises(AgentCancellationRequested):
            await call(host, identity(run_id), dispatch, policy(delay=0.1))
    assert count == 1
    with factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 1


@pytest.mark.asyncio
async def test_child_link_and_reservation_rollback_together(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker, root = Worker(settings, engine), identity(run_id)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = host_for(worker, run_id)
        await host.reserve_provider_attempt("coordinator", root, root, 2)
        await host.fail_provider_attempt("coordinator", root, root,
            failure=classify_failure(http_error(429)), retry_allowed=True, delay=0,
            error=http_error(429), transport_mode="ainvoke_default")
        def crash(db, operation):
            raise RuntimeError("simulated crash during reservation")
        with pytest.raises(RuntimeError):
            await host.reserve_provider_attempt("coordinator", root, attempt_identity(root, 2), 2,
                                                on_reserved=crash)
        with factory() as db:
            row = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
            assert "child_invocation_id" not in row.result["provider_recovery"]
            assert db.get(RunBudget, run_id).model_calls == 1
        # New host resumes the persisted known failure and safely creates child.
        async def dispatch(attempt):
            return {"message": {"content": "recovered after reservation rollback"}}
        saved, actual = await call(host_for(worker, run_id), root, dispatch)
        assert saved["message"]["content"] == "recovered after reservation rollback"
        assert actual == attempt_identity(root, 2)


@pytest.mark.asyncio
async def test_running_child_blocks_reentry(environment):
    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    worker, root = Worker(settings, engine), identity(run_id)
    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        host = host_for(worker, run_id)
        await host.reserve_provider_attempt("coordinator", root, root, 2)
        await host.fail_provider_attempt("coordinator", root, root,
            failure=classify_failure(http_error(429)), retry_allowed=True, delay=0,
            error=http_error(429), transport_mode="ainvoke_default")
        await host.reserve_provider_attempt("coordinator", root, attempt_identity(root, 2), 2)
        async def forbidden(attempt):
            pytest.fail("A recorded dispatch must never execute again")
        with pytest.raises(AgentOperationNeedsReview):
            await call(host_for(worker, run_id), root, forbidden)


def enable_frozen_recovery(worker, run_id, *, workflow=None):
    from omniharness.m4_contract import M4_V10_PLUGIN_VERSIONS, workspace_role_versions
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        snapshot = deepcopy(run.config_snapshot)
        if workflow:
            snapshot["agent"]["workflow_version"] = workflow
            snapshot["agent"]["plugin_versions"] = M4_V10_PLUGIN_VERSIONS
            versions = workspace_role_versions(workflow)
            for role in snapshot["agent"]["roles"]:
                snapshot["agent"]["roles"][role]["role_version"] = versions[role]
            run.workflow_version = workflow
        for role in snapshot["agent"]["roles"].values():
            role["model"]["parameters"] = {**role["model"].get("parameters", {}),
                "omni_request_policy": {"version": 1, "recovery": {
                    "max_attempts": 2, "backoff_seconds": 0}}}
        run.config_snapshot = snapshot


@pytest.mark.asyncio
async def test_actual_collaboration_graph_recovers_stage_and_execution_without_repeating_tools(environment, tmp_path):
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from test_m4_collaboration import InspectStageModel, graph_v4, prepare_v4
    from test_m4_runtime import ToolModel

    from omniharness.stage_contract import STAGE_KEYS

    worker, run_id, workspace, broker, sandbox, project = prepare_v4(environment, tmp_path)
    enable_frozen_recovery(worker, run_id)
    stage_calls, execution_calls = [], []
    class RecoverStage(InspectStageModel):
        async def ainvoke(self, messages):
            if self.stage == "plan":
                stage_calls.append(self.stage)
                if len(stage_calls) == 1:
                    raise http_error(429)
            return await super().ainvoke(messages)
    class RecoverExecution(ToolModel):
        async def _agenerate(self, messages, **kwargs):
            execution_calls.append("execute")
            if len(execution_calls) == 1:
                raise httpx.ConnectTimeout("private-error-canary")
            return self._generate(messages, **kwargs)
    with WorkerLock(worker.engine, worker.settings) as lock:
        worker.lock = lock
        graph, scheduler = graph_v4(worker, run_id, workspace, broker, sandbox, InMemorySaver(),
            stages={role: RecoverStage(role) for role in STAGE_KEYS}, executor_model=RecoverExecution())
        try:
            result = await graph.ainvoke({"messages": [HumanMessage(content="修复并测试")]},
                {"configurable": {"thread_id": run_id}, "recursion_limit": 100}, durability="sync")
        finally:
            await scheduler.aclose()
    assert result["messages"][-1].content
    assert len(stage_calls) == 2 and len(execution_calls) == 5
    assert len(sandbox.calls) == 1
    assert (project / "main.py").read_text() == "print(42)\n"
    with worker.sessions() as db:
        rows = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id,
            OperationRecord.node_id.like("agent:%:model_calls"))))
        assert sum(row.status == "failed" for row in rows) == 2
        assert sum(row.status == "uncertain" for row in rows) == 0
        assert db.get(RunBudget, run_id).model_calls == len(rows)


@pytest.mark.asyncio
async def test_v10_closure_rejects_recovery_before_spending_child_budget(environment, tmp_path):
    from test_m4_collaboration import prepare_v4

    from omniharness.m4_contract import M4_V10_PLUGIN_VERSIONS, workspace_role_versions
    from omniharness.m4_runtime import M4AgentHost
    from omniharness.stage_contract import STAGE_ROLE_BINDINGS

    worker, run_id, *_ = prepare_v4(environment, tmp_path)
    enable_frozen_recovery(worker, run_id, workflow="stage-agent-v10")
    root = replace(identity(run_id), checkpoint_ns="child-checkpoint", task_id="graph-task")
    count = 0
    async def dispatch(attempt):
        nonlocal count
        count += 1
        raise http_error(429)
    with WorkerLock(worker.engine, worker.settings) as lock:
        worker.lock = lock
        host = M4AgentHost(worker, run_id, "stage-agent-v10", role_versions=workspace_role_versions("stage-agent-v10"),
            plugin_versions=M4_V10_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
        with pytest.raises(AgentExternalCallFailed):
            await run_provider_call(host=host, role="execute", root=root, policy=policy(), dispatch=dispatch,
                                    on_reserved_factory=lambda _: None)
        with pytest.raises(Exception, match="unavailable for V10 closure"):
            await host.reserve_provider_attempt("execute", root, attempt_identity(root, 2), 2)
    assert count == 1
    with worker.sessions() as db:
        row = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
        assert row.status == "failed"
        assert row.result["provider_recovery"]["retry_blocked_reason"] == "V10_CLOSURE_UNSUPPORTED"
        assert db.get(RunBudget, run_id).model_calls == 1


@pytest.mark.asyncio
async def test_low_level_child_identity_still_requires_its_own_v10_scope_mac(environment, tmp_path, monkeypatch):
    """MAC safety only: product V10 recovery remains explicitly disabled."""
    from langchain_core.messages import AIMessage
    from test_m4_collaboration import prepare_v4

    from omniharness import m4_collaboration
    from omniharness.m4_contract import M4_V10_PLUGIN_VERSIONS, workspace_role_versions
    from omniharness.m4_runtime import M4AgentHost
    from omniharness.stage_contract import STAGE_ROLE_BINDINGS
    from omniharness.v10_closure import model_scope_mac

    worker, run_id, *_ = prepare_v4(environment, tmp_path)
    enable_frozen_recovery(worker, run_id, workflow="stage-agent-v10")
    root = replace(identity(run_id), checkpoint_ns="child-checkpoint", task_id="graph-task")
    child = attempt_identity(root, 2)
    monkeypatch.setattr(m4_collaboration, "current_task_scope", lambda: {"id": "logical-task"})
    def capture(db, operation):
        operation.result = {**operation.result, "v10_model_scope": {
            "run_id": run_id, "task_id": "logical-task", "checkpoint_ns": child.checkpoint_ns,
            "graph_task_id": child.task_id, "invocation_id": child.invocation_id}}
    with WorkerLock(worker.engine, worker.settings) as lock:
        worker.lock = lock
        host = M4AgentHost(worker, run_id, "stage-agent-v10", role_versions=workspace_role_versions("stage-agent-v10"),
            plugin_versions=M4_V10_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
        # Deliberately exercise the pre-existing base primitive, not the
        # product's disabled V10 recovery reservation API.
        await host.reserve_invocation("model_calls", "execute", None, child, on_reserved=capture)
        evidence = {"messages": [AIMessage(content="saved", response_metadata={"finish_reason": "stop"})
                                 .model_dump(mode="json")]}
        await host.finish_invocation("model_calls", "execute", None, child, result=evidence)
        assert await host.replay_invocation("model_calls", "execute", None, child) == evidence
        with worker.sessions.begin() as db:
            row = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id))
            altered = deepcopy(row.result)
            altered["v10_model_scope"]["invocation_id"] = root.invocation_id
            altered["v10_model_scope_mac"] = model_scope_mac(worker.settings, db.get(Run, run_id),
                row.operation_id, altered["v10_model_scope"], altered["response"])
            row.result = altered
        with pytest.raises(AgentOperationNeedsReview):
            await host.replay_invocation("model_calls", "execute", None, child)


@pytest.mark.asyncio
async def test_native_admission_error_exposes_only_bounded_diagnostic(environment, tmp_path):
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.messages import HumanMessage
    from test_m4_collaboration import prepare_v4
    from test_m4_runtime import ToolModel

    from omniharness.context_runtime import ContextPreparationError
    from omniharness.m4_contract import COLLAB_STAGE_VERSIONS, COLLAB_WORKFLOW_VERSION, M4_PLUGIN_VERSIONS
    from omniharness.m4_runtime import ExecutionBoundary, M4AgentHost
    from omniharness.stage_contract import STAGE_ROLE_BINDINGS

    worker, run_id, *_ = prepare_v4(environment, tmp_path)
    with WorkerLock(worker.engine, worker.settings) as lock:
        worker.lock = lock
        host = M4AgentHost(worker, run_id, COLLAB_WORKFLOW_VERSION, role_versions=COLLAB_STAGE_VERSIONS,
            plugin_versions=M4_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
        boundary = ExecutionBoundary(host=host, gateway=None, scheduler=None)
        request = ModelRequest(model=ToolModel(), messages=[HumanMessage(content="private-input-canary")]*101,
            runtime=SimpleNamespace(execution_info=SimpleNamespace(checkpoint_ns="child", task_id="task",
                                                                  thread_id=run_id)))
        async def forbidden(request):
            pytest.fail("Rejected native input must not dispatch")
        with pytest.raises(ContextPreparationError) as captured:
            await boundary.awrap_model_call(request, forbidden)
        assert captured.value.diagnostic == {"reason": "message_count", "observed": 101,
                                             "limit": 100, "window_known": False}
        assert "private-input-canary" not in json.dumps(captured.value.diagnostic)
    with worker.sessions() as db:
        assert db.get(RunBudget, run_id).model_calls == 0
