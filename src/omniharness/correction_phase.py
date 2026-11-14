"""Identity checks for a prospective correction phase; never dispatches tools.

The in-process receipt cannot be reconstructed from a database record. This
module does not create a phase, authorize a saver, or establish closure. Its
caller must also recheck the complete pinned inventory, action scope and
workspace permissions in the same owner transaction.
"""

from __future__ import annotations

import asyncio
import hmac
import json

from sqlalchemy import select, text

from .correction_reservation import CorrectionReservationUnavailable, correction_invocation_id
from .models import Message, OperationRecord, Run, RunBudgetReservation, RunCommand, ToolOperation, now
from .services import add_event, begin_operation, operation_id, parameters_digest
from .v10_closure import pin_checkpoint_watermark
from .v10_owner_auth import verify_run_origin
from .v10_owner_runtime import V10OwnerListener, _ConsumedCorrectionReceipt


def require_consumed_phase_identity(db, worker, *, ticket, root_id):
    """Recheck the one-shot receipt under the caller's pinned owner transaction.

    This proves identity only, not closure or permission to perform a write.
    The caller must check listener liveness before and after the transaction.
    """
    listener = getattr(worker, "_v10_owner_listener", None)
    owner = getattr(worker, "lock", None)
    run_id = getattr(ticket, "thread_id", None)
    if (not isinstance(listener, V10OwnerListener) or listener.worker is not worker
            or not listener.alive or owner is None or owner.lost or owner.connection is None
            or db.connection() is not owner.connection or not isinstance(run_id, str)):
        raise CorrectionReservationUnavailable("Consumed owner identity unavailable")
    receipt = listener._correction_receipts.get(run_id)
    if (not isinstance(receipt, _ConsumedCorrectionReceipt) or receipt.run_id != run_id
            or receipt.owner is not owner or receipt.owner_connection is not owner.connection
            or receipt.listener_connection is not listener.connection or receipt.ticket != ticket
            or receipt.root_id != root_id or run_id not in listener.consumed
            or run_id in listener.tainted or run_id in listener.accepted):
        raise CorrectionReservationUnavailable("Consumed correction receipt unavailable")
    pin_checkpoint_watermark(db, ticket, root_id)
    run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    user = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
    invocation = correction_invocation_id(run_id)
    op_id = operation_id(run_id, "v10-correction:model_calls", {"invocation_id": invocation})
    op = db.scalar(select(OperationRecord).where(OperationRecord.operation_id == op_id).with_for_update())
    reservation = db.get(RunBudgetReservation, (run_id, invocation))
    data = op.result if op is not None and isinstance(op.result, dict) else {}
    try:
        origin = json.loads(receipt.origin_json)
    except (TypeError, ValueError):
        raise CorrectionReservationUnavailable("Consumed origin unavailable") from None
    if (not run or run.status != "running" or run.workflow_version != "stage-agent-v10" or not user
            or not isinstance(origin, dict) or (run.config_snapshot or {}).get("v10_owner_origin") != origin
            or not verify_run_origin(worker.settings, origin, owner=listener.identity, run=run, message=user)
            or ticket.owner_pid != listener.identity.get("pid")
            or ticket.owner_started.isoformat() != listener.identity.get("backend_started")
            or receipt.operation_id != op_id or op is None or op.run_id != run_id
            or op.node_id != "v10-correction:model_calls" or op.workflow_version != run.workflow_version
            or op.parameters_hash != parameters_digest({"invocation_id": invocation})
            or op.status != "completed" or data.get("dispatch_state") != "RECORDED"
            or reservation is None or reservation.run_id != run_id or reservation.kind != "model_calls"
            or not isinstance(receipt.binding_fingerprint, str)
            or not isinstance(receipt.closure_fingerprint, str)
            or not isinstance(reservation.binding_fingerprint, str)
            or not isinstance(data.get("binding_fingerprint"), str)
            or not isinstance(data.get("closure_fingerprint"), str)
            or not hmac.compare_digest(receipt.binding_fingerprint, reservation.binding_fingerprint)
            or not hmac.compare_digest(receipt.binding_fingerprint, data["binding_fingerprint"])
            or not hmac.compare_digest(receipt.closure_fingerprint, data["closure_fingerprint"])):
        raise CorrectionReservationUnavailable("Consumed correction binding changed")
    return run, receipt


def _set_phase_review(db, run, phase):
    if phase.status == "running":
        phase.status, phase.error_code, phase.finished_at = "uncertain", "PHASE_UNKNOWN", now()
    # Cleanup can race the Worker's control handler or arrive after its final
    # event. Preserve control and terminal states; the uncertain phase ledger
    # still records that this correction must not be replayed.
    if run.status in {"cancel_requested", "cancelled", "completed", "failed", "interrupted"}:
        return
    from .m4_services import expire_approvals
    expire_approvals(db, run.id)
    if run.status in {"pause_requested", "paused"}:
        return
    if run.status != "pending_review":
        run.status, run.error_code = "pending_review", "CORRECTION_PHASE_UNKNOWN"
        run.error_message = "纠错阶段状态需要核对；不会自动重启。"
        assistant = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "assistant"))
        if assistant is not None:
            assistant.status = "pending_review"
        add_event(db, run, "run.pending_review", {"code": run.error_code, "message": run.error_message})


def _mark_phase_review(worker, run_id, phase_id):
    """Best effort after an uncertain commit; an existing phase is never replayed."""
    try:
        owner = getattr(worker, "lock", None)
        if owner is None or owner.lost or owner.connection is None:
            return
        with worker.transaction() as db:
            if db.connection() is not owner.connection:
                return
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            phase = db.get(OperationRecord, phase_id)
            if not run or not phase or phase.run_id != run_id or phase.status not in {"running", "uncertain"}:
                return
            _set_phase_review(db, run, phase)
    except Exception:
        pass


async def prepare_correction_phase(worker, run_id, *, ticket, root_id, graph, executor, checkpointer):
    """Persist one authenticated phase candidate; never creates a writer or calls tools.

    Complete closure, response and workspace checks run under the same pinned
    owner transaction as the unique phase record. A committed record, lost
    listener or uncertain commit is never re-entered by this function.
    """
    from .correction_model import _locked_workspace, load_recorded_correction_batch
    from .correction_reservation import remaining_active_budget, validate_execute_host
    from .m4_invalid_audit import _key, _mac
    from .v10_closure import inspect_consumed_correction_phase
    from .v10_drain import get_drained_execution
    from .v10_sealed_saver import verify_pinned_seal_inventory

    phase_node = "v10-correction:phase"
    phase_parameters = {"version": 1}
    phase_id = operation_id(run_id, phase_node, phase_parameters)
    listener = getattr(worker, "_v10_owner_listener", None)
    owner = getattr(worker, "lock", None)
    if (not isinstance(listener, V10OwnerListener) or listener.worker is not worker
            or owner is None or owner.lost or owner.connection is None
            or getattr(ticket, "thread_id", None) != run_id):
        raise CorrectionReservationUnavailable("Phase owner unavailable")
    connection = owner.connection
    listener_connection = listener.connection
    try:
        if not await listener._live():
            raise CorrectionReservationUnavailable("Phase listener unavailable")
        with worker.transaction() as db:
            if db.connection() is not connection:
                raise CorrectionReservationUnavailable("Phase owner connection changed")
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            pin_checkpoint_watermark(db, ticket, root_id)
            current_run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            existing = db.scalar(select(OperationRecord).where(
                OperationRecord.operation_id == phase_id).with_for_update())
            if existing is not None:
                if current_run is None or existing.run_id != run_id:
                    raise CorrectionReservationUnavailable("Existing correction phase scope changed")
                _set_phase_review(db, current_run, existing)
                listener.claim_failed(run_id)
                return {"status": "PENDING_REVIEW", "tool_dispatch": False, "operation_id": phase_id}
            passed, scope_digest = await asyncio.wait_for(
                inspect_consumed_correction_phase(
                    db, worker, ticket=ticket, root_id=root_id, graph=graph,
                    executor=executor, checkpointer=checkpointer,
                ), timeout=5,
            )
            if not passed:
                raise CorrectionReservationUnavailable("Consumed phase closure unavailable")
            run, receipt = require_consumed_phase_identity(db, worker, ticket=ticket, root_id=root_id)
            if (db.scalar(select(RunCommand.id).where(RunCommand.run_id == run_id).limit(1))
                    or db.scalar(select(ToolOperation.id).where(ToolOperation.run_id == run_id).limit(1))):
                raise CorrectionReservationUnavailable("Correction phase Run changed")
            _locked_workspace(db, run, worker.settings)
            proof = get_drained_execution(worker, run_id, ticket=ticket, graph=graph,
                                          executor=executor, saver=checkpointer)
            validate_execute_host(db, run, proof)
            batch = load_recorded_correction_batch(db, run, worker.settings)
            if remaining_active_budget(db, run_id, proof, len(batch), model_already_reserved=True) <= 0:
                raise CorrectionReservationUnavailable("Correction phase budget exhausted")
            root_state = await asyncio.wait_for(graph.aget_state({"configurable": {"thread_id": run_id}}), timeout=2)
            tasks = ((root_state.values or {}).get("dispatch") or {}).get("tasks")
            if (root_state.config.get("configurable", {}).get("checkpoint_id") != root_id
                    or not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict)
                    or not isinstance(tasks[0].get("id"), str)
                    or any(item["action"].get("task_id") != tasks[0]["id"] for item in batch)):
                raise CorrectionReservationUnavailable("Correction task scope unavailable")
            task_scope = {"id": tasks[0]["id"], "write_paths": tasks[0].get("write_paths")}
            _, head = verify_pinned_seal_inventory(db, ticket, root_id, worker.settings)
            model = db.get(OperationRecord, receipt.operation_id)
            response_mac = (model.result or {}).get("response_mac")
            if (not isinstance(response_mac, str) or not listener.alive or owner.lost
                    or worker.lock is not owner or owner.connection is not connection
                    or getattr(worker, "_v10_owner_listener", None) is not listener
                    or listener.connection is not listener_connection
                    or listener._correction_receipts.get(run_id) is not receipt
                    or run_id in listener.tainted):
                raise CorrectionReservationUnavailable("Correction phase identity changed")
            ledger, created = begin_operation(db, run_id, phase_node, phase_parameters, run.workflow_version)
            if not created:
                raise CorrectionReservationUnavailable("Correction phase already exists")
            phase = {
                "version": 1, "state": "PREPARED", "run_id": run_id, "workspace_id": run.workspace_id,
                "phase_id": phase_id, "namespace": "v10corr-" + phase_id,
                "epoch": ticket.epoch, "owner_pid": ticket.owner_pid,
                "owner_started": ticket.owner_started.isoformat(), "root_id": root_id,
                "original_seal_head": head, "scope_digest": scope_digest,
                "closure_fingerprint": receipt.closure_fingerprint,
                "model_operation_id": receipt.operation_id, "response_mac": response_mac, "batch": batch,
                "task_scope": task_scope,
                "items": [{"action_id": item["action"]["id"], "tool_call_id": item["tool_call"]["id"],
                           "state": "PLANNED"} for item in batch],
            }
            ledger.result = {"phase": phase, "mac": _mac(_key(worker.settings, "stage-agent-v10"),
                                                            "v10-correction-phase", phase)}
            if remaining_active_budget(db, run_id, proof, len(batch), model_already_reserved=True) <= 0:
                raise CorrectionReservationUnavailable("Correction phase budget exhausted before commit")
            db.flush()
        # Consume before any await. A committed phase cannot be re-created;
        # this candidate has no writer and therefore cannot dispatch tools.
        listener._correction_receipts.pop(run_id, None)
        if (not await listener._live() or worker.lock is not owner or owner.lost
                or owner.connection is not connection or listener.connection is not listener_connection):
            listener.claim_failed(run_id)
            _mark_phase_review(worker, run_id, phase_id)
            return {"status": "PENDING_REVIEW", "tool_dispatch": False, "operation_id": phase_id}
        if (not listener.alive or getattr(worker, "_v10_owner_listener", None) is not listener
                or run_id in listener.tainted or run_id in listener._correction_phases):
            listener.claim_failed(run_id)
            _mark_phase_review(worker, run_id, phase_id)
            return {"status": "PENDING_REVIEW", "tool_dispatch": False, "operation_id": phase_id}
        # Recheck the live authorization after the asynchronous listener probe.
        # A later correction write must still lock and recheck it again.
        with worker.transaction() as db:
            if db.connection() is not connection:
                raise CorrectionReservationUnavailable("Phase owner connection changed")
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            pin_checkpoint_watermark(db, ticket, root_id)
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            ledger = db.scalar(select(OperationRecord).where(
                OperationRecord.operation_id == phase_id).with_for_update())
            if (run is None or run.status != "running" or ledger is None or ledger.status != "running"
                    or ledger.run_id != run_id or (ledger.result or {}).get("phase", {}).get("state") != "PREPARED"):
                raise CorrectionReservationUnavailable("Committed correction phase changed")
            _locked_workspace(db, run, worker.settings)
            proof = get_drained_execution(worker, run_id, ticket=ticket, graph=graph,
                                           executor=executor, saver=checkpointer)
            if remaining_active_budget(db, run_id, proof, len(batch), model_already_reserved=True) <= 0:
                raise CorrectionReservationUnavailable("Correction phase budget exhausted after commit")
            if (not listener.alive or worker.lock is not owner or owner.lost
                    or owner.connection is not connection or listener.connection is not listener_connection
                    or getattr(worker, "_v10_owner_listener", None) is not listener
                    or run_id in listener.tainted or run_id in listener._correction_phases):
                raise CorrectionReservationUnavailable("Committed phase owner changed")
        from .v10_owner_runtime import _PreparedCorrectionPhase

        listener._correction_phases[run_id] = _PreparedCorrectionPhase(
            run_id, phase_id, ticket, root_id, owner, connection, listener_connection, checkpointer,
        )
        return {"status": "PREPARED", "tool_dispatch": False, "operation_id": phase_id}
    except BaseException:
        listener.claim_failed(run_id)
        _mark_phase_review(worker, run_id, phase_id)
        raise
