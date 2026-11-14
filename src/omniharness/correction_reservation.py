"""Fail-closed v10 once-per-Run correction model reservation.

The narrow zero-tool proof and durable reservation are isolated from the
Worker dispatcher. Existing reservations are never replayed, including after
owner loss. This module does not send model or tool requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time

from sqlalchemy import select, text

from .m3_budget import ReservationResult, reserve_invocation
from .models import Message, ModelConfig, OperationRecord, Provider, Run, RunBudget, RunBudgetReservation
from .services import begin_operation, operation_id

_NODE = "v10-correction:model_calls"
_HEX = re.compile(r"^[a-f0-9]{64}$")
_PINNED_READ_TIMEOUT_SECONDS = 5


class CorrectionReservationUnavailable(RuntimeError):
    """The transaction, scope or required closure cannot be established."""


class _CorrectionBudgetExhausted(RuntimeError):
    pass


def correction_invocation_id(run_id):
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Run identity required")
    return hashlib.sha256(("omniharness/v10/correction/model/1:" + run_id).encode()).hexdigest()


def _require_drained_scheduler(worker, run_id, *, ticket, graph, executor, checkpointer):
    """Require the fixed host-assembled, one-owner execution drain."""
    from .v10_drain import DrainUnavailable, get_drained_execution

    try:
        return get_drained_execution(
            worker, run_id, ticket=ticket, graph=graph, executor=executor, saver=checkpointer
        )
    except DrainUnavailable:
        raise CorrectionReservationUnavailable("Verified scheduler drain is unavailable") from None


def validate_execute_host(db, run, proof):
    """Reuse the exact assembled host and recheck current model/provider versions."""
    host = getattr(proof, "host", None)
    if host is None or getattr(host, "run_id", None) != run.id:
        raise CorrectionReservationUnavailable("Frozen execute host is unavailable")
    checked, role = host._run(db, "execute")
    snapshot = role.get("model") if isinstance(role, dict) else None
    if checked.id != run.id or not isinstance(snapshot, dict):
        raise CorrectionReservationUnavailable("Frozen execute model is unavailable")
    model = db.scalar(select(ModelConfig).where(ModelConfig.id == snapshot.get("id")).with_for_update())
    provider = (
        db.scalar(select(Provider).where(Provider.id == model.provider_id).with_for_update())
        if model
        else None
    )
    if (
        not model
        or not provider
        or not provider.enabled
        or model.version != snapshot.get("version")
        or provider.version != snapshot.get("provider_version")
    ):
        raise CorrectionReservationUnavailable("Execute model or provider changed")
    return role


def correction_intent_mac(settings, run, user):
    """Authenticate exact frozen user, action, workspace and execute inputs."""
    from .m4_invalid_audit import _key, _mac

    if user is None or user.run_id != run.id or user.role != "user":
        raise CorrectionReservationUnavailable("Original user request unavailable")
    snapshot = run.config_snapshot if isinstance(run.config_snapshot, dict) else {}
    return _mac(
        _key(settings, "stage-agent-v10"),
        "v10-correction-intent",
        {
            "run_id": run.id,
            "workspace_id": run.workspace_id,
            "user_id": user.id,
            "user_content": user.content,
            "action_contract": snapshot.get("action_contract"),
            "actions": (run.outcome or {}).get("v10_actions"),
            "workspace": snapshot.get("workspace"),
            "agent": snapshot.get("agent"),
        },
    )


def remaining_active_budget(db, run_id, proof, action_count, *, model_already_reserved=False):
    """Recheck the live segment against the already-frozen Run budgets."""
    budget = db.scalar(select(RunBudget).where(RunBudget.run_id == run_id).with_for_update())
    started = getattr(proof, "segment_started", None)
    now = time.monotonic()
    if (
        budget is None
        or budget.active_segment_open is not True
        or type(started) not in (int, float)
        or not math.isfinite(started)
        or started < 0
        or started > now
        or type(action_count) is not int
        or not 1 <= action_count <= 2
    ):
        raise CorrectionReservationUnavailable("Live active budget evidence is unavailable")
    remaining = budget.max_active_seconds - budget.active_seconds - math.ceil(now - started)
    if (
        budget.model_calls < 0
        or budget.tool_calls < 0
        or budget.active_seconds < 0
        or budget.max_model_calls < 1
        or budget.max_tool_calls < 0
        or budget.max_active_seconds < 1
    ):
        raise CorrectionReservationUnavailable("Frozen Run budget counters are invalid")
    model_over = (
        not 1 <= budget.model_calls <= budget.max_model_calls
        if model_already_reserved
        else budget.model_calls >= budget.max_model_calls
    )
    if remaining <= 0 or model_over or budget.max_tool_calls - budget.tool_calls < action_count:
        return 0
    return remaining


def _require_closed_inventory(
    db, run, worker, scope_digest, *, ticket, root_id, graph, executor, checkpointer
):
    """Bind the authenticated seal, scoped actions and host drain in this transaction."""
    from .m4_invalid_audit import _key, _mac
    from .v10_sealed_saver import verify_pinned_seal_inventory

    if not isinstance(scope_digest, str) or not _HEX.fullmatch(scope_digest):
        raise CorrectionReservationUnavailable("Action scope binding is unavailable")
    _require_drained_scheduler(
        worker, run.id, ticket=ticket, graph=graph, executor=executor, checkpointer=checkpointer
    )
    watermarks, head = verify_pinned_seal_inventory(db, ticket, root_id, worker.settings)
    if (
        watermarks.get("") != root_id
        or not isinstance(head, dict)
        or not isinstance(head.get("inventory"), dict)
    ):
        raise CorrectionReservationUnavailable("Pinned checkpoint binding is unavailable")
    return _mac(
        _key(worker.settings, "stage-agent-v10"),
        "v10-closed-zero-inventory-v1",
        {
            "run_id": run.id,
            "workspace_id": run.workspace_id,
            "epoch": ticket.epoch,
            "root_checkpoint_id": root_id,
            "seal_mac": head.get("mac"),
            "seal_inventory": head["inventory"],
            "scope_digest": scope_digest,
        },
    )


async def reserve_correction_model(
    worker, run_id, *, ticket, expected_checkpoint_id, graph, executor, checkpointer
):
    """Check owner presence and reserve on the same pinned owner transaction.

    The narrow proof accepts only a drained, host-assembled zero-tool Run.
    This is an isolated primitive and is not wired into Worker dispatch. A
    previously committed reservation is never dispatched again. A lost
    listener, cancelled caller or uncertain commit after reservation must be
    reconciled; no code here invokes a model.
    """
    lock = getattr(worker, "lock", None)
    if lock is None or lock.lost or lock.connection is None or lock.engine.dialect.name != "postgresql":
        raise CorrectionReservationUnavailable("Live PostgreSQL owner fence required")
    from .checkpoint_fencing import FenceTicket
    from .m4_services import validate_workspace_lease
    from .v10_closure import (
        inspect_pinned_zero_tool_inventory,
        pin_checkpoint_watermark,
        screen_zero_tool_correction_scope,
    )
    from .v10_owner_auth import verify_run_origin
    from .v10_owner_runtime import V10OwnerListener

    if not isinstance(ticket, FenceTicket) or ticket.thread_id != run_id:
        raise CorrectionReservationUnavailable("Checkpoint ticket scope differs")
    listener = getattr(worker, "_v10_owner_listener", None)
    qualified_listener = isinstance(listener, V10OwnerListener) and listener.worker is worker
    live = bool(qualified_listener and await listener._live())
    origin = listener.accepted.get(run_id) if live else None
    owner_connection = lock.connection
    listener_connection = listener.connection if qualified_listener else None
    receipt_values = None
    invocation = correction_invocation_id(run_id)
    op_id = operation_id(run_id, _NODE, {"invocation_id": invocation})
    try:
        # Recovery is read-only and never authorizes dispatch; it remains
        # available even after the scheduler/listener has disappeared.
        with worker.transaction() as db:
            if db.connection() is not lock.connection:
                raise CorrectionReservationUnavailable("Transaction is not on the owner connection")
            if (
                db.get(RunBudgetReservation, (run_id, invocation)) is not None
                or db.get(OperationRecord, op_id) is not None
            ):
                if qualified_listener:
                    listener.claim_failed(run_id)
                return {
                    "status": "PENDING_REVIEW",
                    "dispatch": False,
                    "invocation_id": invocation,
                    "operation_id": op_id,
                }
        drain = _require_drained_scheduler(
            worker, run_id, ticket=ticket, graph=graph, executor=executor, checkpointer=checkpointer
        )
        with worker.transaction() as db:
            if db.connection() is not lock.connection:
                raise CorrectionReservationUnavailable("Transaction is not on the owner connection")
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            pin_checkpoint_watermark(db, ticket, expected_checkpoint_id)
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.workflow_version != "stage-agent-v10" or not run.workspace_id:
                raise CorrectionReservationUnavailable("Invalid v10 Run scope")
            reservation = db.get(RunBudgetReservation, (run_id, invocation))
            row = db.get(OperationRecord, op_id)
            if reservation is not None or row is not None:
                consistent = bool(
                    reservation
                    and row
                    and reservation.kind == "model_calls"
                    and row.run_id == run_id
                    and row.node_id == _NODE
                    and row.workflow_version == run.workflow_version
                    and isinstance(row.result, dict)
                    and row.result.get("binding_fingerprint") == reservation.binding_fingerprint
                )
                confirmed = bool(
                    consistent and row.status == "completed" and isinstance(row.result.get("response"), dict)
                )
                result = {
                    "status": "EXISTING" if confirmed else "PENDING_REVIEW",
                    "dispatch": False,
                    "invocation_id": invocation,
                    "operation_id": op_id,
                }
            else:
                user = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "user"))
                if (
                    not live
                    or not isinstance(origin, dict)
                    or not listener.alive
                    or listener.accepted.get(run_id) != origin
                    or run_id in listener.consumed
                    or run_id in listener.tainted
                    or run.status != "running"
                    or user is None
                    or (run.config_snapshot or {}).get("v10_owner_origin") != origin
                    or not validate_workspace_lease(db, run.workspace_id, run.id)
                    or not verify_run_origin(
                        worker.settings, origin, owner=listener.identity, run=run, message=user
                    )
                ):
                    raise CorrectionReservationUnavailable("Owner presence or Run binding unavailable")
                # The sealed inventory and graph/ledger pre-screen run under
                # the same fence and owner transaction. They are not closure.
                passed, reason = await asyncio.wait_for(
                    inspect_pinned_zero_tool_inventory(
                        worker,
                        ticket,
                        graph,
                        executor,
                        checkpointer,
                        expected_checkpoint_id,
                        owner_db=db,
                        require_seal=True,
                    ),
                    timeout=_PINNED_READ_TIMEOUT_SECONDS,
                )
                if passed is not True:
                    raise CorrectionReservationUnavailable("Pinned execution inventory unavailable")
                scoped, scope_digest = await asyncio.wait_for(
                    screen_zero_tool_correction_scope(db, worker, ticket, graph, expected_checkpoint_id),
                    timeout=_PINNED_READ_TIMEOUT_SECONDS,
                )
                if scoped is not True:
                    raise CorrectionReservationUnavailable("Pinned action scope unavailable")
                validate_execute_host(db, run, drain)
                closure = _require_closed_inventory(
                    db,
                    run,
                    worker,
                    scope_digest,
                    ticket=ticket,
                    root_id=expected_checkpoint_id,
                    graph=graph,
                    executor=executor,
                    checkpointer=checkpointer,
                )
                if not isinstance(closure, str) or not _HEX.fullmatch(closure):
                    raise CorrectionReservationUnavailable("Invalid closure proof binding")
                # No await between this process-local recheck and DB commit:
                # listeners and cancellation cannot run concurrently here.
                if not listener.alive or listener.accepted.get(run_id) != origin or lock.lost:
                    raise CorrectionReservationUnavailable("Owner presence changed")
                actions = (run.outcome or {}).get("v10_actions", {}).get("actions", [])
                remaining = remaining_active_budget(db, run.id, drain, len(actions))
                if remaining <= 0:
                    raise _CorrectionBudgetExhausted
                binding = hashlib.sha256(
                    json.dumps(
                        {
                            "domain": "v10-correction-model-v1",
                            "run_id": run.id,
                            "workspace_id": run.workspace_id,
                            "closure": closure,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                reserved = reserve_invocation(
                    db,
                    run_id=run.id,
                    kind="model_calls",
                    invocation_id=invocation,
                    binding_fingerprint=binding,
                )
                if reserved == ReservationResult.EXHAUSTED:
                    result = {"status": "EXHAUSTED", "dispatch": False}
                elif reserved != ReservationResult.RESERVED:
                    raise CorrectionReservationUnavailable("Conflicting correction reservation")
                else:
                    ledger, created = begin_operation(
                        db, run.id, _NODE, {"invocation_id": invocation}, run.workflow_version
                    )
                    if not created:
                        raise CorrectionReservationUnavailable("Conflicting correction ledger")
                    ledger.result = {
                        "binding_fingerprint": binding,
                        "purpose": "v10_correction",
                        "dispatch_state": "RESERVED",
                        "closure_fingerprint": closure,
                        "intent_mac": correction_intent_mac(worker.settings, run, user),
                    }
                    result = {
                        "status": "RESERVED",
                        "dispatch": True,
                        "invocation_id": invocation,
                        "operation_id": op_id,
                    }
                    # Freeze identity before commit. This is not reusable
                    # authority: the process-local receipt is created only
                    # after the owner and listener are checked again below.
                    receipt_values = (
                        json.dumps(origin, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False),
                        binding,
                        closure,
                    )
    except _CorrectionBudgetExhausted:
        if qualified_listener:
            listener.claim_failed(run_id)
        return {"status": "EXHAUSTED", "dispatch": False}
    except BaseException:
        if qualified_listener:
            listener.claim_failed(run_id)
        raise
    if qualified_listener:
        listener.accepted.pop(run_id, None)
        listener.consumed.add(run_id)
    if result.get("status") == "RESERVED":
        try:
            if not qualified_listener or not await listener._live():
                if qualified_listener:
                    listener.claim_failed(run_id)
                return {**result, "status": "PENDING_REVIEW", "dispatch": False}
            # No await between this identity check and registration. A
            # disconnect/taint removes the receipt; neither the DB ledger nor
            # a later listener can recreate it.
            if (receipt_values is None or not listener.alive or lock.lost
                    or worker.lock is not lock or lock.connection is not owner_connection
                    or getattr(worker, "_v10_owner_listener", None) is not listener
                    or listener.connection is not listener_connection
                    or run_id in listener.tainted or run_id not in listener.consumed
                    or run_id in listener._correction_receipts):
                listener.claim_failed(run_id)
                return {**result, "status": "PENDING_REVIEW", "dispatch": False}
            from .v10_owner_runtime import _ConsumedCorrectionReceipt

            origin_json, binding, closure = receipt_values
            listener._correction_receipts[run_id] = _ConsumedCorrectionReceipt(
                run_id, origin_json, lock, owner_connection, listener_connection,
                ticket, expected_checkpoint_id, op_id, binding, closure,
            )
        except BaseException:
            if qualified_listener:
                listener.claim_failed(run_id)
            raise
    return result
