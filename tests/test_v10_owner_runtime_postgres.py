"""Offline real API/Worker qualification tests; no model or tool dispatch."""
# ruff: noqa: F811
import asyncio
import sys
import time
from typing import ClassVar
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import select, text
from test_m4_collaboration import prepare_v4
from test_postgres import postgres_environment  # noqa: F401
from test_v7_postgres_interaction import InteractiveStage
from test_v10_postgres_worker_e2e import NoToolsModel

from omniharness import m4_services
from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema, open_fenced_saver, register_fence
from omniharness.m3_worker import _checkpoint_url, run_agent
from omniharness.models import (
    OperationRecord,
    Run,
    RunBudget,
    RunCommand,
    ToolOperation,
    Workspace,
    WorkspacePolicy,
)
from omniharness.security import create_pairing_code
from omniharness.v10_owner_runtime import V10OwnerListener
from omniharness.worker import Worker, WorkerLock


class CorrectionAwareModel(NoToolsModel):
    correction_calls: ClassVar[int] = 0

    def _generate(self, messages, **kwargs):
        if any("你只可为用户确认的未执行动作返回工具调用" in str(item.content) for item in messages):
            type(self).correction_calls += 1
            response = AIMessage(content="", tool_calls=[{"id": "correction-1", "name": "workspace_patch",
                "args": {"path": "test.txt", "content": "mock result\n", "expected_sha256": None},
                "type": "tool_call"}], response_metadata={"finish_reason": "tool_calls"},
                usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14})
            return ChatResult(generations=[ChatGeneration(message=response)])
        return super()._generate(messages, **kwargs)


class FailingCorrectionModel(NoToolsModel):
    correction_calls: ClassVar[int] = 0

    def _generate(self, messages, **kwargs):
        if any("你只可为用户确认的未执行动作返回工具调用" in str(item.content) for item in messages):
            type(self).correction_calls += 1
            raise RuntimeError("synthetic model interruption")
        return super()._generate(messages, **kwargs)


class TwoActionCorrectionModel(NoToolsModel):
    correction_calls: ClassVar[int] = 0

    def _generate(self, messages, **kwargs):
        if any("你只可为用户确认的未执行动作返回工具调用" in str(item.content) for item in messages):
            type(self).correction_calls += 1
            response = AIMessage(content="", tool_calls=[
                {"id": "write-1", "name": "workspace_patch", "type": "tool_call",
                 "args": {"path": "test.txt", "content": "mock result\n", "expected_sha256": None}},
                {"id": "test-1", "name": "run_command", "type": "tool_call",
                 "args": {"command": ["pytest", "-q"], "cwd": "."}},
            ], response_metadata={"finish_reason": "tool_calls"})
            return ChatResult(generations=[ChatGeneration(message=response)])
        return super()._generate(messages, **kwargs)


class WaitingCorrectionModel(NoToolsModel):
    correction_calls: ClassVar[int] = 0
    correction_started: ClassVar[asyncio.Event | None] = None
    correction_cancelled: ClassVar[asyncio.Event | None] = None

    async def _agenerate(self, messages, **kwargs):
        if any("你只可为用户确认的未执行动作返回工具调用" in str(item.content) for item in messages):
            type(self).correction_calls += 1
            type(self).correction_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if type(self).correction_cancelled is not None:
                    type(self).correction_cancelled.set()
                raise
        return self._generate(messages, **kwargs)


@pytest.fixture(scope="module")
def event_loop_policy():
    return asyncio.WindowsSelectorEventLoopPolicy() if sys.platform == "win32" else asyncio.get_event_loop_policy()


@pytest.mark.asyncio
async def test_real_api_claim_and_listener_loss_fail_closed(postgres_environment, tmp_path, monkeypatch):
    settings, engine, factory, _ = postgres_environment
    with TestClient(create_app(settings, engine), base_url="http://localhost:8788",
                    headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        _, template_id, workspace, broker, sandbox, project = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            template = db.get(Run, template_id)
            model_id, workspace_id = template.model_config_id, template.workspace_id
            template.status = "completed"
            assert m4_services.release_workspace_lease(db, template_id)
            row = db.get(Workspace, workspace_id)
            row.physical_key = uuid4().hex
            row.probe = {"identity": workspace["grant"]["expected_identity"]}
            policy = db.get(WorkspacePolicy, (workspace_id, row.policy_version))
            policy.grant = {**policy.grant, "instance_id": "omni-v6-preview"}
        settings.enable_agent = settings.enable_tools = True
        settings.v6_preview = settings.interactive_preview = settings.v10_preview = True
        settings.instance_id = "omni-v6-preview"
        settings.sandbox_key_file = tmp_path / "sandbox.key"
        settings.workspace_service_key_file = tmp_path / "workspace.key"
        settings.sandbox_key_file.write_text("s" * 64, encoding="ascii")
        settings.workspace_service_key_file.write_text("w" * 64, encoding="ascii")
        settings.checkpoint_schema = "v10_owner_rt_" + uuid4().hex[:12]
        monkeypatch.setattr(m4_services, "_require_preview_fixture", lambda *_: None)
        await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=settings.checkpoint_schema)
        worker = Worker(settings, engine)
        original_process = worker.process
        worker.workspace_broker, worker.sandbox_executor = broker, sandbox
        worker.agent_model_factory = lambda role, _config: NoToolsModel() if role == "execute" else InteractiveStage(role)
        with WorkerLock(engine, settings) as lock:
            worker.lock = lock
            listener = await V10OwnerListener.start(worker)
            assert listener is not None
            worker._v10_owner_listener = listener
            try:
                # A pinned owner transaction may await graph reads longer than
                # a listener poll; the listener must not nest a transaction.
                with worker.transaction() as db:
                    assert db.connection() is lock.connection
                    await asyncio.sleep(0.35)
                    assert listener.alive and not lock.lost
                await asyncio.sleep(0.05)
                assert listener.alive and not lock.lost
                created = client.post("/api/v1/conversations", json={})
                assert created.status_code == 201, created.text
                conversation = created.json()
                body = {"content": "create test.txt", "model_config_id": model_id, "client_request_id": "new",
                        "workflow_type": "agent", "workspace_id": workspace_id, "agent_strategy": "compact",
                        "action_requirements": [{"kind": "workspace_file_write", "path": "test.txt"}]}
                response = await asyncio.to_thread(client.post,
                    f"/api/v1/conversations/{conversation['id']}/messages", json=body)
                assert response.status_code == 202, response.text
                run_id = response.json()["run_id"]
                deadline = time.monotonic() + 5
                while run_id not in listener.pending and listener.alive and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                assert run_id in listener.pending
                with factory() as db:
                    origin = db.get(Run, run_id).config_snapshot["v10_owner_origin"]
                original_transaction = worker.transaction
                def no_database_for_forged_event():
                    raise AssertionError("Forged notification reached the owner DB connection")
                monkeypatch.setattr(worker, "transaction", no_database_for_forged_event)
                listener._register({**origin, "mac": "0" * 64})
                monkeypatch.setattr(worker, "transaction", original_transaction)
                # A duplicate API request never re-signs or re-notifies.
                repeat = await asyncio.to_thread(client.post,
                    f"/api/v1/conversations/{conversation['id']}/messages", json=body)
                assert repeat.json()["run_id"] == run_id
                with factory() as db:
                    assert db.get(Run, run_id).config_snapshot["v10_owner_origin"] == origin
                assert worker.claim() == run_id
                assert await listener.confirm_claim(run_id)
                assert run_id in listener.accepted
                assert await listener.consume_owner_presence(run_id)
                assert not await listener.consume_owner_presence(run_id)
                await run_agent(worker, run_id)
                with factory() as db:
                    run = db.get(Run, run_id)
                    assert run.status == "completed", (run.error_code, run.error_message)
                    assert run.outcome["v10_actions"]["actions"][0]["task_id"] == "t1"
                    assert "step_id" not in run.outcome["v10_actions"]["actions"][0]
                    assert not (project / "test.txt").exists()

                async def new_run(request_id, overrides=None):
                    created = client.post("/api/v1/conversations", json={})
                    assert created.status_code == 201, created.text
                    conversation = created.json()
                    response = await asyncio.to_thread(client.post,
                        f"/api/v1/conversations/{conversation['id']}/messages",
                        json={**body, **(overrides or {}), "client_request_id": request_id})
                    assert response.status_code == 202, response.text
                    identity = response.json()["run_id"]
                    deadline = time.monotonic() + 5
                    while identity not in listener.pending and listener.alive and time.monotonic() < deadline:
                        await asyncio.sleep(0.05)
                    assert identity in listener.pending
                    return identity

                # Exercise the actual Worker process with the isolated mock
                # correction flag; the proposed tool must not be dispatched.
                worker._v10_mock_correction_test_only = True
                worker.agent_model_factory = lambda role, _config: (
                    CorrectionAwareModel() if role == "execute" else InteractiveStage(role))
                CorrectionAwareModel.correction_calls = 0
                mock_run_id = await new_run("worker-mock-correction")
                assert await worker.run_once() == mock_run_id
                with factory() as db:
                    mock_run = db.get(Run, mock_run_id)
                    assert mock_run.status == "pending_review", (mock_run.error_code, mock_run.error_message)
                    assert mock_run.error_code == "CORRECTION_NOT_DISPATCHED"
                    record = db.scalar(select(OperationRecord).where(OperationRecord.run_id == mock_run_id,
                        OperationRecord.node_id == "v10-correction:model_calls"))
                    assert record and record.status == "completed" and record.result["dispatch_state"] == "RECORDED"
                    assert record.result["batch_status"] == "VALIDATED_NOT_DISPATCHED"
                    assert not list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == mock_run_id)))
                assert CorrectionAwareModel.correction_calls == 1
                assert not (project / "test.txt").exists()
                worker._v10_mock_correction_test_only = False
                worker.agent_model_factory = lambda role, _config: NoToolsModel() if role == "execute" else InteractiveStage(role)
                with factory.begin() as db:
                    db.get(Run, mock_run_id).status = "completed"
                    assert m4_services.release_workspace_lease(db, mock_run_id)

                worker._v10_mock_correction_test_only = True
                worker.agent_model_factory = lambda role, _config: (
                    TwoActionCorrectionModel() if role == "execute" else InteractiveStage(role))
                TwoActionCorrectionModel.correction_calls = 0
                two_id = await new_run("worker-two-ordered-actions", {"action_requirements": [
                    {"kind": "workspace_file_write", "path": "test.txt", "client_id": "write", "depends_on": []},
                    {"kind": "sandbox_command", "argv": ["pytest", "-q"], "cwd": ".", "client_id": "test",
                     "depends_on": ["write"]},
                ]})
                assert await worker.run_once() == two_id
                with factory() as db:
                    run = db.get(Run, two_id)
                    assert run.status == "pending_review", (run.error_code, run.error_message)
                    record = db.scalar(select(OperationRecord).where(OperationRecord.run_id == two_id,
                        OperationRecord.node_id == "v10-correction:model_calls"))
                    assert record and record.status == "completed" and record.result["batch_status"] == "VALIDATED_NOT_DISPATCHED"
                    assert not list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == two_id)))
                assert TwoActionCorrectionModel.correction_calls == 1
                assert not (project / "test.txt").exists() and not sandbox.calls
                with factory.begin() as db:
                    db.get(Run, two_id).status = "completed"
                    assert m4_services.release_workspace_lease(db, two_id)

                worker._v10_mock_correction_test_only = True
                worker.agent_model_factory = lambda role, _config: (
                    FailingCorrectionModel() if role == "execute" else InteractiveStage(role))
                FailingCorrectionModel.correction_calls = 0
                failed_id = await new_run("worker-mock-correction-failure")
                assert await worker.run_once() == failed_id
                with factory() as db:
                    failed = db.get(Run, failed_id)
                    assert failed.status == "pending_review"
                    assert failed.error_code == "CORRECTION_RESULT_UNKNOWN"
                    record = db.scalar(select(OperationRecord).where(OperationRecord.run_id == failed_id,
                        OperationRecord.node_id == "v10-correction:model_calls"))
                    assert record and record.status == "uncertain" and record.result["dispatch_state"] == "UNCERTAIN"
                    assert not list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == failed_id)))
                assert FailingCorrectionModel.correction_calls == 1
                assert await worker.run_once() is None
                assert FailingCorrectionModel.correction_calls == 1
                worker._v10_mock_correction_test_only = False
                worker.agent_model_factory = lambda role, _config: NoToolsModel() if role == "execute" else InteractiveStage(role)
                with factory.begin() as db:
                    db.get(Run, failed_id).status = "completed"
                    assert m4_services.release_workspace_lease(db, failed_id)

                worker._v10_mock_correction_test_only = True
                worker.agent_model_factory = lambda role, _config: (
                    WaitingCorrectionModel() if role == "execute" else InteractiveStage(role))
                WaitingCorrectionModel.correction_calls = 0
                WaitingCorrectionModel.correction_started = asyncio.Event()
                cancelled_id = await new_run("worker-cancel-correction")
                work = asyncio.create_task(worker.run_once())
                await asyncio.wait_for(WaitingCorrectionModel.correction_started.wait(), timeout=30)
                with factory.begin() as db:
                    db.get(Run, cancelled_id).status = "cancel_requested"
                assert await asyncio.wait_for(work, timeout=10) == cancelled_id
                with factory() as db:
                    cancelled = db.get(Run, cancelled_id)
                    assert cancelled.status == "pending_review"
                    record = db.scalar(select(OperationRecord).where(OperationRecord.run_id == cancelled_id,
                        OperationRecord.node_id == "v10-correction:model_calls"))
                    assert record and record.status == "uncertain" and record.result["dispatch_state"] == "UNCERTAIN"
                    assert not list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == cancelled_id)))
                assert WaitingCorrectionModel.correction_calls == 1
                assert await worker.run_once() is None
                assert WaitingCorrectionModel.correction_calls == 1
                worker._v10_mock_correction_test_only = False
                worker.agent_model_factory = lambda role, _config: NoToolsModel() if role == "execute" else InteractiveStage(role)
                with factory.begin() as db:
                    db.get(Run, cancelled_id).status = "completed"
                    assert m4_services.release_workspace_lease(db, cancelled_id)

                # The committed DISPATCHED claim is the authorization point.
                # Revocation afterwards cancels this already-in-flight mock
                # request best effort and never replays its unknown result.
                worker._v10_mock_correction_test_only = True
                worker.agent_model_factory = lambda role, _config: (
                    WaitingCorrectionModel() if role == "execute" else InteractiveStage(role))
                WaitingCorrectionModel.correction_calls = 0
                WaitingCorrectionModel.correction_started = asyncio.Event()
                WaitingCorrectionModel.correction_cancelled = asyncio.Event()
                revoked_id = await new_run("worker-revoke-correction")
                work = asyncio.create_task(worker.run_once())
                await asyncio.wait_for(WaitingCorrectionModel.correction_started.wait(), timeout=30)
                changed = await asyncio.to_thread(client.patch, f"/api/v1/workspaces/{workspace_id}",
                                                  json={"enabled": False})
                assert changed.status_code == 200, changed.text
                assert await asyncio.wait_for(work, timeout=10) == revoked_id
                await asyncio.wait_for(WaitingCorrectionModel.correction_cancelled.wait(), timeout=5)
                with factory() as db:
                    revoked = db.get(Run, revoked_id)
                    assert revoked.status == "pending_review"
                    record = db.scalar(select(OperationRecord).where(OperationRecord.run_id == revoked_id,
                        OperationRecord.node_id == "v10-correction:model_calls"))
                    assert record and record.status == "uncertain" and record.result["dispatch_state"] == "UNCERTAIN"
                    assert not list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == revoked_id)))
                assert WaitingCorrectionModel.correction_calls == 1
                changed = await asyncio.to_thread(client.patch, f"/api/v1/workspaces/{workspace_id}",
                                                  json={"enabled": True})
                assert changed.status_code == 200, changed.text
                with factory.begin() as db:
                    db.get(Run, revoked_id).status = "completed"
                    assert m4_services.release_workspace_lease(db, revoked_id)
                worker._v10_mock_correction_test_only = False
                worker.agent_model_factory = lambda role, _config: NoToolsModel() if role == "execute" else InteractiveStage(role)

                # Build the actual v10 stage and execution graphs using the
                # sealed saver and in-memory model doubles, then inspect their
                # durable root/child states under the owner transaction.
                from typing import TypedDict

                from langchain_core.messages import HumanMessage
                from langgraph.graph import END, START, StateGraph
                from langgraph.runtime import Runtime
                from langgraph.types import Command, interrupt

                from omniharness.artifacts import ArtifactStore
                from omniharness.correction_model import (
                    dispatch_reserved_correction,
                    load_recorded_correction_batch,
                )
                from omniharness.correction_phase import prepare_correction_phase
                from omniharness.correction_reservation import (
                    CorrectionReservationUnavailable,
                    correction_invocation_id,
                    remaining_active_budget,
                    reserve_correction_model,
                )
                from omniharness.m3_delegation import HierarchicalDelegation
                from omniharness.m3_stages import build_stage_graph
                from omniharness.m4_contract import workspace_role_versions
                from omniharness.m4_runtime import M4AgentHost, build_execution_agent
                from omniharness.stage_contract import STAGE_KEYS, STAGE_ROLE_BINDINGS
                from omniharness.v10_closure import (
                    inspect_consumed_correction_phase,
                    inspect_pinned_zero_tool_inventory,
                    screen_zero_tool_correction_scope,
                )
                from omniharness.v10_correction_saver import V10CorrectionSaver
                from omniharness.v10_drain import (
                    DrainUnavailable,
                    capture_drained_execution,
                    get_drained_execution,
                    start_host_graph,
                )
                from omniharness.v10_namespace_prototype import CorrectionNamespacePrototype
                from omniharness.v10_sealed_saver import SEALED_PLUGIN_VERSIONS, V10SealedSaver

                closed_id = await new_run("zero-tool-graph")
                assert worker.claim() == closed_id
                assert await listener.confirm_claim(closed_id)
                ticket = register_fence(lock, closed_id, "stage-agent-v10", schema=settings.checkpoint_schema)
                async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as base:
                    saver = V10SealedSaver(base.conn, ticket, business_schema=postgres_environment[3], settings=settings)
                    with factory() as db:
                        frozen_workspace = db.get(Run, closed_id).config_snapshot["workspace"]
                    host = M4AgentHost(worker, closed_id, "stage-agent-v10",
                        role_versions=workspace_role_versions("stage-agent-v10"), plugin_versions=SEALED_PLUGIN_VERSIONS,
                        role_bindings=STAGE_ROLE_BINDINGS)
                    host._active_segment_started = time.monotonic()
                    with factory.begin() as db:
                        db.get(RunBudget, closed_id).active_segment_open = True
                    scheduler = HierarchicalDelegation()
                    CorrectionAwareModel.correction_calls = 0
                    executor = build_execution_agent(model=CorrectionAwareModel(), host=host, workspace=frozen_workspace,
                        broker=broker, sandbox=sandbox, artifacts=ArtifactStore(settings.data_dir / "artifacts"),
                        scheduler=scheduler)
                    graph = build_stage_graph(models={role: InteractiveStage(role) for role in STAGE_KEYS},
                        hooks=host, checkpointer=saver, workflow_version="stage-agent-v10", execution_agent=executor,
                        execution_strategy="compact")
                    config = {"configurable": {"thread_id": closed_id}, "recursion_limit": 100}
                    with pytest.raises(DrainUnavailable):
                        start_host_graph(worker, ticket=ticket, graph=graph, executor=graph,
                            saver=saver, scheduler=scheduler, payload={}, config=config)
                    with pytest.raises(DrainUnavailable):
                        start_host_graph(worker, ticket=ticket, graph=graph, executor=executor,
                            saver=saver, scheduler=HierarchicalDelegation(), payload={}, config=config)
                    try:
                        graph_task = start_host_graph(worker, ticket=ticket, graph=graph, executor=executor,
                            saver=saver, scheduler=scheduler,
                            payload={"messages": [HumanMessage(content="create test.txt")]}, config=config)
                        result = await graph_task
                        assert not result.get("__interrupt__")
                    finally:
                        await scheduler.aclose()
                    proof = await capture_drained_execution(worker, ticket=ticket, graph=graph, executor=executor,
                        saver=saver, scheduler=scheduler, graph_task=graph_task)
                    assert get_drained_execution(worker, closed_id, ticket=ticket, graph=graph,
                                                 executor=executor, saver=saver) is proof
                    with pytest.raises(DrainUnavailable):
                        get_drained_execution(worker, closed_id, ticket=ticket, graph=graph,
                                              executor=graph, saver=saver)
                    root_id = (await graph.aget_state(config)).config["configurable"]["checkpoint_id"]
                    with worker.transaction() as db:
                        passed, reason = await inspect_pinned_zero_tool_inventory(
                            worker, ticket, graph, executor, saver, root_id, owner_db=db, require_seal=True)
                        assert passed, reason
                        scoped, reason = await screen_zero_tool_correction_scope(db, worker, ticket, graph, root_id)
                    assert passed, reason
                    assert scoped, reason

                    # Revocation cannot slip between the locked policy check
                    # and the live probe. It waits for this transaction, then
                    # the next check observes the revoked workspace.
                    probe_started, finish_probe = asyncio.Event(), asyncio.Event()
                    class GatedBroker:
                        async def request(self, workspace_id, version, action, **payload):
                            probe_started.set()
                            await finish_probe.wait()
                            return await broker.request(workspace_id, version, action, **payload)
                    worker.workspace_broker = GatedBroker()
                    async def scope_with_lock():
                        with worker.transaction() as db:
                            return await screen_zero_tool_correction_scope(db, worker, ticket, graph, root_id)
                    check_task = asyncio.create_task(scope_with_lock())
                    await asyncio.wait_for(probe_started.wait(), timeout=5)
                    def revoke_workspace():
                        with factory.begin() as db:
                            row = db.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update())
                            row.enabled = False
                    revoke_task = asyncio.create_task(asyncio.to_thread(revoke_workspace))
                    await asyncio.sleep(0.1)
                    assert not revoke_task.done()
                    finish_probe.set()
                    assert (await check_task)[0]
                    await asyncio.wait_for(revoke_task, timeout=5)
                    worker.workspace_broker = broker
                    with worker.transaction() as db:
                        assert await screen_zero_tool_correction_scope(db, worker, ticket, graph, root_id) == (
                            False, "workspace_policy_changed")
                    with factory.begin() as db:
                        db.get(Workspace, workspace_id).enabled = True

                    with worker.transaction() as db:
                        savepoint = db.begin_nested()
                        budget = db.get(RunBudget, closed_id)
                        budget.active_seconds = budget.max_active_seconds
                        db.flush()
                        assert remaining_active_budget(db, closed_id, proof, 1) == 0
                        savepoint.rollback()
                    with factory() as db:
                        model_calls_before = db.get(RunBudget, closed_id).model_calls
                    reservation = await reserve_correction_model(worker, closed_id, ticket=ticket,
                        expected_checkpoint_id=root_id, graph=graph, executor=executor, checkpointer=saver)
                    assert reservation["status"] == "RESERVED" and reservation["dispatch"] is True
                    correction = await dispatch_reserved_correction(worker, closed_id, ticket=ticket,
                        root_id=root_id, graph=graph, executor=executor, checkpointer=saver)
                    assert correction["status"] == "RECORDED"
                    assert correction["batch_status"] == "VALIDATED_NOT_DISPATCHED"
                    assert CorrectionAwareModel.correction_calls == 1
                    with worker.transaction() as db:
                        phase_ok, phase_reason = await inspect_consumed_correction_phase(
                            db, worker, ticket=ticket, root_id=root_id, graph=graph,
                            executor=executor, checkpointer=saver)
                    assert phase_ok, phase_reason
                    with worker.transaction() as db:
                        savepoint = db.begin_nested()
                        model_record = db.get(OperationRecord, correction["operation_id"])
                        model_record.result = {**model_record.result, "response_mac": "0" * 64}
                        db.flush()
                        with pytest.raises(CorrectionReservationUnavailable):
                            load_recorded_correction_batch(db, db.get(Run, closed_id), settings)
                        savepoint.rollback()
                    phase = await prepare_correction_phase(worker, closed_id, ticket=ticket, root_id=root_id,
                        graph=graph, executor=executor, checkpointer=saver)
                    assert phase["status"] == "PREPARED" and phase["tool_dispatch"] is False
                    async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as correction_base:
                        writer = V10CorrectionSaver(correction_base, worker=worker, ticket=ticket, root_id=root_id,
                            graph=graph, executor=executor, execution_saver=saver)
                        mapped = CorrectionNamespacePrototype(writer, run_id=closed_id,
                            prefix="v10corr-" + phase["operation_id"])
                        observed = []
                        class DummyState(TypedDict):
                            value: str
                        async def wait_approval(state, runtime: Runtime):
                            info = runtime.execution_info
                            observed.append((info.thread_id, info.checkpoint_ns, info.task_id))
                            return {"value": interrupt({"type": "approval"})}
                        child = StateGraph(DummyState)
                        child.add_node("approval", wait_approval)
                        child.add_edge(START, "approval")
                        child.add_edge("approval", END)
                        parent = StateGraph(DummyState)
                        parent.add_node("correction", child.compile())
                        parent.add_edge(START, "correction")
                        parent.add_edge("correction", END)
                        correction_graph = parent.compile(checkpointer=mapped)
                        waiting = await correction_graph.ainvoke({"value": "start"}, config, durability="sync")
                        assert waiting["__interrupt__"]
                        assert (await graph.aget_state(config)).config["configurable"]["checkpoint_id"] == root_id
                        completed = await correction_graph.ainvoke(Command(resume="approved"), config, durability="sync")
                        assert completed["value"] == "approved" and len(observed) == 2 and observed[0] == observed[1]
                        assert observed[0][0] == closed_id and observed[0][1].startswith("correction:")
                        assert (await graph.aget_state(config)).config["configurable"]["checkpoint_id"] == root_id
                        from omniharness.v10_sealed_saver import SealedSaverUnavailable
                        for control in ("pause_requested", "cancel_requested"):
                            with factory.begin() as db:
                                db.get(Run, closed_id).status = control
                            with pytest.raises(SealedSaverUnavailable):
                                await correction_graph.aupdate_state(config, {"value": "blocked"})
                            with factory.begin() as db:
                                db.get(Run, closed_id).status = "running"
                        with factory.begin() as db:
                            command = RunCommand(run_id=closed_id, sequence_number=1, command_type="pause",
                                                 client_request_id="pending-phase-test", status="pending", payload={})
                            db.add(command)
                            db.flush()
                            command_id = command.id
                        with pytest.raises(SealedSaverUnavailable):
                            await correction_graph.aupdate_state(config, {"value": "pending-command"})
                        with factory.begin() as db:
                            db.delete(db.get(RunCommand, command_id))
                        await writer.close_writes()
                    repeated_phase = await prepare_correction_phase(worker, closed_id, ticket=ticket, root_id=root_id,
                        graph=graph, executor=executor, checkpointer=saver)
                    assert repeated_phase["status"] == "PENDING_REVIEW" and repeated_phase["tool_dispatch"] is False
                    again = await dispatch_reserved_correction(worker, closed_id, ticket=ticket,
                        root_id=root_id, graph=graph, executor=executor, checkpointer=saver)
                    assert again["status"] == "PENDING_REVIEW"
                    assert CorrectionAwareModel.correction_calls == 1
                    repeat = await reserve_correction_model(worker, closed_id, ticket=ticket,
                        expected_checkpoint_id=root_id, graph=graph, executor=executor, checkpointer=saver)
                    assert repeat["status"] == "PENDING_REVIEW" and repeat["dispatch"] is False
                    from omniharness.models import RunBudgetReservation
                    with factory() as db:
                        assert db.get(RunBudget, closed_id).model_calls == model_calls_before + 1
                        assert db.get(RunBudgetReservation, (closed_id, correction_invocation_id(closed_id))) is not None
                        row = db.get(OperationRecord, correction["operation_id"])
                        assert row.status == "completed" and row.result["dispatch_state"] == "RECORDED"
                        assert row.result["usage"]["total_tokens"] == 14
                        assert not list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == closed_id)))
                        assert not (project / "test.txt").exists()
                listener.discard(closed_id)
                with factory.begin() as db:
                    db.get(Run, closed_id).status = "completed"
                    assert m4_services.release_workspace_lease(db, closed_id)

                # Pristine state is rechecked inside the actual claim lock;
                # rejecting qualification must not stop the ordinary Run.
                for field in ("active_seconds", "active_segment_open", "outcome"):
                    identity = await new_run("dirty-" + field)
                    with factory.begin() as db:
                        if field == "outcome":
                            db.get(Run, identity).outcome = {"prior": "record"}
                        else:
                            setattr(db.get(RunBudget, identity), field, True if field == "active_segment_open" else 1)
                    assert worker.claim() == identity
                    assert identity not in listener.committing and identity not in listener.accepted
                    assert identity in listener.tainted
                    with factory.begin() as db:
                        db.get(Run, identity).status = "completed"
                        assert m4_services.release_workspace_lease(db, identity)

                locked_id = await new_run("locked-origin")
                with factory() as db:
                    locked_origin = db.get(Run, locked_id).config_snapshot["v10_owner_origin"]
                with factory.begin() as blocker:
                    blocker.scalar(select(Run).where(Run.id == locked_id).with_for_update())
                    started = time.monotonic()
                    listener._register(locked_origin)
                    assert time.monotonic() - started < 1.5
                    assert not lock.lost and listener.alive
                assert locked_id in listener.tainted and locked_id not in listener.pending
                assert worker.claim() == locked_id
                assert locked_id not in listener.committing and locked_id not in listener.accepted
                with factory.begin() as db:
                    db.get(Run, locked_id).status = "completed"
                    assert m4_services.release_workspace_lease(db, locked_id)

                # Exercise the real run_once path without pre-waiting for the
                # notification; a short delayed delivery should be observed.
                register = listener._register
                gates = {}
                async def delayed_register(origin):
                    await gates.setdefault(origin["run_id"], asyncio.Event()).wait()
                    register(origin)
                monkeypatch.setattr(listener, "_register", lambda origin: asyncio.create_task(delayed_register(origin)))
                created = client.post("/api/v1/conversations", json={})
                assert created.status_code == 201, created.text
                raced = await asyncio.to_thread(client.post,
                    f"/api/v1/conversations/{created.json()['id']}/messages",
                    json={**body, "client_request_id": "notification-race"})
                assert raced.status_code == 202, raced.text
                raced_id = raced.json()["run_id"]
                created = client.post("/api/v1/conversations", json={})
                assert created.status_code == 201, created.text
                later = await asyncio.to_thread(client.post,
                    f"/api/v1/conversations/{created.json()['id']}/messages",
                    json={**body, "client_request_id": "other-notification"})
                assert later.status_code == 202, later.text
                later_id = later.json()["run_id"]
                seen = []
                async def no_model(identity):
                    seen.append((identity, identity in listener.accepted))
                monkeypatch.setattr(worker, "process", no_model)
                claim_task = asyncio.create_task(worker.run_once())
                await asyncio.sleep(0.02)
                assert not claim_task.done()
                gates.setdefault(later_id, asyncio.Event()).set()
                await asyncio.sleep(0.02)
                assert later_id in listener.pending
                assert not claim_task.done()
                gates.setdefault(raced_id, asyncio.Event()).set()
                assert await claim_task == raced_id
                assert seen == [(raced_id, True)]
                assert raced_id not in listener.accepted  # process return discards it
                monkeypatch.setattr(listener, "_register", register)
                with factory.begin() as db:
                    db.get(Run, raced_id).status = "completed"
                    assert m4_services.release_workspace_lease(db, raced_id)
                assert worker.claim() == later_id
                with factory.begin() as db:
                    db.get(Run, later_id).status = "completed"
                    assert m4_services.release_workspace_lease(db, later_id)
                listener.claim_failed(later_id)

                # Test-only actual correction gateway with the real authenticated
                # broker app; its registry and data directory belong to tmp_path.
                from dataclasses import replace

                import httpx
                from test_v10_docker_sandbox import DisposableDockerSandbox

                from omniharness.workspace_broker import create_broker_app
                from omniharness.workspace_io import WorkspaceError

                grant = replace(broker.io.registry.grants[workspace_id], instance_id=settings.instance_id)
                broker.io.registry.grants[workspace_id] = grant
                broker.io.registry.versions[(workspace_id, grant.version)] = grant
                def require_test_lease(instance_id, ws_id, owner_run):
                    with factory() as check:
                        if instance_id != settings.instance_id or not m4_services.validate_workspace_lease(check, ws_id, owner_run):
                            raise WorkspaceError("lease_required", "Workspace lease is unavailable")
                broker.io.require_write_lease = require_test_lease
                app = create_broker_app(broker.io, instance_id=settings.instance_id, service_key="w" * 64)
                class TestASGIBroker:
                    def __init__(self):
                        self.calls = []
                    async def request(self, ws_id, version, action, **payload):
                        self.calls.append(action)
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker",
                                headers={"Authorization": "Bearer " + "w" * 64,
                                         "X-Omni-Instance": settings.instance_id}) as conn:
                            response = await conn.post(f"/workspaces/{ws_id}/{action}",
                                json={"policy_version": version, **payload})
                        response.raise_for_status()
                        return response.json()
                real_broker = TestASGIBroker()
                real_sandbox = DisposableDockerSandbox()
                worker.workspace_broker, worker.sandbox_executor = real_broker, real_sandbox
                worker._v10_mock_correction_test_only = True
                worker._v10_mock_tool_execution_test_only = True
                worker.agent_model_factory = lambda role, _config: (
                    CorrectionAwareModel() if role == "execute" else InteractiveStage(role))
                monkeypatch.setattr(worker, "process", original_process)
                CorrectionAwareModel.correction_calls = 0
                patch_id = await new_run("real-gateway-single")
                assert await worker.run_once() == patch_id
                with factory() as db:
                    patch_run = db.get(Run, patch_id)
                    operations = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == patch_id)))
                    assert patch_run.status == "pending_review", (patch_run.status, patch_run.error_code, patch_run.error_message)
                    assert patch_run.error_code == "CORRECTION_TOOL_REVIEW", (patch_run.error_code, patch_run.error_message)
                    assert len(operations) == 1 and operations[0].status == "completed", [(op.tool_name, op.status) for op in operations]
                assert (project / "test.txt").read_text(encoding="utf-8") == "mock result\n"
                assert CorrectionAwareModel.correction_calls == 1 and not real_sandbox.calls
                with factory.begin() as db:
                    db.get(Run, patch_id).status = "completed"
                    assert m4_services.release_workspace_lease(db, patch_id)
                worker._v10_mock_tool_execution_test_only = False
                worker._v10_mock_correction_test_only = False
                worker.workspace_broker, worker.sandbox_executor = broker, sandbox

                run_id = await new_run("listener-loss")
                assert worker.claim() == run_id
                assert await listener.confirm_claim(run_id)
                assert run_id in listener.accepted

                # Killing the actual listening DB backend invalidates the
                # in-memory grant and cannot be repaired by a saved origin.
                with factory.begin() as db:
                    db.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": listener.listener_pid})
                deadline = time.monotonic() + 5
                while listener.alive and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                assert not listener.alive
                assert not await listener.consume_owner_presence(run_id)
                assert run_id not in listener.accepted
            finally:
                await listener.close()
                worker._v10_owner_listener = None
            with factory.begin() as db:
                db.get(Run, run_id).status = "completed"
                assert m4_services.release_workspace_lease(db, run_id)
            created = client.post("/api/v1/conversations", json={})
            assert created.status_code == 201, created.text
            fallback = await asyncio.to_thread(client.post,
                f"/api/v1/conversations/{created.json()['id']}/messages",
                json={**body, "client_request_id": "without-listener"})
            assert fallback.status_code == 202, fallback.text
            fallback_id = fallback.json()["run_id"]
            with factory() as db:
                assert "v10_owner_origin" not in db.get(Run, fallback_id).config_snapshot
            assert worker.claim() == fallback_id
            with factory.begin() as db:
                db.get(Run, fallback_id).status = "completed"
                assert m4_services.release_workspace_lease(db, fallback_id)
            monkeypatch.setattr(worker, "process", original_process)
            listener = await V10OwnerListener.start(worker)
            assert listener is not None
            worker._v10_owner_listener = listener
            try:
                worker._v10_mock_correction_test_only = True
                worker.agent_model_factory = lambda role, _config: (
                    WaitingCorrectionModel() if role == "execute" else InteractiveStage(role))
                WaitingCorrectionModel.correction_calls = 0
                WaitingCorrectionModel.correction_started = asyncio.Event()
                WaitingCorrectionModel.correction_cancelled = asyncio.Event()
                interrupted_id = await new_run("owner-loss-during-correction")
                pending_work = asyncio.create_task(worker.run_once())
                await asyncio.wait_for(WaitingCorrectionModel.correction_started.wait(), timeout=30)
                owner_pid = listener.identity["pid"]
                with factory.begin() as db:
                    db.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": owner_pid})
                from omniharness.worker import WorkerOwnershipLost
                with pytest.raises(WorkerOwnershipLost):
                    await asyncio.wait_for(pending_work, timeout=15)
                await asyncio.wait_for(WaitingCorrectionModel.correction_cancelled.wait(), timeout=5)
                assert WaitingCorrectionModel.correction_calls == 1
                with factory() as db:
                    record = db.scalar(select(OperationRecord).where(OperationRecord.run_id == interrupted_id,
                        OperationRecord.node_id == "v10-correction:model_calls"))
                    assert record and record.result["dispatch_state"] in {"DISPATCHED", "UNCERTAIN"}
                    assert not list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == interrupted_id)))
                from omniharness.m3_worker import recover_agents
                replacement = Worker(settings, engine)
                with WorkerLock(engine, settings) as replacement_owner:
                    replacement.lock = replacement_owner
                    await recover_agents(replacement)
                with factory() as db:
                    assert db.get(Run, interrupted_id).status == "pending_review"
                assert WaitingCorrectionModel.correction_calls == 1
            finally:
                await listener.close()
                worker._v10_owner_listener = None
