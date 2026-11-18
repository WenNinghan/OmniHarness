"""Fence pin checks against the disposable PostgreSQL test database only."""
# ruff: noqa: F811
import asyncio
from dataclasses import replace
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from test_postgres import postgres_environment  # noqa: F401

from omniharness.checkpoint_fencing import initialize_checkpoint_schema, open_fenced_saver, register_fence
from omniharness.m3_worker import _checkpoint_url
from omniharness.v10_closure import CheckpointPinUnavailable, pin_checkpoint_watermark
from omniharness.worker import Worker, WorkerLock


@pytest.mark.asyncio
async def test_pin_rejects_foreign_owner_and_blocks_saver_until_commit(postgres_environment):
    from conftest import create_chat
    from fastapi.testclient import TestClient

    from omniharness.api import create_app
    from omniharness.m4_contract import M4_V10_PLUGIN_VERSIONS
    from omniharness.models import Run
    from omniharness.security import create_pairing_code

    settings, engine, factory, _ = postgres_environment
    schema = "v10_pin_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=schema)
    worker = Worker(settings, engine)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        thread_id = create_chat(client)[2]["run_id"]
    with factory.begin() as db:
        run = db.get(Run, thread_id)
        run.status, run.workflow_version = "running", "stage-agent-v10"
        run.config_snapshot = {**run.config_snapshot, "agent": {"plugin_versions": M4_V10_PLUGIN_VERSIONS}}

    class State(TypedDict):
        value: str

    with WorkerLock(engine, settings) as lock:
        worker.lock = lock
        ticket = register_fence(lock, thread_id, "stage-agent-v10", schema=schema)
        async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as saver:
            builder = StateGraph(State)
            builder.add_node("step", lambda state: {"value": state["value"]})
            builder.add_edge(START, "step")
            builder.add_edge("step", END)
            graph = builder.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": thread_id}}
            await graph.ainvoke({"value": "first"}, config, durability="sync")
            saved = await graph.aget_state(config)
            assert not saved.next and not saved.interrupts
            checkpoint_id = saved.config["configurable"]["checkpoint_id"]

            # A stale epoch and a different SQL backend cannot reuse the ticket.
            with worker.transaction() as db:
                with pytest.raises(CheckpointPinUnavailable):
                    pin_checkpoint_watermark(db, replace(ticket, epoch=ticket.epoch + 1), checkpoint_id)
            with factory.begin() as db:
                with pytest.raises(CheckpointPinUnavailable):
                    pin_checkpoint_watermark(db, ticket, checkpoint_id)

            # Saver trigger takes FOR SHARE on the same fence row; an update
            # begun under the pin must wait for this owner transaction.
            with worker.transaction() as db:
                assert pin_checkpoint_watermark(db, ticket, checkpoint_id)[""] == checkpoint_id
                task = asyncio.create_task(graph.aupdate_state(config, {"value": "second"}))
                await asyncio.sleep(0.25)
                assert not task.done()
            await asyncio.wait_for(task, timeout=10)
            current = await graph.aget_state(config)
            assert current.config["configurable"]["checkpoint_id"] != checkpoint_id
            with worker.transaction() as db:
                with pytest.raises(CheckpointPinUnavailable):
                    pin_checkpoint_watermark(db, ticket, checkpoint_id)


@pytest.mark.asyncio
async def test_child_response_binding_and_stale_checkpoint_fail_closed(postgres_environment, tmp_path):
    import json

    from fastapi.testclient import TestClient
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from sqlalchemy import select
    from test_m4_collaboration import prepare_v4

    from omniharness.api import create_app
    from omniharness.artifacts import ArtifactStore
    from omniharness.m3_delegation import HierarchicalDelegation
    from omniharness.m3_host import AgentOperationNeedsReview
    from omniharness.m3_stages import build_stage_graph
    from omniharness.m4_contract import M4_V10_PLUGIN_VERSIONS, workspace_role_versions
    from omniharness.m4_runtime import M4AgentHost, build_execution_agent
    from omniharness.models import OperationRecord, Run, RunBudgetReservation
    from omniharness.security import create_pairing_code
    from omniharness.stage_contract import STAGE_KEYS, STAGE_ROLE_BINDINGS
    from omniharness.v10_closure import (
        child_terminal_mac,
        inspect_pinned_zero_tool_inventory,
        screen_zero_tool_execution_inventory,
    )

    settings, engine, factory, _ = postgres_environment
    schema = "v10_child_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=schema)

    class Stage:
        async def ainvoke(self, messages):
            return AIMessage(content=json.dumps({"status": "continue", "questions": []}))

    class Model(BaseChatModel):
        @property
        def _llm_type(self): return "v10-offline-postgres-test"
        def bind_tools(self, tools, **kwargs): return self
        def _generate(self, messages, **kwargs):
            message = AIMessage(content=json.dumps({"status": "completed", "content": "claimed", "questions": []}),
                                response_metadata={"finish_reason": "stop"})
            return ChatResult(generations=[ChatGeneration(message=message)])
        async def _agenerate(self, messages, **kwargs): return self._generate(messages, **kwargs)

    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, workspace, broker, sandbox, _ = prepare_v4((settings, engine, factory, client), tmp_path)
        versions = workspace_role_versions("stage-agent-v10")
        with factory.begin() as db:
            run = db.get(Run, run_id)
            agent = {**run.config_snapshot["agent"], "workflow_version": "stage-agent-v10",
                     "plugin_versions": M4_V10_PLUGIN_VERSIONS, "strategy": "single"}
            agent["roles"] = {role: {**agent["roles"][role], "role_version": versions[role]} for role in STAGE_KEYS}
            run.config_snapshot = {**run.config_snapshot, "agent": agent}
            run.workflow_version = "stage-agent-v10"
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            ticket = register_fence(owner, run_id, "stage-agent-v10", schema=schema)
            async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as saver:
                host = M4AgentHost(worker, run_id, "stage-agent-v10", role_versions=versions,
                                   plugin_versions=M4_V10_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
                scheduler = HierarchicalDelegation()
                executor = build_execution_agent(model=Model(), host=host, workspace=workspace, broker=broker,
                    sandbox=sandbox, artifacts=ArtifactStore(settings.data_dir / "artifacts"), scheduler=scheduler)
                graph = build_stage_graph(models={role: Stage() for role in STAGE_KEYS}, hooks=host,
                    checkpointer=saver, workflow_version="stage-agent-v10", execution_agent=executor,
                    execution_strategy="single")
                config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
                try:
                    await graph.ainvoke({"messages": [HumanMessage(content="test")]}, config, durability="sync")
                    root = await graph.aget_state(config)
                    root_id = root.config["configurable"]["checkpoint_id"]
                    passed, reason = await inspect_pinned_zero_tool_inventory(worker, ticket, graph, executor, saver, root_id)
                    assert (passed, reason) == (True, "pinned_preconditions_only_not_closed")
                    with factory.begin() as db:
                        run = db.get(Run, run_id)
                        saved_outcome = dict(run.outcome)
                        observations = dict(saved_outcome["v10_child_executions"])
                        observations["single"] = {**observations["single"], "mac": "0" * 64}
                        run.outcome = {**saved_outcome, "v10_child_executions": observations}
                    passed, reason = await inspect_pinned_zero_tool_inventory(worker, ticket, graph, executor, saver, root_id)
                    assert (passed, reason) == (False, "child_observation_invalid")
                    with factory.begin() as db:
                        db.get(Run, run_id).outcome = saved_outcome
                    history = [item async for item in graph.aget_state_history(config)]
                    stale = next(item for item in history if item.next == ("execute_task",))
                    with pytest.raises(AgentOperationNeedsReview):
                        await graph.ainvoke(None, {**stale.config, "recursion_limit": 100}, durability="sync")
                finally:
                    await scheduler.aclose()
                with factory() as db:
                    run = db.get(Run, run_id)
                    record = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id,
                        OperationRecord.node_id == "agent:execute:model_calls"))
                    scope = record.result["v10_model_scope"]
                    observation = run.outcome["v10_child_executions"]["single"]
                    assert observation["mac"] == child_terminal_mac(settings, run, observation["manifest"])
                    reservation = db.get(RunBudgetReservation, {"run_id": run_id,
                                                                "invocation_id": scope["invocation_id"]})
                    args = {"run": run, "settings": settings, "task_id": "single",
                            "records": [record], "reservations": [reservation]}
                    assert screen_zero_tool_execution_inventory(**args)[0]
                    record.result = {**record.result, "v10_model_scope_mac": "0" * 64}
                    assert not screen_zero_tool_execution_inventory(**args)[0]
