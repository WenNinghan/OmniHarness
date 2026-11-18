"""Offline-only v10 sealed saver checks against disposable PostgreSQL."""
# ruff: noqa: F811
import asyncio
import sys
from typing import TypedDict
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from test_m4_collaboration import prepare_v4
from test_postgres import postgres_environment  # noqa: F401

from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema, open_fenced_saver, register_fence
from omniharness.m3_worker import _checkpoint_url
from omniharness.models import OperationRecord, Run
from omniharness.security import create_pairing_code
from omniharness.v10_sealed_saver import SEALED_PLUGIN_VERSIONS, V10SealedSaver, create_seal_origin
from omniharness.worker import WorkerLock


@pytest.mark.asyncio
async def test_sealed_saver_graph_commits_checkpoint_and_chain(postgres_environment, tmp_path, monkeypatch):
    from copy import deepcopy

    from omniharness import v10_sealed_saver as module
    from omniharness.v10_sealed_saver import SealedSaverUnavailable
    settings, engine, factory, business_schema = postgres_environment
    schema = "v10_sealed_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=schema)

    class State(TypedDict):
        value: str

    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, *_ = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            run.workflow_version = "stage-agent-v10"
            run.config_snapshot = {**run.config_snapshot, "agent": {**run.config_snapshot["agent"],
                "workflow_version": "stage-agent-v10", "plugin_versions": SEALED_PLUGIN_VERSIONS},
                "v10_checkpoint_seal_origin": create_seal_origin(settings, run.id, run.workspace_id)}
        with WorkerLock(engine, settings) as owner:
            ticket = register_fence(owner, run_id, "stage-agent-v10", schema=schema)
            async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as base:
                saver = V10SealedSaver(base.conn, ticket, business_schema=business_schema, settings=settings)
                builder = StateGraph(State)
                builder.add_node("step", lambda state: {"value": state["value"] + "!"})
                builder.add_edge(START, "step")
                builder.add_edge("step", END)
                graph = builder.compile(checkpointer=saver)
                config = {"configurable": {"thread_id": run_id}}
                assert (await graph.ainvoke({"value": "test"}, config, durability="sync"))["value"] == "test!"
                assert (await graph.aget_state(config)).values["value"] == "test!"
                with factory() as db:
                    run = db.get(Run, run_id)
                    head = run.outcome["v10_checkpoint_seal_head"]
                    saved_outcome = deepcopy(run.outcome)
                    rows = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id,
                        OperationRecord.node_id == "v10-checkpoint-seal-v1")))
                    assert len(rows) == head["sequence"] > 0
                    assert all(row.workflow_version == "stage-agent-v10" for row in rows)
                    count = len(rows)
                root_id = (await graph.aget_state(config)).config["configurable"]["checkpoint_id"]
                with owner.session(factory) as db:
                    watermarks, checked_head = module.verify_pinned_seal_inventory(db, ticket, root_id, settings)
                    assert watermarks[""] == root_id
                    assert checked_head == head
                from omniharness.v10_closure import inspect_pinned_zero_tool_inventory
                worker.lock = owner
                with owner.session(factory) as db:
                    passed, reason = await inspect_pinned_zero_tool_inventory(
                        worker, ticket, graph, graph, saver, root_id, owner_db=db, require_seal=True)
                    assert not passed and reason != "sealed_owner_scope_unavailable"
                worker.lock = None
                with owner.session(factory) as db:
                    with pytest.raises(SealedSaverUnavailable):
                        monkeypatch.setattr(module, "_VERSIONS", {"langgraph": "0.invalid"})
                        module.verify_pinned_seal_inventory(db, ticket, root_id, settings)
                monkeypatch.undo()
                with factory.begin() as db:
                    run = db.get(Run, run_id)
                    original_snapshot = deepcopy(run.config_snapshot)
                    other = create_seal_origin(settings, run.id, run.workspace_id)
                    other["state"] = "started"
                    key = module.hmac.new(module._key(settings, "stage-agent-v10"),
                        b"omniharness/v10/checkpoint-seal/v1", module.hashlib.sha256).digest()
                    other["mac"] = module._tag(key, "origin", {k: other[k] for k in
                        ("version", "run_id", "workspace_id", "nonce", "state")})
                    run.config_snapshot = {**run.config_snapshot, "v10_checkpoint_seal_origin": other}
                with pytest.raises(SealedSaverUnavailable), owner.session(factory) as db:
                    module.verify_pinned_seal_inventory(db, ticket, root_id, settings)
                with factory.begin() as db:
                    db.get(Run, run_id).config_snapshot = original_snapshot

                original_inventory = saver._inventory
                attempts = 0
                async def fail_after_write(cur):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 2:
                        raise SealedSaverUnavailable("synthetic failure after checkpoint write")
                    return await original_inventory(cur)
                monkeypatch.setattr(saver, "_inventory", fail_after_write)
                with pytest.raises(SealedSaverUnavailable):
                    await graph.aupdate_state(config, {"value": "must-roll-back"})
                monkeypatch.setattr(saver, "_inventory", original_inventory)
                assert (await graph.aget_state(config)).config["configurable"]["checkpoint_id"] == root_id
                with factory() as db:
                    assert len(list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id,
                        OperationRecord.node_id == "v10-checkpoint-seal-v1")))) == count
                    assert db.get(Run, run_id).outcome == saved_outcome

                monkeypatch.setattr(module, "_MAX_ROWS", 0)
                with pytest.raises(SealedSaverUnavailable):
                    await graph.aupdate_state(config, {"value": "over-limit"})
                monkeypatch.undo()
                assert (await graph.aget_state(config)).config["configurable"]["checkpoint_id"] == root_id

                with factory.begin() as db:
                    run = db.get(Run, run_id)
                    outcome = deepcopy(run.outcome)
                    outcome["v10_checkpoint_seal_head"]["sequence"] += 1
                    run.outcome = outcome
                with pytest.raises(SealedSaverUnavailable):
                    await graph.aupdate_state(config, {"value": "tampered-head"})
                with pytest.raises(SealedSaverUnavailable), owner.session(factory) as db:
                    module.verify_pinned_seal_inventory(db, ticket, root_id, settings)
                with factory.begin() as db:
                    db.get(Run, run_id).outcome = saved_outcome
                assert (await graph.aget_state(config)).config["configurable"]["checkpoint_id"] == root_id

                # Clearing checkpoint/head/chain cannot make the already
                # started origin eligible for initialization again.
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    await base.conn.execute(f"DELETE FROM {schema}.{table} WHERE thread_id=%s", (run_id,))
                with factory.begin() as db:
                    for row in db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id,
                            OperationRecord.node_id == "v10-checkpoint-seal-v1")):
                        db.delete(row)
                    db.get(Run, run_id).outcome = {}
                from langgraph.checkpoint.base import empty_checkpoint
                with pytest.raises(SealedSaverUnavailable, match="Started Run cannot be treated as pristine"):
                    await saver.aput({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}},
                                     empty_checkpoint(), {"source": "input", "step": -1, "parents": {}}, {})


@pytest.mark.asyncio
async def test_sealed_saver_matches_official_for_valid_writes_and_rejects_conflicts(postgres_environment, tmp_path, monkeypatch):
    from copy import deepcopy

    from conftest import create_chat
    from langgraph.checkpoint.base import empty_checkpoint

    from omniharness.m4_contract import M4_V10_PLUGIN_VERSIONS
    from omniharness.v10_sealed_saver import SealedSaverUnavailable

    settings, engine, factory, business_schema = postgres_environment
    sealed_schema = "v10_sealed_" + uuid4().hex[:12]
    official_schema = "v10_base_" + uuid4().hex[:12]
    for name in (sealed_schema, official_schema):
        await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=name)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, *_ = prepare_v4((settings, engine, factory, client), tmp_path)
        official_run_id = create_chat(client)[2]["run_id"]
        with factory.begin() as db:
            run = db.get(Run, run_id)
            run.workflow_version = "stage-agent-v10"
            run.config_snapshot = {**run.config_snapshot, "agent": {**run.config_snapshot["agent"],
                "workflow_version": "stage-agent-v10", "plugin_versions": SEALED_PLUGIN_VERSIONS},
                "v10_checkpoint_seal_origin": create_seal_origin(settings, run.id, run.workspace_id)}
            official_run = db.get(Run, official_run_id)
            official_run.status, official_run.workflow_version = "running", "stage-agent-v10"
            official_run.config_snapshot = {**official_run.config_snapshot,
                "agent": {"plugin_versions": M4_V10_PLUGIN_VERSIONS}}
        with WorkerLock(engine, settings) as owner:
            sealed_ticket = register_fence(owner, run_id, "stage-agent-v10", schema=sealed_schema)
            official_ticket = register_fence(owner, official_run_id, "stage-agent-v10", schema=official_schema)
            async with open_fenced_saver(_checkpoint_url(settings.database_url), sealed_ticket) as base:
                async with open_fenced_saver(_checkpoint_url(settings.database_url), official_ticket) as official:
                    saver = V10SealedSaver(base.conn, sealed_ticket, business_schema=business_schema, settings=settings)
                    config = {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}
                    cp = empty_checkpoint()
                    cp["channel_values"] = {"text": "hello", "items": [1, 2]}
                    cp["channel_versions"] = {"text": "v1", "items": "v1"}
                    versions = {"text": "v1", "items": "v1"}
                    meta = {"source": "input", "step": -1, "parents": {}}
                    sealed_config = await saver.aput(config, deepcopy(cp), meta, versions)
                    official_config = await official.aput(
                        {"configurable": {"thread_id": official_run_id, "checkpoint_ns": ""}},
                        deepcopy(cp), meta, versions)
                    a, b = await saver.aget_tuple(sealed_config), await official.aget_tuple(official_config)
                    assert a.checkpoint == b.checkpoint and a.metadata == b.metadata
                    writes = [("sample", {"n": 1})]
                    await saver.aput_writes(sealed_config, writes, "graph-task")
                    await official.aput_writes(official_config, writes, "graph-task")
                    before = (await saver.aget_tuple(sealed_config)).pending_writes
                    assert before == (await official.aget_tuple(official_config)).pending_writes
                    with factory() as db:
                        sequence = db.get(Run, run_id).outcome["v10_checkpoint_seal_head"]["sequence"]
                    await saver.aput_writes(sealed_config, writes, "graph-task")
                    with factory() as db:
                        assert db.get(Run, run_id).outcome["v10_checkpoint_seal_head"]["sequence"] == sequence
                    from omniharness import v10_sealed_saver as module
                    monkeypatch.setattr(module, "_MAX_SEALS", sequence)
                    # A replay of the latest identical write remains allowed
                    # at the limit; a new seal is refused before SQL mutation.
                    await saver.aput_writes(sealed_config, writes, "graph-task")
                    with pytest.raises(SealedSaverUnavailable, match="chain exceeds bounds"):
                        await saver.aput_writes(sealed_config, writes, "another-task")
                    monkeypatch.undo()
                    with pytest.raises(SealedSaverUnavailable):
                        await saver.aput_writes(sealed_config, [("sample", {"n": 2})], "graph-task")
                    assert (await saver.aget_tuple(sealed_config)).pending_writes == before
                    await saver.aput_writes(sealed_config, [("__error__", "first")], "error-task")
                    await official.aput_writes(official_config, [("__error__", "first")], "error-task")
                    assert (await saver.aget_tuple(sealed_config)).pending_writes == (
                        await official.aget_tuple(official_config)).pending_writes
                    with pytest.raises(SealedSaverUnavailable):
                        await saver.aput_writes(sealed_config, [("__error__", "changed")], "error-task")
                    # Exact replay from an earlier seal after a later write is
                    # intentionally refused; this candidate is conservative.
                    with pytest.raises(SealedSaverUnavailable):
                        await saver.aput_writes(sealed_config, writes, "graph-task")


@pytest.mark.asyncio
async def test_missing_business_insert_privilege_rolls_back_checkpoint(postgres_environment, tmp_path):
    import secrets

    import psycopg
    from langgraph.checkpoint.base import empty_checkpoint
    from psycopg import sql
    from sqlalchemy.engine import make_url

    from omniharness.checkpoint_fencing import FencingError

    settings, engine, factory, business_schema = postgres_environment
    schema = "v10_sealed_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=schema)
    role = "v10_limited_" + uuid4().hex[:12]
    password = secrets.token_hex(16)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, *_ = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            run.workflow_version = "stage-agent-v10"
            run.config_snapshot = {**run.config_snapshot, "agent": {**run.config_snapshot["agent"],
                "workflow_version": "stage-agent-v10", "plugin_versions": SEALED_PLUGIN_VERSIONS},
                "v10_checkpoint_seal_origin": create_seal_origin(settings, run.id, run.workspace_id)}
        with WorkerLock(engine, settings) as owner:
            ticket = register_fence(owner, run_id, "stage-agent-v10", schema=schema)
            async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as admin_saver:
                conn = admin_saver.conn
                await conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)))
                await conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {}, {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(business_schema), sql.Identifier(role)))
                await conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}")
                                   .format(sql.Identifier(schema), sql.Identifier(role)))
                await conn.execute(sql.SQL("GRANT SELECT, UPDATE ON {}.runs TO {}").format(
                    sql.Identifier(business_schema), sql.Identifier(role)))
                for table in ("run_budgets", "agent_runs", "tool_operations", "operation_records"):
                    await conn.execute(sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
                        sql.Identifier(business_schema), sql.Identifier(table), sql.Identifier(role)))
                url = make_url(_checkpoint_url(settings.database_url)).set(username=role, password=password)
                with pytest.raises(FencingError):
                    async with open_fenced_saver(url.render_as_string(hide_password=False), ticket):
                        pass
                # Only the disposable test role receives visibility necessary
                # to inspect another backend's owner identity.
                await conn.execute(sql.SQL("GRANT pg_read_all_stats TO {}").format(sql.Identifier(role)))
                async with open_fenced_saver(url.render_as_string(hide_password=False), ticket) as limited:
                    saver = V10SealedSaver(limited.conn, ticket, business_schema=business_schema, settings=settings)
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        await saver.aput({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}},
                            empty_checkpoint(), {"source": "input", "step": -1, "parents": {}}, {})
                assert await admin_saver.aget_tuple({"configurable": {"thread_id": run_id}}) is None
                with factory() as db:
                    run = db.get(Run, run_id)
                    assert not (run.outcome or {}).get("v10_checkpoint_seal_head")
                    assert run.config_snapshot["v10_checkpoint_seal_origin"]["state"] == "unused"


@pytest.mark.asyncio
async def test_real_process_kill_rolls_back_checkpoint_and_origin(postgres_environment, tmp_path):
    import os
    import subprocess
    import time
    from pathlib import Path

    from sqlalchemy import text

    settings, engine, factory, business_schema = postgres_environment
    schema = "v10_kill_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=schema)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, *_ = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            run.workflow_version = "stage-agent-v10"
            run.config_snapshot = {**run.config_snapshot, "agent": {**run.config_snapshot["agent"],
                "workflow_version": "stage-agent-v10", "plugin_versions": SEALED_PLUGIN_VERSIONS},
                "v10_checkpoint_seal_origin": create_seal_origin(settings, run.id, run.workspace_id)}
        ready = tmp_path / "sealed-write-ready"
        env = os.environ.copy()
        env.update({"V10_TEST_DATABASE_URL": settings.database_url, "V10_TEST_DATA_DIR": str(settings.data_dir),
            "V10_TEST_SECRET_DIR": str(settings.secret_dir), "V10_TEST_RUN_ID": run_id,
            "V10_TEST_CHECKPOINT_SCHEMA": schema, "V10_TEST_BUSINESS_SCHEMA": business_schema,
            "V10_TEST_READY_FILE": str(ready)})
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("v10_sealed_crash_child.py"))],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 20
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            assert ready.exists(), "Child failed to reach the uncommitted checkpoint write"
        finally:
            if child.poll() is None:
                child.kill()
            await asyncio.to_thread(child.wait, 10)
        with factory() as db:
            assert db.execute(text(f"SELECT count(*) FROM {schema}.checkpoints WHERE thread_id=:run"),
                              {"run": run_id}).scalar_one() == 0
            assert not list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id,
                OperationRecord.node_id == "v10-checkpoint-seal-v1")))
            run = db.get(Run, run_id)
            assert run.config_snapshot["v10_checkpoint_seal_origin"]["state"] == "unused"
            assert not (run.outcome or {}).get("v10_checkpoint_seal_head")


@pytest.mark.asyncio
async def test_small_scale_sealing_records_elapsed_and_python_memory(postgres_environment, tmp_path, capsys):
    import time
    import tracemalloc
    from copy import deepcopy

    import psycopg
    from langgraph.checkpoint.base import empty_checkpoint
    from sqlalchemy import text

    from omniharness.checkpoint_fencing import _conninfo

    settings, engine, factory, business_schema = postgres_environment
    schema = "v10_scale_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=schema)

    class State(TypedDict):
        value: str

    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, *_ = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            run.workflow_version = "stage-agent-v10"
            run.config_snapshot = {**run.config_snapshot, "agent": {**run.config_snapshot["agent"],
                "workflow_version": "stage-agent-v10", "plugin_versions": SEALED_PLUGIN_VERSIONS},
                "v10_checkpoint_seal_origin": create_seal_origin(settings, run.id, run.workspace_id)}
        with WorkerLock(engine, settings) as owner:
            ticket = register_fence(owner, run_id, "stage-agent-v10", schema=schema)
            async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as base:
                saver = V10SealedSaver(base.conn, ticket, business_schema=business_schema, settings=settings)
                builder = StateGraph(State)
                builder.add_node("step", lambda state: {"value": state["value"]})
                builder.add_edge(START, "step")
                builder.add_edge("step", END)
                graph = builder.compile(checkpointer=saver)
                config = {"configurable": {"thread_id": run_id}}
                await graph.ainvoke({"value": "initial"}, config, durability="sync")
                tracemalloc.start()
                try:
                    start = time.perf_counter()
                    for i in range(10):
                        await graph.aupdate_state(config, {"value": "x" * 1024 + str(i)})
                    first = time.perf_counter() - start
                    start = time.perf_counter()
                    for i in range(10, 30):
                        await graph.aupdate_state(config, {"value": "x" * 1024 + str(i)})
                    latter = time.perf_counter() - start
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                with factory() as db:
                    head = db.get(Run, run_id).outcome["v10_checkpoint_seal_head"]
                assert head["sequence"] >= 30
                # This is a bounded sample, not a throughput or memory limit.
                print(f"V10_SEAL_SCALE first10_s={first:.3f} next20_s={latter:.3f} "
                      f"python_peak_mib={peak / 1024 / 1024:.2f} seals={head['sequence']}")
                # Terminate the saver database backend after checkpoint SQL and
                # before seal insertion. The independent business connection
                # must observe neither part of the interrupted transaction.
                root = await graph.aget_state(config)
                root_id = root.config["configurable"]["checkpoint_id"]
                with factory() as db:
                    before = deepcopy(db.get(Run, run_id).outcome)
                cursor = await base.conn.execute("SELECT pg_backend_pid() AS pid")
                pid = (await cursor.fetchone())["pid"]
                killer = await psycopg.AsyncConnection.connect(_conninfo(_checkpoint_url(settings.database_url)),
                                                               autocommit=True)
                original_inventory = saver._inventory
                calls = 0
                async def terminate_after_checkpoint(cur):
                    nonlocal calls
                    calls += 1
                    result = await original_inventory(cur)
                    if calls == 2:
                        await killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
                    return result
                saver._inventory = terminate_after_checkpoint
                try:
                    with pytest.raises(psycopg.OperationalError):
                        await saver.aput({"configurable": {"thread_id": run_id, "checkpoint_ns": "",
                            "checkpoint_id": root_id}}, empty_checkpoint(),
                            {"source": "input", "step": -1, "parents": {}}, {})
                finally:
                    await killer.close()
                with factory() as db:
                    latest = db.execute(text(f"SELECT checkpoint_id FROM {schema}.checkpoints "
                        "WHERE thread_id=:run AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1"),
                        {"run": run_id}).scalar_one()
                    assert latest == root_id
                    assert db.get(Run, run_id).outcome == before


@pytest.mark.asyncio
async def test_owner_loss_after_checkpoint_write_rolls_back_seal(postgres_environment, tmp_path):
    from langgraph.checkpoint.base import empty_checkpoint
    from sqlalchemy import text

    from omniharness.v10_sealed_saver import SealedSaverUnavailable

    settings, engine, factory, business_schema = postgres_environment
    schema = "v10_owner_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=schema)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, *_ = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            run.workflow_version = "stage-agent-v10"
            run.config_snapshot = {**run.config_snapshot, "agent": {**run.config_snapshot["agent"],
                "workflow_version": "stage-agent-v10", "plugin_versions": SEALED_PLUGIN_VERSIONS},
                "v10_checkpoint_seal_origin": create_seal_origin(settings, run.id, run.workspace_id)}
        with WorkerLock(engine, settings) as owner:
            ticket = register_fence(owner, run_id, "stage-agent-v10", schema=schema)
            async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as base:
                saver = V10SealedSaver(base.conn, ticket, business_schema=business_schema, settings=settings)
                inventory = saver._inventory
                calls = 0
                async def lose_owner(cur):
                    nonlocal calls
                    calls += 1
                    result = await inventory(cur)
                    if calls == 2:
                        owner.connection.invalidate()
                        owner.lost = True
                    return result
                saver._inventory = lose_owner
                with pytest.raises(SealedSaverUnavailable):
                    await saver.aput({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}},
                        empty_checkpoint(), {"source": "input", "step": -1, "parents": {}}, {})
                with factory() as db:
                    assert db.execute(text(f"SELECT count(*) FROM {schema}.checkpoints WHERE thread_id=:run"),
                                      {"run": run_id}).scalar_one() == 0
                    assert not list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id,
                        OperationRecord.node_id == "v10-checkpoint-seal-v1")))
                    assert db.get(Run, run_id).config_snapshot["v10_checkpoint_seal_origin"]["state"] == "unused"
