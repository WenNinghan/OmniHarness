"""Disposable PostgreSQL tests for the saver-local write barrier."""

import asyncio
import sys
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import select
from test_m4_collaboration import prepare_v4
from test_postgres import postgres_environment  # noqa: F401

from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema, open_fenced_saver, register_fence
from omniharness.m3_worker import _checkpoint_url
from omniharness.models import OperationRecord, Run
from omniharness.security import create_pairing_code
from omniharness.v10_sealed_saver import (
    SEALED_PLUGIN_VERSIONS,
    SealedSaverUnavailable,
    V10SealedSaver,
    create_seal_origin,
)
from omniharness.worker import WorkerLock


@pytest.fixture(scope="module")
def event_loop_policy():
    return (
        asyncio.WindowsSelectorEventLoopPolicy()
        if sys.platform == "win32"
        else asyncio.get_event_loop_policy()
    )


@asynccontextmanager
async def prepared(pg, tmp_path, *, create_writer=True, legacy=False, unknown=False):
    settings, engine, factory, business_schema = pg
    schema = "v10_drain_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=schema)
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert (
            client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        )
        worker, run_id, *_ = prepare_v4((settings, engine, factory, client), tmp_path)
        with factory.begin() as db:
            run = db.get(Run, run_id)
            run.workflow_version = "stage-agent-v10"
            run.config_snapshot = {
                **run.config_snapshot,
                "agent": {
                    **run.config_snapshot["agent"],
                    "workflow_version": "stage-agent-v10",
                    "plugin_versions": SEALED_PLUGIN_VERSIONS,
                },
                "v10_checkpoint_seal_origin": create_seal_origin(settings, run.id, run.workspace_id),
            }
            if legacy or unknown:
                from omniharness.m4_contract import M4_V10_PLUGIN_VERSIONS

                snapshot = {
                    key: value
                    for key, value in run.config_snapshot.items()
                    if key != "v10_checkpoint_seal_origin"
                }
                snapshot["agent"] = {
                    **snapshot["agent"],
                    "plugin_versions": {"unknown": "1"} if unknown else M4_V10_PLUGIN_VERSIONS,
                }
                run.config_snapshot = snapshot
        with WorkerLock(engine, settings) as owner:
            worker.lock = owner
            ticket = register_fence(owner, run_id, "stage-agent-v10", schema=schema)
            async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as base:
                saver = (
                    V10SealedSaver(base.conn, ticket, business_schema=business_schema, settings=settings)
                    if create_writer
                    else None
                )
                yield saver, run_id, factory, base, ticket, owner, settings


def input_checkpoint(run_id):
    config = {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"items": ["one"]}
    checkpoint["channel_versions"] = {"items": "v1"}
    return config, checkpoint, {"source": "input", "step": -1, "parents": {}}, {"items": "v1"}


@pytest.mark.asyncio
async def test_preprocessing_started_before_barrier_cannot_write(request, tmp_path, monkeypatch):
    pg = request.getfixturevalue("postgres_environment")
    async with prepared(pg, tmp_path) as (saver, run_id, factory, base, ticket, owner, settings):
        args = input_checkpoint(run_id)
        entered, release = threading.Event(), threading.Event()
        original = saver._dump_blobs

        def slow_dump(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("fixture timeout")
            return original(*args)

        monkeypatch.setattr(saver, "_dump_blobs", slow_dump)
        writer = asyncio.create_task(saver.aput(*args))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            closer = asyncio.create_task(saver.close_writes())
            await asyncio.sleep(0)
            assert not closer.done()
            release.set()
            with pytest.raises(SealedSaverUnavailable, match="writes are closed"):
                await writer
            await asyncio.wait_for(closer, 5)
            assert saver._active_writes == 0
            saver.assert_write_drain(ticket)
            with pytest.raises(SealedSaverUnavailable, match="writes are closed"):
                await saver.aput(*args)
            await saver.close_writes()
            assert await saver.aget_tuple(args[0]) is None
            with factory() as db:
                assert not list(
                    db.scalars(
                        select(OperationRecord).where(
                            OperationRecord.run_id == run_id,
                            OperationRecord.node_id == "v10-checkpoint-seal-v1",
                        )
                    )
                )
        finally:
            release.set()
            if not writer.done():
                await writer


@pytest.mark.asyncio
async def test_barrier_waits_for_write_already_holding_saver_lock(request, tmp_path, monkeypatch):
    pg = request.getfixturevalue("postgres_environment")
    async with prepared(pg, tmp_path) as (saver, run_id, factory, base, ticket, owner, settings):
        args = input_checkpoint(run_id)
        entered, release = asyncio.Event(), asyncio.Event()
        original = saver._inventory
        first = True

        async def slow_inventory(cur):
            nonlocal first
            if first:
                first = False
                entered.set()
                await release.wait()
            return await original(cur)

        monkeypatch.setattr(saver, "_inventory", slow_inventory)
        writer = asyncio.create_task(saver.aput(*args))
        await asyncio.wait_for(entered.wait(), 5)
        closer = asyncio.create_task(saver.close_writes())
        await asyncio.sleep(0)
        assert not closer.done()
        release.set()
        await asyncio.wait_for(writer, 5)
        await asyncio.wait_for(closer, 5)
        with pytest.raises(SealedSaverUnavailable, match="writes are closed"):
            await saver.aput(*args)
        assert (await saver.aget_tuple(args[0])) is not None
        with factory() as db:
            rows = list(
                db.scalars(
                    select(OperationRecord).where(
                        OperationRecord.run_id == run_id, OperationRecord.node_id == "v10-checkpoint-seal-v1"
                    )
                )
            )
            assert len(rows) == 1


@pytest.mark.asyncio
async def test_ordinary_and_duplicate_writer_are_blocked_after_sealed_registration(request, tmp_path):
    pg = request.getfixturevalue("postgres_environment")
    async with prepared(pg, tmp_path) as (saver, run_id, factory, base, ticket, owner, settings):
        from omniharness.v10_saver_guard import SaverPhaseUnavailable

        args = input_checkpoint(run_id)
        with pytest.raises(SaverPhaseUnavailable):
            await base.aput(*args)
        with pytest.raises(SaverPhaseUnavailable):
            V10SealedSaver(base.conn, ticket, business_schema=saver.business_schema, settings=settings)
        await saver.close_writes()
        with pytest.raises(SaverPhaseUnavailable):
            await base.aput(*args)
        with pytest.raises(SaverPhaseUnavailable):
            V10SealedSaver(base.conn, ticket, business_schema=saver.business_schema, settings=settings)
        assert await base.aget_tuple(args[0]) is None


@pytest.mark.asyncio
async def test_persisted_sealed_run_blocks_ordinary_before_registration(request, tmp_path):
    from omniharness.checkpoint_fencing import SEALED_V10_FENCE_VERSION
    from omniharness.v10_saver_guard import SaverPhaseUnavailable

    pg = request.getfixturevalue("postgres_environment")
    async with prepared(pg, tmp_path, create_writer=False) as (
        _,
        run_id,
        factory,
        base,
        ticket,
        owner,
        settings,
    ):
        assert ticket.workflow_version == SEALED_V10_FENCE_VERSION
        args = input_checkpoint(run_id)
        with pytest.raises(SaverPhaseUnavailable):
            await base.aput(*args)
        assert await base.aget_tuple(args[0]) is None


@pytest.mark.asyncio
async def test_unknown_v10_configuration_is_rejected_at_fence(request, tmp_path):
    from omniharness.checkpoint_fencing import FencingError

    pg = request.getfixturevalue("postgres_environment")
    with pytest.raises(FencingError):
        async with prepared(pg, tmp_path, create_writer=False, unknown=True):
            raise AssertionError("Unknown v10 must not acquire a ticket")


@pytest.mark.asyncio
async def test_legacy_v10_ordinary_saver_still_writes_transactionally(request, tmp_path):
    pg = request.getfixturevalue("postgres_environment")
    async with prepared(pg, tmp_path, create_writer=False, legacy=True) as (
        _,
        run_id,
        factory,
        base,
        ticket,
        owner,
        settings,
    ):
        assert ticket.workflow_version == "stage-agent-v10"
        args = input_checkpoint(run_id)
        result = await base.aput(*args)
        await base.aput_writes(result, [("result", {"value": 1})], "graph-task")
        stored = await base.aget_tuple(result)
        assert stored.checkpoint["channel_values"]["items"] == ["one"]
        assert stored.pending_writes[0][1:] == ("result", {"value": 1})


@pytest.mark.asyncio
async def test_other_read_context_does_not_retire_active_writer(request, tmp_path):
    pg = request.getfixturevalue("postgres_environment")
    async with prepared(pg, tmp_path) as (saver, run_id, factory, base, ticket, owner, settings):
        async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as other:
            assert other.conn is not saver.conn
            assert (
                await other.aget_tuple({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}) is None
            )
        args = input_checkpoint(run_id)
        await saver.aput(*args)
        assert await saver.aget_tuple(args[0]) is not None


@pytest.mark.asyncio
async def test_new_owner_gets_durable_sealed_marker_before_any_writer(request, tmp_path):
    from omniharness.checkpoint_fencing import SEALED_V10_FENCE_VERSION
    from omniharness.v10_saver_guard import SaverPhaseUnavailable

    pg = request.getfixturevalue("postgres_environment")
    _, engine, _, _ = pg
    async with prepared(pg, tmp_path) as (saver, run_id, factory, base, ticket, owner, settings):
        args = input_checkpoint(run_id)
        await saver.aput(*args)
        schema, old_epoch = ticket.schema, ticket.epoch
    with WorkerLock(engine, settings) as new_owner:
        new_ticket = register_fence(new_owner, run_id, "stage-agent-v10", schema=schema)
        assert new_ticket.epoch > old_epoch
        assert new_ticket.workflow_version == SEALED_V10_FENCE_VERSION
        async with open_fenced_saver(_checkpoint_url(settings.database_url), new_ticket) as ordinary:
            with pytest.raises(SaverPhaseUnavailable, match="rejects ordinary"):
                await ordinary.aput(*args)


@pytest.mark.asyncio
async def test_same_owner_resume_needs_pinned_grant_and_old_writer_stays_closed(request, tmp_path):
    from omniharness.v10_saver_guard import SaverPhaseUnavailable, authorize_same_owner_resume

    pg = request.getfixturevalue("postgres_environment")
    async with prepared(pg, tmp_path) as (saver, run_id, factory, base, ticket, owner, settings):
        args = input_checkpoint(run_id)
        root = (await saver.aput(*args))["configurable"]["checkpoint_id"]
        with factory.begin() as db:
            db.get(Run, run_id).status = "awaiting_user"
        await saver.retire_after_context()
        with pytest.raises(SealedSaverUnavailable):
            await saver.aput(*args)
        with pytest.raises(SaverPhaseUnavailable):
            V10SealedSaver(base.conn, ticket, business_schema=saver.business_schema, settings=settings)
        with factory.begin() as db:
            db.get(Run, run_id).status = "running"
        with owner.session(factory) as db:
            authorize_same_owner_resume(db, ticket, root, settings)
        resumed = V10SealedSaver(base.conn, ticket, business_schema=saver.business_schema, settings=settings)
        with pytest.raises(SealedSaverUnavailable):
            await saver.aput(*args)
        with factory.begin() as db:
            db.get(Run, run_id).status = "completed"
        await resumed.retire_after_context()
        with pytest.raises(SaverPhaseUnavailable):
            V10SealedSaver(base.conn, ticket, business_schema=saver.business_schema, settings=settings)


@pytest.mark.asyncio
async def test_failed_open_revokes_unconsumed_resume_grant(request, tmp_path):
    from omniharness.checkpoint_fencing import FencingError
    from omniharness.v10_saver_guard import SaverPhaseUnavailable, authorize_same_owner_resume

    pg = request.getfixturevalue("postgres_environment")
    async with prepared(pg, tmp_path) as (saver, run_id, factory, base, ticket, owner, settings):
        root = (await saver.aput(*input_checkpoint(run_id)))["configurable"]["checkpoint_id"]
        with factory.begin() as db:
            db.get(Run, run_id).status = "awaiting_user"
        await saver.retire_after_context()
        with factory.begin() as db:
            db.get(Run, run_id).status = "running"
        with owner.session(factory) as db:
            authorize_same_owner_resume(db, ticket, root, settings)
        with pytest.raises(FencingError):
            async with open_fenced_saver("postgresql://invalid/test", ticket):
                raise AssertionError("connection should fail")
        with pytest.raises(SaverPhaseUnavailable):
            V10SealedSaver(base.conn, ticket, business_schema=saver.business_schema, settings=settings)


@pytest.mark.asyncio
async def test_resume_refused_until_old_preprocessing_has_exited(request, tmp_path, monkeypatch):
    from omniharness.v10_saver_guard import SaverPhaseUnavailable, authorize_same_owner_resume

    pg = request.getfixturevalue("postgres_environment")
    async with prepared(pg, tmp_path) as (saver, run_id, factory, base, ticket, owner, settings):
        args = input_checkpoint(run_id)
        root = (await saver.aput(*args))["configurable"]["checkpoint_id"]
        entered, release = threading.Event(), threading.Event()
        original = saver._dump_blobs

        def slow_dump(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("fixture timeout")
            return original(*args)

        monkeypatch.setattr(saver, "_dump_blobs", slow_dump)
        late = asyncio.create_task(saver.aput(*input_checkpoint(run_id)))
        retire = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            with factory.begin() as db:
                db.get(Run, run_id).status = "awaiting_user"
            retire = asyncio.create_task(saver.retire_after_context())
            await asyncio.sleep(0.05)
            assert not retire.done()
            with factory.begin() as db:
                db.get(Run, run_id).status = "running"
            with pytest.raises(SaverPhaseUnavailable), owner.session(factory) as db:
                authorize_same_owner_resume(db, ticket, root, settings)
            release.set()
            with pytest.raises(SealedSaverUnavailable, match="writes are closed"):
                await late
            await asyncio.wait_for(retire, 5)
            with owner.session(factory) as db:
                authorize_same_owner_resume(db, ticket, root, settings)
            resumed = V10SealedSaver(
                base.conn, ticket, business_schema=saver.business_schema, settings=settings
            )
            assert resumed is not saver
        finally:
            release.set()
            if not late.done():
                with pytest.raises(SealedSaverUnavailable):
                    await late
            if retire is not None and not retire.done():
                await retire
