"""No model calls: real sealed checkpoints and a test-only synthetic closure gate."""

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import TypedDict
from uuid import uuid4

import pytest
from conftest import create_chat
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from sqlalchemy import event, select
from test_postgres import postgres_environment  # noqa: F401

from omniharness import correction_reservation as reservations
from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema, open_fenced_saver, register_fence
from omniharness.m3_worker import _checkpoint_url
from omniharness.models import (
    Message,
    OperationRecord,
    Run,
    RunBudget,
    RunBudgetReservation,
    Workspace,
    WorkspaceLease,
)
from omniharness.security import create_pairing_code
from omniharness.services import BudgetLimits, ensure_budget
from omniharness.v10_sealed_saver import SEALED_PLUGIN_VERSIONS, V10SealedSaver, create_seal_origin
from omniharness.worker import Worker, WorkerLock


@pytest.fixture(scope="module")
def event_loop_policy():
    return (
        asyncio.WindowsSelectorEventLoopPolicy()
        if sys.platform == "win32"
        else asyncio.get_event_loop_policy()
    )


@pytest.mark.asyncio
async def test_missing_owner_never_reserves():
    worker = type("Worker", (), {"lock": None})()
    with pytest.raises(reservations.CorrectionReservationUnavailable):
        await reservations.reserve_correction_model(
            worker,
            "run",
            ticket=None,
            expected_checkpoint_id="checkpoint",
            graph=None,
            executor=None,
            checkpointer=None,
        )
    assert reservations.correction_invocation_id("run") == reservations.correction_invocation_id("run")
    assert reservations.correction_invocation_id("run") != reservations.correction_invocation_id("other")


def prepare(pg):
    settings, engine, factory, _ = pg
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert (
            client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        )
        _, _, queued = create_chat(client)
    run_id, workspace_id = queued["run_id"], str(uuid4())
    with factory.begin() as db:
        db.add(Workspace(id=workspace_id, name="isolated", host_path="/fixture", enabled=True))
        db.flush()
        run = db.get(Run, run_id)
        run.workspace_id, run.workflow_version, run.status = workspace_id, "stage-agent-v10", "running"
        run.config_snapshot = {
            **run.config_snapshot,
            "agent": {"plugin_versions": SEALED_PLUGIN_VERSIONS},
            "v10_checkpoint_seal_origin": create_seal_origin(settings, run_id, workspace_id),
        }
        ensure_budget(
            db,
            run_id,
            BudgetLimits(max_model_calls=2, max_tool_calls=2, max_revisions=1, max_active_seconds=60),
        )
    return run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_listener_after_commit", [False, True])
async def test_postgres_real_pin_then_fail_closed_and_atomic_ledger(
    request, monkeypatch, lose_listener_after_commit
):
    pg = request.getfixturevalue("postgres_environment")
    settings, engine, factory, business_schema = pg
    schema = "v10_reservation_" + uuid4().hex[:12]
    await initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=schema)
    run_id = prepare(pg)
    worker = Worker(settings, engine)

    class State(TypedDict):
        value: str

    with WorkerLock(engine, settings) as owner:
        worker.lock = owner
        from omniharness.v10_owner_auth import make_run_origin, new_owner_identity
        from omniharness.v10_owner_runtime import V10OwnerListener

        with worker.transaction() as db:
            from sqlalchemy import text

            identity_row = (
                db.execute(
                    text(
                        "SELECT pg_backend_pid() AS pid, backend_start FROM pg_stat_activity "
                        "WHERE pid=pg_backend_pid()"
                    )
                )
                .mappings()
                .one()
            )
            identity = new_owner_identity(
                identity_row["pid"], identity_row["backend_start"], settings.instance_id
            )
            run = db.get(Run, run_id)
            message = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
            origin = make_run_origin(settings, identity, run, message)
            run.config_snapshot = {**run.config_snapshot, "v10_owner_origin": origin}
            db.add(WorkspaceLease(workspace_id=run.workspace_id, run_id=run_id))
        # Test-only synthetic listener enrollment; production accepts only its
        # actual notification/authentication flow.
        listener = object.__new__(V10OwnerListener)
        listener.worker, listener.identity, listener.alive = worker, identity, True
        listener.connection = object()
        listener.pending, listener.committing, listener.accepted = {}, {}, {run_id: origin}
        listener.consumed, listener.tainted = set(), set()
        listener._correction_receipts = {}
        listener._correction_phases = {}

        async def live():
            return True

        monkeypatch.setattr(listener, "_live", live)
        worker._v10_owner_listener = listener

        def reenroll():
            listener.accepted[run_id] = origin
            listener.consumed.discard(run_id)
            listener.tainted.discard(run_id)

        ticket = register_fence(owner, run_id, "stage-agent-v10", schema=schema)
        async with open_fenced_saver(_checkpoint_url(settings.database_url), ticket) as base:
            saver = V10SealedSaver(base.conn, ticket, business_schema=business_schema, settings=settings)
            builder = StateGraph(State)
            builder.add_node("step", lambda state: {"value": state["value"] + "!"})
            builder.add_edge(START, "step")
            builder.add_edge("step", END)
            graph = builder.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": run_id}}
            await graph.ainvoke({"value": "fixture"}, config, durability="sync")
            checkpoint = (await graph.aget_state(config)).config["configurable"]["checkpoint_id"]
            # Without host-owned scheduler drain proof, reject before pinning.
            with pytest.raises(reservations.CorrectionReservationUnavailable, match="scheduler drain"):
                await reservations.reserve_correction_model(
                    worker,
                    run_id,
                    ticket=ticket,
                    expected_checkpoint_id=checkpoint,
                    graph=graph,
                    executor=graph,
                    checkpointer=saver,
                )
            with factory() as db:
                assert db.get(RunBudget, run_id).model_calls == 0
            reenroll()
            # Test-only synthetic drain proof isolates the transaction paths.
            monkeypatch.setattr(
                reservations, "_require_drained_scheduler", lambda worker, run_id, **kwargs: None
            )
            # Test-only budget fixture: real monotonic segment is exercised by
            # the full host integration, not by this synthetic graph.
            monkeypatch.setattr(reservations, "remaining_active_budget", lambda *args: 60)
            monkeypatch.setattr(reservations, "validate_execute_host", lambda *args: {})
            # Test-only simulated graph inventory and action scope; still
            # perform the real pinned checkpoint-seal verification.
            from omniharness import v10_closure
            from omniharness.v10_sealed_saver import verify_pinned_seal_inventory

            async def inventory(
                worker, ticket, graph, executor, checkpointer, root, *, owner_db, require_seal
            ):
                assert require_seal is True
                verify_pinned_seal_inventory(owner_db, ticket, root, worker.settings)
                return True, "synthetic-inventory-screen"

            async def scope(db, worker, ticket, graph, root):
                return True, "b" * 64

            monkeypatch.setattr(v10_closure, "inspect_pinned_zero_tool_inventory", inventory)
            monkeypatch.setattr(v10_closure, "screen_zero_tool_correction_scope", scope)

            async def stuck(*args, **kwargs):
                await asyncio.sleep(60)

            # Simulate a saver lock stall. Timeout must roll back the owner
            # transaction and release the pinned fence for another session.
            with monkeypatch.context() as patcher:
                patcher.setattr(v10_closure, "inspect_pinned_zero_tool_inventory", stuck)
                patcher.setattr(reservations, "_PINNED_READ_TIMEOUT_SECONDS", 0.05)
                with pytest.raises(TimeoutError):
                    await reservations.reserve_correction_model(
                        worker,
                        run_id,
                        ticket=ticket,
                        expected_checkpoint_id=checkpoint,
                        graph=graph,
                        executor=graph,
                        checkpointer=saver,
                    )
            with factory.begin() as db:
                from sqlalchemy import text

                db.execute(text("SET LOCAL lock_timeout = '500ms'"))
                assert (
                    db.execute(
                        text(f"SELECT epoch FROM {schema}.fences WHERE thread_id=:run FOR UPDATE"),
                        {"run": run_id},
                    ).scalar_one()
                    == ticket.epoch
                )
            reenroll()
            statements = []

            def capture(conn, cursor, statement, parameters, context, executemany):
                if "FOR UPDATE" in statement.upper():
                    statements.append(statement.lower())

            def reject_closure(*args, **kwargs):
                raise reservations.CorrectionReservationUnavailable("synthetic closure rejection")

            event.listen(engine, "before_cursor_execute", capture)
            try:
                with monkeypatch.context() as patcher:
                    patcher.setattr(reservations, "_require_closed_inventory", reject_closure)
                    with pytest.raises(reservations.CorrectionReservationUnavailable, match="closure"):
                        await reservations.reserve_correction_model(
                            worker,
                            run_id,
                            ticket=ticket,
                            expected_checkpoint_id=checkpoint,
                            graph=graph,
                            executor=graph,
                            checkpointer=saver,
                        )
            finally:
                event.remove(engine, "before_cursor_execute", capture)
            assert next(i for i, sql in enumerate(statements) if ".fences" in sql) < next(
                i for i, sql in enumerate(statements) if "from runs" in sql
            )
            with factory() as db:
                assert db.get(RunBudget, run_id).model_calls == 0
                assert (
                    db.get(RunBudgetReservation, (run_id, reservations.correction_invocation_id(run_id)))
                    is None
                )
                assert not list(
                    db.scalars(
                        select(OperationRecord).where(
                            OperationRecord.run_id == run_id,
                            OperationRecord.node_id == "v10-correction:model_calls",
                        )
                    )
                )
                original_outcome = deepcopy(db.get(Run, run_id).outcome)
            reenroll()
            # A changed seal is rejected BEFORE the unavailable closure gate.
            with factory.begin() as db:
                run = db.get(Run, run_id)
                modified = deepcopy(run.outcome)
                modified["v10_checkpoint_seal_head"]["sequence"] += 1
                run.outcome = modified
            from omniharness.v10_sealed_saver import SealedSaverUnavailable

            with pytest.raises(SealedSaverUnavailable):
                await reservations.reserve_correction_model(
                    worker,
                    run_id,
                    ticket=ticket,
                    expected_checkpoint_id=checkpoint,
                    graph=graph,
                    executor=graph,
                    checkpointer=saver,
                )
            with factory.begin() as db:
                db.get(Run, run_id).outcome = original_outcome

            reenroll()
            # Test seam ONLY: actual seal verification above is real; this
            # digest simulates the still-unimplemented complete closure proof.
            monkeypatch.setattr(
                reservations, "_require_closed_inventory", lambda db, run, worker, seal, **kwargs: "a" * 64
            )

            def fail_before_ledger(*args, **kwargs):
                raise RuntimeError("simulated abort before ledger")

            with monkeypatch.context() as patcher:
                patcher.setattr(reservations, "begin_operation", fail_before_ledger)
                with pytest.raises(RuntimeError, match="simulated abort"):
                    await reservations.reserve_correction_model(
                        worker,
                        run_id,
                        ticket=ticket,
                        expected_checkpoint_id=checkpoint,
                        graph=graph,
                        executor=graph,
                        checkpointer=saver,
                    )
            with factory() as db:
                assert db.get(RunBudget, run_id).model_calls == 0
                assert (
                    db.get(RunBudgetReservation, (run_id, reservations.correction_invocation_id(run_id)))
                    is None
                )
            reenroll()

            def competitor(_):
                try:
                    with WorkerLock(engine, settings):
                        return "entered"
                except RuntimeError:
                    return "rejected"

            with ThreadPoolExecutor(max_workers=6) as pool:
                assert list(pool.map(competitor, range(6))) == ["rejected"] * 6
            live_checks = []

            async def final_live():
                live_checks.append(True)
                return not lose_listener_after_commit or len(live_checks) == 1

            monkeypatch.setattr(listener, "_live", final_live)
            result = await reservations.reserve_correction_model(
                worker,
                run_id,
                ticket=ticket,
                expected_checkpoint_id=checkpoint,
                graph=graph,
                executor=graph,
                checkpointer=saver,
            )
            assert len(live_checks) == 2
            assert result["status"] == ("PENDING_REVIEW" if lose_listener_after_commit else "RESERVED")
            assert result["dispatch"] is (not lose_listener_after_commit)
            receipt = listener._correction_receipts.get(run_id)
            if lose_listener_after_commit:
                assert receipt is None and run_id in listener.tainted
            else:
                assert receipt is not None
                assert receipt.run_id == run_id and receipt.ticket == ticket
                assert receipt.owner is owner and receipt.owner_connection is owner.connection
                assert receipt.listener_connection is listener.connection
                assert receipt.root_id == checkpoint and receipt.operation_id == result["operation_id"]
                assert receipt.closure_fingerprint == "a" * 64
            with factory() as db:
                assert db.get(RunBudget, run_id).model_calls == 1
                assert db.get(OperationRecord, result["operation_id"]).status == "running"
            listener._invalidate()
            assert run_id not in listener._correction_receipts
            assert run_id in listener.tainted
        owner.connection.invalidate()
        owner.lost = True
    # Disconnecting the client does not synchronously remove its backend or
    # release its session advisory lock on PostgreSQL. Observe that exact
    # backend ending before testing the replacement owner's acquisition.
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        with engine.connect() as connection:
            previous_owner_exists = connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                     "WHERE pid = :pid AND backend_start = :backend_start)"),
                {"pid": identity_row["pid"], "backend_start": identity_row["backend_start"]},
            )
        if not previous_owner_exists:
            break
        assert asyncio.get_running_loop().time() < deadline, "Previous owner backend did not exit"
        await asyncio.sleep(0.01)
    replacement = Worker(settings, engine)
    with WorkerLock(engine, settings) as new_owner:
        replacement.lock = new_owner
        new_ticket = register_fence(new_owner, run_id, "stage-agent-v10", schema=schema)
        recovered = await reservations.reserve_correction_model(
            replacement,
            run_id,
            ticket=new_ticket,
            expected_checkpoint_id=checkpoint,
            graph=None,
            executor=None,
            checkpointer=None,
        )
        assert recovered["status"] == "PENDING_REVIEW" and recovered["dispatch"] is False
    with factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 1
        assert db.get(RunBudgetReservation, (run_id, result["invocation_id"])) is not None
