"""Isolated experiment only: no production saver or database migration."""
# ruff: noqa: F811
import asyncio
from typing import TypedDict
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from test_m4_collaboration import prepare_v4
from test_postgres import postgres_environment  # noqa: F401

from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema, open_fenced_saver, register_fence
from omniharness.m3_worker import _checkpoint_url
from omniharness.m4_contract import M4_V10_PLUGIN_VERSIONS
from omniharness.models import OperationRecord, Run
from omniharness.security import create_pairing_code
from omniharness.v10_closure import pin_checkpoint_watermark
from omniharness.worker import WorkerLock


@pytest.mark.asyncio
async def test_same_connection_prototype_atomicity_fence_and_constraints(postgres_environment, tmp_path):
    settings, engine, factory, business_schema = postgres_environment
    checkpoint_schema = "v10_proto_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=checkpoint_schema)

    class State(TypedDict):
        value: str

    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, *_ = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            run.workflow_version = "stage-agent-v10"
            run.config_snapshot = {**run.config_snapshot, "agent": {**run.config_snapshot["agent"],
                "workflow_version": "stage-agent-v10", "plugin_versions": M4_V10_PLUGIN_VERSIONS}}
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            ticket = register_fence(owner, run_id, "stage-agent-v10", schema=checkpoint_schema)
            async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as saver:
                builder = StateGraph(State)
                builder.add_node("step", lambda state: {"value": state["value"]})
                builder.add_edge(START, "step")
                builder.add_edge("step", END)
                graph = builder.compile(checkpointer=saver)
                config = {"configurable": {"thread_id": run_id}}
                await graph.ainvoke({"value": "original"}, config, durability="sync")
                original = (await graph.aget_state(config)).config["configurable"]["checkpoint_id"]
                seal_id = "a" * 64
                # Schema name comes only from the strictly generated test fixture.
                insert = f"""INSERT INTO {business_schema}.operation_records
                    (operation_id, run_id, node_id, parameters_hash, status, attempts, workflow_version, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now())"""
                params = (seal_id, run_id, "v10-prototype-seal", "b" * 64, "completed", 1, "stage-agent-v10")

                with pytest.raises(RuntimeError):
                    async with saver.conn.transaction():
                        await graph.aupdate_state(config, {"value": "rolled-back"})
                        await saver.conn.execute(insert, params)
                        raise RuntimeError("synthetic rollback")
                assert (await graph.aget_state(config)).config["configurable"]["checkpoint_id"] == original
                with factory() as db:
                    assert db.get(OperationRecord, seal_id) is None

                async with saver.conn.transaction():
                    await graph.aupdate_state(config, {"value": "committed"})
                    await saver.conn.execute(insert, params)
                committed = (await graph.aget_state(config)).config["configurable"]["checkpoint_id"]
                assert committed != original
                with factory() as db:
                    row = db.get(OperationRecord, seal_id)
                    assert row and row.node_id == "v10-prototype-seal" and row.run_id == run_id

                # Existing primary keys cannot be silently overwritten; the
                # duplicate aborts the coupled checkpoint transaction too.
                with pytest.raises(psycopg.errors.UniqueViolation):
                    async with saver.conn.transaction():
                        await graph.aupdate_state(config, {"value": "duplicate"})
                        await saver.conn.execute(insert, params)
                assert (await graph.aget_state(config)).config["configurable"]["checkpoint_id"] == committed

                for invalid, expected in (
                    (("e" * 64, run_id, None, "b" * 64, "completed", 1, "stage-agent-v10"),
                     psycopg.errors.NotNullViolation),
                    (("f" * 64, str(uuid4()), "v10-prototype-seal", "b" * 64, "completed", 1, "stage-agent-v10"),
                     psycopg.errors.ForeignKeyViolation),
                ):
                    with pytest.raises(expected):
                        async with saver.conn.transaction():
                            await graph.aupdate_state(config, {"value": "invalid-row"})
                            await saver.conn.execute(insert, invalid)
                    assert (await graph.aget_state(config)).config["configurable"]["checkpoint_id"] == committed

                # Invalid epoch causes the saver trigger to reject the whole
                # transaction before a coupled business row can be committed.
                await saver.conn.execute("SELECT set_config('omni.owner_epoch', %s, false)", (str(ticket.epoch + 1),))
                with pytest.raises(psycopg.errors.RaiseException):
                    async with saver.conn.transaction():
                        await graph.aupdate_state(config, {"value": "stale"})
                        await saver.conn.execute(insert, ("c" * 64, *params[1:]))
                await saver.conn.execute("SELECT set_config('omni.owner_epoch', %s, false)", (str(ticket.epoch),))
                assert (await graph.aget_state(config)).config["configurable"]["checkpoint_id"] == committed

                async def coupled_write():
                    async with saver.conn.transaction():
                        await graph.aupdate_state(config, {"value": "after-pin"})
                        await saver.conn.execute(insert, ("d" * 64, *params[1:]))
                with worker.transaction() as db:
                    pin_checkpoint_watermark(db, ticket, committed)
                    task = asyncio.create_task(coupled_write())
                    await asyncio.sleep(0.25)
                    assert not task.done()
                await asyncio.wait_for(task, timeout=10)
                with factory() as db:
                    assert db.get(OperationRecord, "d" * 64) is not None
                assert (await graph.aget_state(config)).values["value"] == "after-pin"

                # Loss of the advisory-lock owner makes a stale saver reject
                # future writes; the prototype business row must not appear.
                last_id = (await graph.aget_state(config)).config["configurable"]["checkpoint_id"]
                owner.connection.invalidate()
                owner.lost = True
                with pytest.raises(psycopg.errors.RaiseException):
                    async with saver.conn.transaction():
                        await graph.aupdate_state(config, {"value": "owner-lost"})
                        await saver.conn.execute(insert, ("9" * 64, *params[1:]))
                assert (await graph.aget_state(config)).config["configurable"]["checkpoint_id"] == last_id
                with factory() as db:
                    assert db.get(OperationRecord, "9" * 64) is None
