"""Host-owned correction batch binding for the existing tool gateway.

This component is not attached to Worker. A caller must use the sealed
correction saver and the real ExecutionBoundary; it never executes a tool.
"""
from __future__ import annotations

import asyncio
import copy
import hmac
import json
import math
import time

from sqlalchemy import select

from .artifacts import ArtifactStore
from .correction_model import _locked_workspace, load_recorded_correction_batch
from .correction_reservation import CorrectionReservationUnavailable
from .m3_agents import invocation_identity
from .m4_invalid_audit import _key, _mac
from .models import OperationRecord, RunBudget, RunCommand, ToolOperation, WorkspaceSnapshot, now
from .review_evidence import build_review_evidence_v9, v9_snapshot_mac
from .v10_correction_saver import V10CorrectionSaver


class CorrectionGatewayBinding:
    """Fixed same-Run phase and per-item ledger checks for a real gateway."""

    def __init__(self, host, saver):
        if (type(saver) is not V10CorrectionSaver or host.worker is not saver._worker
                or host.run_id != saver.ticket.thread_id or host.workflow_version != "stage-agent-v10"
                or getattr(host, "_v10_correction_binding", None) is not None):
            raise CorrectionReservationUnavailable("Correction gateway identity unavailable")
        self.host, self.worker, self.saver = host, host.worker, saver
        host._v10_correction_binding = self

    def _phase(self, db, *, require_budget):
        if db.connection() is not self.saver._grant.owner_connection:
            raise CorrectionReservationUnavailable("Correction gateway owner changed")
        self.saver._assert_phase_writer(self.saver._namespace)
        run, _ = self.host._run(db, "execute")
        _locked_workspace(db, run, self.worker.settings)
        if db.scalar(select(RunCommand.id).where(
                RunCommand.run_id == run.id, RunCommand.status == "pending").limit(1)):
            raise CorrectionReservationUnavailable("Correction control command pending")
        ledger = db.scalar(select(OperationRecord).where(
            OperationRecord.operation_id == self.saver._grant.phase_id).with_for_update())
        data = ledger.result if ledger is not None and isinstance(ledger.result, dict) else {}
        phase, signature = data.get("phase"), data.get("mac")
        if (ledger is None or ledger.status != "running" or ledger.run_id != run.id
                or ledger.node_id != "v10-correction:phase" or not isinstance(phase, dict)
                or phase.get("state") != "ACTIVE" or phase.get("phase_id") != ledger.operation_id
                or phase.get("run_id") != run.id or phase.get("workspace_id") != run.workspace_id
                or phase.get("namespace") != self.saver._namespace or not isinstance(signature, str)
                or not hmac.compare_digest(signature, _mac(_key(self.worker.settings, "stage-agent-v10"),
                                                            "v10-correction-phase", phase))
                or not isinstance(phase.get("items"), list) or not isinstance(phase.get("batch"), list)
                or len(phase["items"]) != len(phase["batch"])):
            raise CorrectionReservationUnavailable("Correction phase ledger changed")
        if phase["batch"] != load_recorded_correction_batch(db, run, self.worker.settings):
            raise CorrectionReservationUnavailable("Correction response changed")
        if require_budget:
            budget = db.scalar(select(RunBudget).where(RunBudget.run_id == run.id).with_for_update())
            started = self.saver._segment_started
            current = time.monotonic()
            if (budget is None or budget.active_segment_open is not True or type(started) not in (int, float)
                    or not math.isfinite(started) or started > current or budget.active_seconds < 0
                    or budget.tool_calls < 0 or budget.tool_calls > budget.max_tool_calls
                    or budget.max_active_seconds - budget.active_seconds - math.ceil(current - started) <= 0):
                raise CorrectionReservationUnavailable("Correction active budget exhausted")
        return run, ledger, copy.deepcopy(phase)

    def _save(self, ledger, phase):
        ledger.result = {"phase": phase, "mac": _mac(_key(self.worker.settings, "stage-agent-v10"),
                                                      "v10-correction-phase", phase)}

    @staticmethod
    def _item(phase, action_id):
        matches = [(item, entry) for item, entry in zip(phase["items"], phase["batch"])
                   if item.get("action_id") == action_id and entry.get("action", {}).get("id") == action_id]
        if len(matches) != 1 or matches[0][0].get("tool_call_id") != matches[0][1].get("tool_call", {}).get("id"):
            raise CorrectionReservationUnavailable("Correction item identity changed")
        return matches[0]

    def _recorded(self, db, run, phase, action_id):
        verified, ready = self._verified_result(db, run, phase, action_id)
        return verified and ready

    def _verified_result(self, db, run, phase, action_id):
        item, entry = self._item(phase, action_id)
        if item.get("state") != "RECORDED" or not isinstance(item.get("tool_operation_id"), str):
            return False, False
        op = db.get(ToolOperation, item["tool_operation_id"])
        if not self._operation_matches(run, entry, op):
            return False, False
        packet = build_review_evidence_v9(db, run, ArtifactStore(self.worker.settings.data_dir / "artifacts"),
                                          settings=self.worker.settings, operation_ids=[op.id])
        found = [value for value in packet.get("operations", []) if value.get("operation_id") == op.id]
        if len(found) != 1 or found[0].get("dimensions", {}).get("execution") != "RECORDED":
            return False, False
        if op.tool_name == "workspace_patch":
            verified = found[0].get("payload_digest_matches") is True
            return verified, verified
        verified = (op.tool_name == "run_command"
                    and found[0].get("dimensions", {}).get("exit_code") == "RECORDED")
        return verified, verified and found[0].get("exit_code") == 0

    @staticmethod
    def _operation_matches(run, entry, op):
        from .m4_tools import CommandArguments, PatchArgumentsV8

        call = entry["tool_call"]
        try:
            schema = PatchArgumentsV8 if call["name"] == "workspace_patch" else CommandArguments
            parameters = schema.model_validate(call["args"], strict=True).model_dump(mode="json")
        except Exception:
            return False
        return bool(op is not None and op.run_id == run.id and op.tool_call_id == call["id"]
                    and op.tool_name == call["name"] and op.status == "completed"
                    and op.parameters == parameters
                    and (op.result or {}).get("host_task_id") == entry["action"].get("task_id"))

    def _dependencies(self, db, run, phase, entry):
        return all(self._recorded(db, run, phase, identity) for identity in entry["dependencies"])

    def _snapshot_matches_dependencies(self, db, run, phase, entry, operation):
        if operation.tool_name != "run_command":
            return True
        patches = []
        for identity in entry["dependencies"]:
            item, predecessor = self._item(phase, identity)
            if predecessor["tool_call"]["name"] == "workspace_patch":
                if not self._recorded(db, run, phase, identity):
                    return False
                prior = db.get(ToolOperation, item["tool_operation_id"])
                patches.append((prior.parameters.get("path"), (prior.result or {}).get("sha256")))
        if not patches:
            return True
        snapshot = db.get(WorkspaceSnapshot, operation.snapshot_id) if operation.snapshot_id else None
        saved = operation.result if isinstance(operation.result, dict) else {}
        manifest = snapshot.manifest if snapshot and isinstance(snapshot.manifest, dict) else {}
        mac = saved.get("v9_snapshot_mac")
        if (snapshot is None or snapshot.run_id != run.id or snapshot.workspace_id != run.workspace_id
                or snapshot.policy_version != (run.config_snapshot.get("workspace") or {}).get("policy_version")
                or manifest.get("snapshot_id") != snapshot.id or manifest.get("run_id") != run.id
                or manifest.get("workspace_id") != run.workspace_id or not isinstance(mac, str)
                or not hmac.compare_digest(mac, v9_snapshot_mac(self.worker.settings, run, operation, snapshot))
                or not isinstance(manifest.get("files"), list)):
            return False
        files = {}
        for item in manifest["files"]:
            if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                    or item["path"] in files or not isinstance(item.get("sha256"), str)):
                return False
            files[item["path"]] = item["sha256"]
        return all(isinstance(path, str) and isinstance(digest, str) and files.get(path) == digest
                   for path, digest in patches)

    async def load(self):
        async with self.host.serial:
            with self.worker.transaction() as db:
                run, _, phase = self._phase(db, require_budget=True)
                if any(item.get("state") != "PLANNED" for item in phase["items"]):
                    raise CorrectionReservationUnavailable("Correction phase is not new")
                scope = phase.get("task_scope")
                if not isinstance(scope, dict) or set(scope) != {"id", "write_paths"}:
                    raise CorrectionReservationUnavailable("Correction task scope unavailable")
                batch = phase["batch"]
                return copy.deepcopy(scope), {
                    "missing_actions": [item["action"] for item in batch],
                    "tool_calls": [item["tool_call"] for item in batch],
                    "dependency_graph": {item["action"]["id"]: item["dependencies"] for item in batch},
                    "completed_action_ids": [],
                }

    async def verify_dependencies(self, action, dependencies):
        async with self.host.serial:
            with self.worker.transaction() as db:
                run, _, phase = self._phase(db, require_budget=True)
                item, entry = self._item(phase, action.get("id"))
                return bool(entry["action"] == action and entry["dependencies"] == dependencies
                            and item.get("state") in {"PLANNED", "BOUND"}
                            and self._dependencies(db, run, phase, entry))

    async def bind_request(self, request, candidate):
        call = request.tool_call
        identity = invocation_identity(request, kind="tool_calls", role="execute", tool_name=call.get("name"))
        async with self.host.serial:
            with self.worker.transaction() as db:
                run, ledger, phase = self._phase(db, require_budget=True)
                item, entry = self._item(phase, candidate.get("action", {}).get("id"))
                if (entry != candidate or call != entry["tool_call"] or identity.thread_id != run.id
                        or not self._dependencies(db, run, phase, entry)):
                    raise CorrectionReservationUnavailable("Correction invocation binding changed")
                binding = {"invocation_id": identity.invocation_id, "task_id": identity.task_id,
                           "checkpoint_ns": identity.checkpoint_ns}
                if item.get("state") == "PLANNED":
                    item.update({"state": "BOUND", **binding})
                    self._save(ledger, phase)
                elif item.get("state") != "BOUND" or any(item.get(k) != v for k, v in binding.items()):
                    raise CorrectionReservationUnavailable("Correction invocation cannot be replayed")

    def mark_dispatched(self, db, request, identity, operation):
        """Called within the existing atomic gateway budget/operation claim."""
        run, ledger, phase = self._phase(db, require_budget=True)
        call = request.tool_call
        matches = [entry for entry in phase["batch"] if entry["tool_call"] == call]
        if len(matches) != 1:
            raise CorrectionReservationUnavailable("Correction tool call changed")
        entry = matches[0]
        item, _ = self._item(phase, entry["action"]["id"])
        if (item.get("state") != "BOUND" or item.get("invocation_id") != identity.invocation_id
                or item.get("task_id") != identity.task_id or item.get("checkpoint_ns") != identity.checkpoint_ns
                or operation.run_id != run.id or operation.tool_call_id != call["id"]
                or not self._dependencies(db, run, phase, entry)
                or not self._snapshot_matches_dependencies(db, run, phase, entry, operation)):
            raise CorrectionReservationUnavailable("Correction dispatch identity or dependency changed")
        item.update({"state": "DISPATCHED", "tool_operation_id": operation.id})
        self._save(ledger, phase)

    async def confirm_record(self, action, message):
        async with self.host.serial:
            with self.worker.transaction() as db:
                run, ledger, phase = self._phase(db, require_budget=False)
                item, entry = self._item(phase, action.get("id"))
                op = db.get(ToolOperation, item.get("tool_operation_id"))
                if (entry["action"] != action or item.get("state") != "DISPATCHED"
                        or not self._operation_matches(run, entry, op)
                        or message.tool_call_id != op.tool_call_id or message.name != op.tool_name):
                    return {"status": "UNKNOWN"}
                try:
                    if json.loads(message.content) != op.result:
                        return {"status": "UNKNOWN"}
                except (TypeError, ValueError):
                    return {"status": "UNKNOWN"}
                item["state"] = "RECORDED"
                verified, ready = self._verified_result(db, run, phase, action["id"])
                if not verified:
                    return {"status": "UNKNOWN"}
                self._save(ledger, phase)
                return {"status": "RECORDED", "operation_id": op.id, "dependency_ready": ready}

    async def finalize(self):
        """Close an entirely authenticated batch without claiming goal success."""
        async with self.host.serial:
            with self.worker.transaction() as db:
                run, ledger, phase = self._phase(db, require_budget=False)
                if not phase["items"] or any(item.get("state") != "RECORDED" for item in phase["items"]):
                    raise CorrectionReservationUnavailable("Correction batch is not fully recorded")
                for item in phase["items"]:
                    verified, _ = self._verified_result(db, run, phase, item["action_id"])
                    if not verified:
                        raise CorrectionReservationUnavailable("Correction result could not be authenticated")
                phase["state"] = "COMPLETED"
                self._save(ledger, phase)
                ledger.status, ledger.finished_at = "completed", now()
                return {"status": "ACTIONS_RECORDED", "goal_success": "UNVERIFIED"}

    async def stop_after_failure(self, exc):
        """Invalidate replay authority; preserve existing control exceptions."""
        from .correction_phase import _mark_phase_review
        from .m3_host import AgentCancellationRequested, AgentControlPending

        self.saver._listener.claim_failed(self.host.run_id)
        if isinstance(exc, (AgentCancellationRequested, AgentControlPending)):
            return
        try:
            await asyncio.wait_for(self.host.scheduler.wait_operations(), timeout=5)
        except BaseException:
            return
        _mark_phase_review(self.worker, self.host.run_id, self.saver._grant.phase_id)


async def build_bound_correction_graph(*, host, boundary, saver, checkpointer):
    """Assemble only the fixed host adapter and existing real gateway middleware."""
    from .correction_graph import build_correction_tool_graph
    from .m4_runtime import ExecutionBoundary
    from .m4_tools import WorkspaceToolGateway
    from .v10_namespace_prototype import CorrectionNamespacePrototype

    if (type(boundary) is not ExecutionBoundary or boundary.host is not host
            or type(boundary.gateway) is not WorkspaceToolGateway or boundary.gateway.host is not host
            or boundary.scheduler is not boundary.gateway.scheduler
            or type(checkpointer) is not CorrectionNamespacePrototype or checkpointer.delegate is not saver
            or checkpointer.run_id != host.run_id or checkpointer.prefix != saver._namespace):
        raise CorrectionReservationUnavailable("Correction graph assembly differs")
    binding = CorrectionGatewayBinding(host, saver)
    scope, payload = await binding.load()
    graph = build_correction_tool_graph(
        workspace=boundary.gateway.workspace, execution_boundary=boundary, frozen_task_scope=scope,
        verify_dependencies=binding.verify_dependencies, confirm_record=binding.confirm_record,
        bind_request=binding.bind_request, failure_handler=binding.stop_after_failure, checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": host.run_id, "omni_task_scope": scope}}
    return graph, payload, config
