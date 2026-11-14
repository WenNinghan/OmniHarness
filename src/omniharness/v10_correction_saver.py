"""Isolated sealed correction checkpoint writer; not connected to tools or Worker.

The execution generation remains permanently closed. A live prepared phase
may claim one new correction generation; process or owner loss cannot rebuild
that grant from its database ledger. This class never dispatches a tool.
"""
from __future__ import annotations

import asyncio
import hmac

from psycopg import sql
from sqlalchemy import select, text

from .checkpoint_fencing import BoundAsyncPostgresSaver
from .correction_model import _locked_workspace, load_recorded_correction_batch
from .correction_phase import _mark_phase_review
from .correction_reservation import CorrectionReservationUnavailable, remaining_active_budget
from .m4_invalid_audit import _key, _mac
from .models import OperationRecord, Run
from .v10_closure import pin_checkpoint_watermark
from .v10_drain import get_drained_execution
from .v10_owner_runtime import V10OwnerListener, _PreparedCorrectionPhase
from .v10_sealed_saver import SealedSaverUnavailable, V10SealedSaver, verify_pinned_seal_inventory


class V10CorrectionSaver(V10SealedSaver):
    """A single live-owner generation restricted to the correction namespace."""

    def __init__(self, base, *, worker, ticket, root_id, graph, executor, execution_saver):
        listener, owner = getattr(worker, "_v10_owner_listener", None), getattr(worker, "lock", None)
        grant = (listener._correction_phases.get(ticket.thread_id)
                 if isinstance(listener, V10OwnerListener) else None)
        if (type(base) is not BoundAsyncPostgresSaver or base.ticket != ticket
                or not isinstance(grant, _PreparedCorrectionPhase) or grant.run_id != ticket.thread_id
                or grant.ticket != ticket or grant.root_id != root_id or grant.owner is not owner
                or owner is None or owner.lost or owner.connection is not grant.owner_connection
                or grant.listener_connection is not listener.connection or not listener.alive
                or grant.execution_saver is not execution_saver or base.conn is execution_saver.conn
                or ticket.thread_id in listener.tainted):
            raise SealedSaverUnavailable("Prepared correction generation unavailable")
        self._worker, self._listener, self._grant = worker, listener, grant
        self._settings, self._execution_saver = worker.settings, execution_saver
        self._namespace = "v10corr-" + grant.phase_id
        phase_id = grant.phase_id
        try:
            with worker.transaction() as db:
                if db.connection() is not owner.connection:
                    raise CorrectionReservationUnavailable("Correction owner changed")
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                pin_checkpoint_watermark(db, ticket, root_id)
                run = db.scalar(select(Run).where(Run.id == ticket.thread_id).with_for_update())
                ledger = db.scalar(select(OperationRecord).where(
                    OperationRecord.operation_id == phase_id).with_for_update())
                data = ledger.result if ledger is not None and isinstance(ledger.result, dict) else {}
                phase, signature = data.get("phase"), data.get("mac")
                if (not run or run.status != "running" or not ledger or ledger.status != "running"
                        or ledger.run_id != run.id or ledger.node_id != "v10-correction:phase"
                        or not isinstance(phase, dict) or not isinstance(signature, str)
                        or not hmac.compare_digest(signature, _mac(_key(worker.settings, "stage-agent-v10"),
                                                                    "v10-correction-phase", phase))
                        or phase.get("state") != "PREPARED" or phase.get("run_id") != run.id
                        or phase.get("phase_id") != phase_id or phase.get("namespace") != self._namespace
                        or phase.get("root_id") != root_id or phase.get("epoch") != ticket.epoch
                        or phase.get("owner_pid") != ticket.owner_pid
                        or phase.get("owner_started") != ticket.owner_started.isoformat()):
                    raise CorrectionReservationUnavailable("Prepared phase ledger differs")
                _locked_workspace(db, run, worker.settings)
                proof = get_drained_execution(worker, run.id, ticket=ticket, graph=graph,
                                              executor=executor, saver=execution_saver)
                batch = load_recorded_correction_batch(db, run, worker.settings)
                _, head = verify_pinned_seal_inventory(db, ticket, root_id, worker.settings)
                if (batch != phase.get("batch") or head != phase.get("original_seal_head")
                        or remaining_active_budget(db, run.id, proof, len(batch), model_already_reserved=True) <= 0):
                    raise CorrectionReservationUnavailable("Prepared phase evidence changed")
                listener_start = db.execute(text("SELECT backend_start FROM pg_catalog.pg_stat_activity "
                                                 "WHERE pid=:pid"), {"pid": listener.listener_pid}).scalar_one_or_none()
                if listener_start is None or listener.connection.closed or listener.connection.broken:
                    raise CorrectionReservationUnavailable("Correction listener unavailable")
                self._listener_started = listener_start
                self._segment_started = proof.segment_started
                updated = {**phase, "state": "ACTIVE"}
                ledger.result = {"phase": updated, "mac": _mac(_key(worker.settings, "stage-agent-v10"),
                                                                "v10-correction-phase", updated)}
            super().__init__(base.conn, ticket, business_schema=execution_saver.business_schema, settings=worker.settings)
        except BaseException:
            listener.claim_failed(ticket.thread_id)
            _mark_phase_review(worker, ticket.thread_id, phase_id)
            raise

    def _register_phase_writer(self):
        from .v10_saver_guard import register_correction_writer
        register_correction_writer(self.ticket, self._phase_token, self, self._execution_saver)

    def _assert_phase_writer(self, namespace):
        from .v10_saver_guard import assert_correction_writer
        listener, owner, grant = self._listener, self._worker.lock, self._grant
        if (not isinstance(namespace, str)
                or not (namespace == self._namespace or namespace.startswith(self._namespace + "|"))
                or getattr(self._worker, "_v10_owner_listener", None) is not listener or not listener.alive
                or owner is not grant.owner or owner.lost or owner.connection is not grant.owner_connection
                or listener.connection is not grant.listener_connection
                or listener.connection.closed or listener.connection.broken
                or listener._correction_phases.get(grant.run_id) is not grant or grant.run_id in listener.tainted):
            raise SealedSaverUnavailable("Correction generation identity or namespace differs")
        assert_correction_writer(self.ticket, self._phase_token, self, self._execution_saver)

    def _bound(self, config):
        super()._bound(config)
        self._assert_phase_writer(config.get("configurable", {}).get("checkpoint_ns"))

    async def close_writes(self):
        from .v10_saver_guard import close_correction_writer
        try:
            async with self.lock:
                close_correction_writer(self.ticket, self._phase_token)
                self._writes_closed = True
            await asyncio.wait_for(self._write_idle.wait(), timeout=10)
        finally:
            # Even cancellation before acquiring the saver lock must revoke
            # the process-local grant; every pending write checks it again.
            self._listener.claim_failed(self.ticket.thread_id)
            if self._active_writes == 0 and not self.lock.locked():
                _mark_phase_review(self._worker, self.ticket.thread_id, self._grant.phase_id)
            else:
                # Never synchronously wait on the Run row while a saver write
                # on this same event loop may hold it. A dead owner/loop leaves
                # ACTIVE durable and requires recovery review, never replay.
                async def mark_after_drain():
                    try:
                        await asyncio.wait_for(self._write_idle.wait(), timeout=10)
                        _mark_phase_review(self._worker, self.ticket.thread_id, self._grant.phase_id)
                    except (Exception, asyncio.CancelledError):
                        pass
                asyncio.create_task(mark_after_drain())

    async def _check_phase_transaction(self, cur, run, namespace):
        self._assert_phase_writer(namespace)
        if run.get("status") != "running":
            raise SealedSaverUnavailable("Correction Run is no longer running")
        schema = sql.Identifier(self.business_schema)
        await cur.execute(sql.SQL("SELECT 1 FROM {}.run_commands WHERE run_id=%s AND status='pending' LIMIT 1")
                          .format(schema), (run["id"],))
        if await cur.fetchone() is not None:
            raise SealedSaverUnavailable("Correction Run has a pending control command")
        await cur.execute(sql.SQL("SELECT node_id,run_id,status,workflow_version,result FROM {}.operation_records "
                                  "WHERE operation_id=%s FOR UPDATE").format(schema), (self._grant.phase_id,))
        record = await cur.fetchone()
        data = record["result"] if record and isinstance(record["result"], dict) else {}
        phase, signature = data.get("phase"), data.get("mac")
        if (not record or record["node_id"] != "v10-correction:phase" or record["run_id"] != run["id"]
                or record["status"] != "running" or record["workflow_version"] != "stage-agent-v10"
                or not isinstance(phase, dict) or not isinstance(signature, str)
                or not hmac.compare_digest(signature, _mac(_key(self._settings, "stage-agent-v10"),
                                                            "v10-correction-phase", phase))
                or phase.get("state") != "ACTIVE" or phase.get("phase_id") != self._grant.phase_id
                or phase.get("run_id") != run["id"] or phase.get("workspace_id") != run["workspace_id"]
                or phase.get("namespace") != self._namespace or phase.get("root_id") != self._grant.root_id
                or phase.get("epoch") != self.ticket.epoch or phase.get("owner_pid") != self.ticket.owner_pid
                or phase.get("owner_started") != self.ticket.owner_started.isoformat()):
            raise SealedSaverUnavailable("Correction phase ledger differs")
        await cur.execute(sql.SQL("SELECT run_id,node_id,status,workflow_version,result FROM {}.operation_records "
                                  "WHERE operation_id=%s FOR UPDATE").format(schema),
                          (phase.get("model_operation_id"),))
        model = await cur.fetchone()
        model_data = model["result"] if model and isinstance(model["result"], dict) else {}
        response = (model_data.get("response") or {}).get("message")
        response_mac, request_digest = model_data.get("response_mac"), model_data.get("request_sha256")
        if (not model or model["run_id"] != run["id"] or model["node_id"] != "v10-correction:model_calls"
                or model["status"] != "completed" or model["workflow_version"] != "stage-agent-v10"
                or model_data.get("dispatch_state") != "RECORDED" or not isinstance(response, dict)
                or not isinstance(response_mac, str) or not isinstance(request_digest, str)
                or not hmac.compare_digest(response_mac, phase.get("response_mac", ""))
                or not hmac.compare_digest(response_mac, _mac(_key(self._settings, "stage-agent-v10"),
                    "v10-correction-response", {"run_id": run["id"], "operation_id": phase["model_operation_id"],
                    "request_sha256": request_digest, "response": response}))):
            raise SealedSaverUnavailable("Correction native response differs")
        await cur.execute(sql.SQL("SELECT id,content FROM {}.messages WHERE run_id=%s AND role='user'").format(schema),
                          (run["id"],))
        users = await cur.fetchall()
        if len(users) != 1:
            raise SealedSaverUnavailable("Correction user request unavailable")
        snapshot, user = run["config_snapshot"], users[0]
        intent = _mac(_key(self._settings, "stage-agent-v10"), "v10-correction-intent", {
            "run_id": run["id"], "workspace_id": run["workspace_id"], "user_id": user["id"],
            "user_content": user["content"], "action_contract": snapshot.get("action_contract"),
            "actions": (run["outcome"] or {}).get("v10_actions"), "workspace": snapshot.get("workspace"),
            "agent": snapshot.get("agent"),
        })
        if not isinstance(model_data.get("intent_mac"), str) or not hmac.compare_digest(intent, model_data["intent_mac"]):
            raise SealedSaverUnavailable("Correction intent differs")
        workspace = snapshot.get("workspace") or {}
        await cur.execute(sql.SQL("SELECT enabled,policy_version,host_path,probe FROM {}.workspaces "
                                  "WHERE id=%s FOR UPDATE").format(schema), (run["workspace_id"],))
        row = await cur.fetchone()
        await cur.execute(sql.SQL("SELECT run_id FROM {}.workspace_leases WHERE workspace_id=%s FOR UPDATE")
                          .format(schema), (run["workspace_id"],))
        lease = await cur.fetchone()
        await cur.execute(sql.SQL("SELECT \"grant\",tool_policy FROM {}.workspace_policies WHERE workspace_id=%s "
                                  "AND version=%s FOR UPDATE").format(schema),
                          (run["workspace_id"], workspace.get("policy_version")))
        policy = await cur.fetchone()
        if (not row or not row["enabled"] or not lease or lease["run_id"] != run["id"] or not policy
                or row["policy_version"] != workspace.get("policy_version")
                or row["host_path"] != (workspace.get("grant") or {}).get("root")
                or (row["probe"] or {}).get("identity") != workspace.get("probe_identity")
                or policy["grant"] != workspace.get("grant") or policy["tool_policy"] != workspace.get("tool_policy")):
            raise SealedSaverUnavailable("Correction workspace or lease changed")
        head = phase.get("original_seal_head")
        if not isinstance(head, dict) or not isinstance(head.get("mac"), str):
            raise SealedSaverUnavailable("Original execution seal unavailable")
        await cur.execute(sql.SQL("SELECT result FROM {}.operation_records WHERE run_id=%s AND node_id=%s "
                                  "AND result->>'mac'=%s").format(schema),
                          (run["id"], "v10-checkpoint-seal-v1", head["mac"]))
        original = await cur.fetchall()
        if len(original) != 1:
            raise SealedSaverUnavailable("Original execution seal is missing")
        seal = original[0]["result"].get("seal", {})
        if (seal.get("sequence") != head.get("sequence") or seal.get("inventory") != head.get("inventory")
                or seal.get("epoch") != head.get("epoch")):
            raise SealedSaverUnavailable("Original execution seal changed")
        await cur.execute("SELECT 1 FROM pg_catalog.pg_stat_activity WHERE pid=%s AND backend_start=%s",
                          (self._listener.listener_pid, self._listener_started))
        if await cur.fetchone() is None:
            raise SealedSaverUnavailable("Correction listener backend unavailable")
        self._assert_phase_writer(namespace)
