"""Owner-bound Deep Agents call boundaries; not wired to the product Worker.

This building block deliberately fails closed on ambiguous model/tool re-entry.
It does not reconcile checkpoints, deliver user commands, or implement pause
and resume. The product Agent API must remain unavailable until those pieces
and the fenced checkpointer are integrated and verified end to end.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt
from sqlalchemy import select

from .m3_agents import AgentBoundaryError, InvocationIdentity
from .m3_budget import ReservationResult, reserve_invocation
from .models import (
    AgentRun,
    ArtifactVersion,
    ModelConfig,
    OperationRecord,
    Provider,
    Run,
    RunBudgetReservation,
    RunCommand,
    ToolOperation,
    now,
)
from .services import (
    add_event,
    apply_commands,
    begin_operation,
    finish_operation,
    mark_uncertain,
    operation_id,
    validate_agent_run_versions,
)


class AgentControlPending(AgentBoundaryError):
    """A durable user command requires host reconciliation before dispatch."""


class AgentCancellationRequested(AgentBoundaryError):
    """The host must persist a cancelled state without sending a new call."""


class AgentOperationNeedsReview(AgentBoundaryError):
    """An existing call identity cannot safely be replayed."""


DelegationVerifier = Callable[[InvocationIdentity], Awaitable[bool]]


def _failure_diagnostic(exc: BaseException, transport_mode: str) -> dict:
    """Keep only bounded classifications; exception text is never evidence."""
    import httpx
    import openai
    from sqlalchemy.exc import SQLAlchemyError

    from .providers import _safe_error

    allowed_codes = {
        "DEPENDENCY_MISSING", "PROVIDER_TIMEOUT", "PROVIDER_AUTH", "PROVIDER_RATE_LIMIT",
        "PROVIDER_NOT_FOUND", "PROVIDER_REQUEST_REJECTED", "PROVIDER_UNAVAILABLE",
        "PROVIDER_CONNECTION", "PROVIDER_ERROR",
    }
    code = _safe_error(exc).code
    if not isinstance(code, str) or code not in allowed_codes:
        code = "PROVIDER_ERROR"
    status = getattr(exc, "status_code", None)
    if type(status) is not int:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    if isinstance(exc, asyncio.CancelledError):
        category = "cancelled"
    elif isinstance(exc, (TimeoutError, httpx.TimeoutException, openai.APITimeoutError)):
        category = "timeout"
    elif isinstance(exc, (httpx.HTTPStatusError, openai.APIStatusError)):
        category = "http_status"
    elif isinstance(exc, (ConnectionError, httpx.NetworkError, openai.APIConnectionError)):
        category = "connection"
    elif isinstance(exc, SQLAlchemyError):
        category = "database"
    elif isinstance(exc, AgentBoundaryError):
        category = "boundary"
    else:
        category = "unknown"
    return {"error_code": code, "http_status": status, "exception_category": category,
            "transport_mode": transport_mode if transport_mode in {
                "ainvoke_default", "compatible-native-stream-v1"} else "unknown"}


class FencedAgentHost:
    """Synchronous short DB transactions on the held WorkerLock connection.

    One instance is shared by all subagents of a Run. Callers must not access
    the owner connection concurrently from elsewhere while a hook is running.
    A delegation verifier is trusted host code and must prove a matching child
    graph checkpoint exists before it returns True; absence fails closed.
    """

    def __init__(self, worker, run_id: str, workflow_version: str, *, role_versions: dict[str, str],
                 plugin_versions: dict[str, str], role_bindings: dict[str, str],
                 verify_delegation_resume: DelegationVerifier | None = None,
                 coordinator_role: str = "coordinator"):
        if worker.lock is None or worker.lock.lost:
            raise AgentBoundaryError("A live Worker execution lock is required")
        if not run_id or not workflow_version or not role_bindings:
            raise AgentBoundaryError("Run, workflow and explicit role bindings are required")
        self.worker, self.run_id, self.workflow_version = worker, run_id, workflow_version
        self.role_versions, self.plugin_versions = deepcopy(role_versions), deepcopy(plugin_versions)
        self.role_bindings = deepcopy(role_bindings)
        self.coordinator_role = coordinator_role
        self.verify_delegation_resume = verify_delegation_resume
        self.serial = asyncio.Lock()

    def _run(self, db, role: str, *, allow_supplements: bool = False, allow_control: bool = False):
        run = db.scalar(select(Run).where(Run.id == self.run_id).with_for_update())
        if run is None:
            raise AgentBoundaryError("Run is unavailable")
        validate_agent_run_versions(run, self.workflow_version, role_versions=self.role_versions,
                                    plugin_versions=self.plugin_versions)
        if not allow_control:
            from .history_scope import require_current_scope_authorized
            require_current_scope_authorized(db, run)
        role_key = self.role_bindings.get(role)
        roles = run.config_snapshot["agent"]["roles"]
        if not role_key or role_key not in roles:
            raise AgentBoundaryError("Agent role has no frozen model binding")
        frozen_model = roles[role_key]["model"]
        model = db.get(ModelConfig, frozen_model["id"])
        provider = db.get(Provider, model.provider_id) if model else None
        if provider is None or not provider.enabled:
            raise AgentBoundaryError("A model used by this Run has been disabled")
        if not allow_control and run.status != "running":
            raise AgentControlPending("Run is not currently executable")
        pending = select(RunCommand.id).where(RunCommand.run_id == self.run_id, RunCommand.status == "pending")
        if allow_supplements:
            pending = pending.where(RunCommand.command_type != "append_requirement")
        if db.scalar(pending.limit(1)):
            # This layer cannot yet safely consume or checkpoint commands.
            raise AgentControlPending("User command requires checkpoint reconciliation")
        return run, roles[role_key]

    @staticmethod
    def _binding(identity: InvocationIdentity, role_snapshot: dict, plugin_versions: dict, kind: str,
                 role: str, tool_name: str | None) -> str:
        # Hash encrypted config, version metadata and invocation input hash. No
        # raw request content, provider credential, or snapshot is logged.
        data = {"request": identity.binding_fingerprint, "kind": kind, "role": role, "tool": tool_name,
                "role_snapshot": role_snapshot, "plugins": plugin_versions}
        if identity.purpose != "task":
            data["purpose"] = identity.purpose
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _node(kind: str, role: str) -> str:
        return f"agent:{role}:{kind}"

    def _agent_run_id(self, invocation_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"omniharness:{self.run_id}:{invocation_id}"))

    def _verify_identity(self, identity: InvocationIdentity):
        if not isinstance(identity, InvocationIdentity) or identity.thread_id != self.run_id:
            raise AgentBoundaryError("Invocation thread does not match the Run")

    async def check_control(self, kind: str, role: str, tool_name: str | None) -> None:
        del kind, tool_name
        async with self.serial:
            if self.worker.lock is None:
                raise AgentBoundaryError("A live Worker execution lock is required")
            with self.worker.transaction() as db:
                # Supplement commands stay pending until their stable message ID
                # is actually present in a synchronously saved model input.
                apply_commands(db, self.run_id, defer_requirements=True)
                run, _ = self._run(db, role, allow_supplements=True, allow_control=True)
                status = run.status
        if status == "cancel_requested":
            raise AgentCancellationRequested("Run cancellation requested")
        if status == "pause_requested":
            signal = interrupt({"type": "pause", "run_id": self.run_id})
            if signal != "continue":
                raise AgentControlPending("Invalid resume signal")
            async with self.serial:
                with self.worker.transaction() as db:
                    run, _ = self._run(db, role, allow_supplements=True, allow_control=True)
                    if run.status != "running":
                        raise AgentControlPending("Run was not resumed by its owner")
        elif status != "running":
            raise AgentControlPending("Run is not currently executable")

    async def stage_requirements(self, role: str, existing_messages) -> list[HumanMessage]:
        """Prepare stable-ID messages without marking commands consumed.

        The caller MUST checkpoint the returned state before any model call.
        """
        existing = {m.id: m.content for m in existing_messages
                    if isinstance(m, HumanMessage) and isinstance(m.id, str) and m.id.startswith("omni-command:")}
        async with self.serial:
            if self.worker.lock is None:
                raise AgentBoundaryError("A live Worker execution lock is required")
            with self.worker.transaction() as db:
                self._run(db, role, allow_supplements=True)
                pending = list(db.scalars(select(RunCommand).where(
                    RunCommand.run_id == self.run_id, RunCommand.status.in_(["pending", "applied"]),
                    RunCommand.command_type == "append_requirement",
                ).order_by(RunCommand.sequence_number)))
                staged = []
                for command in pending:
                    if command.status == "applied" and command.payload.get("delivery") != "prepared_for_model":
                        # Older chat-only 'applied' means recorded, not delivered.
                        continue
                    message_id = f"omni-command:{command.id}"
                    content = command.payload.get("content")
                    if not isinstance(content, str) or not content:
                        raise AgentBoundaryError("A persisted user requirement is invalid")
                    if message_id in existing:
                        if existing[message_id] != content:
                            raise AgentBoundaryError("A checkpoint requirement differs from the saved command")
                    else:
                        staged.append(HumanMessage(id=message_id, content=content))
                return staged

    async def reserve(self, kind: str, role: str, tool_name: str | None) -> bool:
        del kind, role, tool_name
        raise AgentBoundaryError("A durable invocation identity is required")

    async def reserve_invocation(self, kind: str, role: str, tool_name: str | None,
                                 identity: InvocationIdentity, *, prepare=None, on_reserved=None) -> bool:
        self._verify_identity(identity)
        async with self.serial:
            if self.worker.lock is None:
                raise AgentBoundaryError("A live Worker execution lock is required")
            with self.worker.transaction() as db:
                run, role_snapshot = self._run(db, role, allow_supplements=True)
                if self.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
                    # The audit writer uses the same fenced Run lock; check before
                    # any new reservation so a sibling cannot dispatch after the limit.
                    from .m4_invalid_audit import assert_delegation_unclaimed, assert_not_limited
                    assert_not_limited(db, self.run_id, self.workflow_version)
                    if kind == "tool_calls" and tool_name == "task":
                        assert_delegation_unclaimed(db, self.run_id, identity, self.workflow_version)
                pending_requirements = list(db.scalars(select(RunCommand).where(
                    RunCommand.run_id == self.run_id, RunCommand.status == "pending",
                    RunCommand.command_type == "append_requirement",
                ).order_by(RunCommand.sequence_number).with_for_update()))
                if identity.purpose == "task" and pending_requirements and (kind != "model_calls"
                                             or any(item.id not in identity.command_ids for item in pending_requirements)):
                    raise AgentControlPending("Requirements have not yet been checkpointed into the next model input")
                binding = self._binding(identity, role_snapshot, self.plugin_versions, kind, role, tool_name)
                reservation = reserve_invocation(db, run_id=self.run_id, kind=kind,
                                                 invocation_id=identity.invocation_id,
                                                 binding_fingerprint=binding)
                if reservation == ReservationResult.EXHAUSTED:
                    return False
                if reservation == ReservationResult.RESERVED and prepare is not None:
                    prepare(db)  # Raising rolls back the budget and the slot together.
                if reservation == ReservationResult.ALREADY_RESERVED:
                    needs_proof = kind == "tool_calls" and tool_name == "task"
                    if not needs_proof:
                        raise AgentOperationNeedsReview("Previous invocation requires review before any replay")
                elif kind == "tool_calls" and tool_name == "task":
                    if identity.delegated_role not in self.role_bindings:
                        raise AgentBoundaryError("Delegation target is not bound")
                    add_event(db, run, "agent.delegated", {"role": identity.delegated_role,
                                                           "parent_role": role,
                                                           "invocation_id": identity.invocation_id})
                    return True
                else:
                    row, executable = begin_operation(db, run.id, self._node(kind, role),
                                                      {"invocation_id": identity.invocation_id},
                                                      self.workflow_version)
                    if not executable:
                        raise AgentOperationNeedsReview("Previous operation requires review before any replay")
                    from .model_usage import model_binding
                    row.result = {"binding_fingerprint": binding, "purpose": identity.purpose,
                                  "model_binding": model_binding(role_snapshot.get("model"))}
                    if on_reserved is not None:
                        on_reserved(db, row)
                    if kind == "model_calls":
                        depth = sum(part.startswith("tools:") for part in identity.checkpoint_ns.split("|"))
                        if depth > 2:
                            raise AgentBoundaryError("Maximum delegation depth exceeded")
                        node = AgentRun(id=self._agent_run_id(identity.invocation_id), run_id=run.id,
                                        role=role, status="running", depth=depth,
                                        model_config_id=role_snapshot["model"]["id"], started_at=now())
                        db.add(node)
                        db.flush()
                        add_event(db, run, "agent.model_started", {"agent_run_id": node.id, "role": role})
                    for command in pending_requirements if identity.purpose == "task" else ():
                        command.status, command.applied_at = "applied", now()
                        command.payload = {**command.payload, "delivery": "prepared_for_model"}
                        add_event(db, run, "run.command", {
                            "command_id": command.id, "sequence_number": command.sequence_number,
                            "command_type": "append_requirement", "result": "applied", "reason": None,
                            "run_status": run.status, "delivery": "prepared_for_model",
                            "content": command.payload.get("content"),
                        })
                    return True
        # Only a trusted host can prove a child checkpoint belongs to this
        # exact task. Do not hold the synchronous DB transaction over an await.
        if self.verify_delegation_resume and await self.verify_delegation_resume(identity):
            await self.check_control("delegation", role, "task")
            return True
        raise AgentOperationNeedsReview("Delegation has no verified resumable child checkpoint")

    async def replay_invocation(self, kind: str, role: str, tool_name: str | None,
                                identity: InvocationIdentity) -> dict | None:
        """Return exact saved evidence; a recorded dispatch without it never reruns."""
        self._verify_identity(identity)
        async with self.serial:
            with self.worker.transaction() as db:
                run, role_snapshot = self._run(db, role, allow_supplements=True)
                binding = self._binding(identity, role_snapshot, self.plugin_versions, kind, role, tool_name)
                reserved = db.get(RunBudgetReservation, {"run_id": self.run_id,
                                                        "invocation_id": identity.invocation_id})
                if reserved is None:
                    return None
                if reserved.kind != kind or reserved.binding_fingerprint != binding:
                    raise AgentOperationNeedsReview("Saved invocation binding differs")
                op_id = operation_id(self.run_id, self._node(kind, role), {"invocation_id": identity.invocation_id})
                row = db.get(OperationRecord, op_id)
                if (row is None or row.status != "completed" or not isinstance(row.result, dict)
                        or row.result.get("binding_fingerprint") != binding
                        or not isinstance(row.result.get("response"), dict)):
                    raise AgentOperationNeedsReview("Saved invocation has no confirmed replayable result")
                if (self.workflow_version == "stage-agent-v10" and kind == "model_calls"
                        and role == "execute" and identity.purpose == "task"):
                    import hmac

                    from .m4_collaboration import current_task_scope
                    from .v10_closure import model_scope_mac
                    scope = row.result.get("v10_model_scope")
                    signature = row.result.get("v10_model_scope_mac")
                    try:
                        logical_task = current_task_scope()["id"]
                        valid = (isinstance(scope, dict) and scope.get("run_id") == run.id
                            and scope.get("task_id") == logical_task
                            and scope.get("invocation_id") == identity.invocation_id
                            and scope.get("checkpoint_ns") == identity.checkpoint_ns
                            and scope.get("graph_task_id") == identity.task_id
                            and isinstance(signature, str) and hmac.compare_digest(signature,
                                model_scope_mac(self.worker.settings, run, row.operation_id,
                                                scope, row.result["response"])))
                    except (KeyError, ValueError, RuntimeError, TypeError):
                        valid = False
                    if not valid:
                        raise AgentOperationNeedsReview("Saved execution model scope cannot be authenticated")
                return deepcopy(row.result["response"])

    async def _finish(self, kind: str, role: str, tool_name: str | None,
                      identity: InvocationIdentity, *, uncertain: bool, result: dict | None = None,
                      result_factory=None, diagnostic: dict | None = None):
        self._verify_identity(identity)
        if tool_name == "task":
            raise AgentBoundaryError("Delegation is completed by its child graph")
        async with self.serial:
            if self.worker.lock is None:
                raise AgentBoundaryError("A live Worker execution lock is required")
            with self.worker.transaction() as db:
                # Preserve result evidence even if cancel/pause was requested
                # while the already-dispatched operation was in flight.
                run = db.get(Run, self.run_id)
                if run is None:
                    raise AgentBoundaryError("Run is unavailable")
                validate_agent_run_versions(run, self.workflow_version, role_versions=self.role_versions,
                                            plugin_versions=self.plugin_versions)
                role_key = self.role_bindings.get(role)
                roles = run.config_snapshot["agent"]["roles"]
                if not role_key or role_key not in roles:
                    raise AgentBoundaryError("Agent role has no frozen model binding")
                binding = self._binding(identity, roles[role_key], self.plugin_versions, kind, role, tool_name)
                reserved = db.get(RunBudgetReservation, {"run_id": self.run_id,
                                                        "invocation_id": identity.invocation_id})
                if reserved is None or reserved.kind != kind or reserved.binding_fingerprint != binding:
                    raise AgentBoundaryError("Invocation does not match its frozen reservation")
                op_id = operation_id(self.run_id, self._node(kind, role), {"invocation_id": identity.invocation_id})
                if uncertain:
                    changed = mark_uncertain(db, op_id, "RESULT_UNKNOWN")
                    if changed and diagnostic is not None:
                        operation = db.get(OperationRecord, op_id)
                        operation.result = {**(operation.result or {}), "diagnostic": diagnostic}
                else:
                    if result_factory is not None:
                        result = result_factory(db)
                    from .model_usage import model_binding
                    evidence = {"outcome": "completed", "purpose": identity.purpose,
                                "model_binding": model_binding(roles[role_key].get("model"))}
                    if (self.workflow_version == "stage-agent-v10" and kind == "model_calls"
                            and role == "execute" and identity.purpose == "task"):
                        reserved_operation = db.get(OperationRecord, op_id)
                        saved = reserved_operation.result if reserved_operation else None
                        scope = saved.get("v10_model_scope") if isinstance(saved, dict) else None
                        if (not isinstance(scope, dict) or scope.get("run_id") != run.id
                                or scope.get("invocation_id") != identity.invocation_id
                                or scope.get("checkpoint_ns") != identity.checkpoint_ns
                                or scope.get("graph_task_id") != identity.task_id
                                or not isinstance(scope.get("task_id"), str) or not scope["task_id"]
                                or not isinstance(result, dict)):
                            raise AgentOperationNeedsReview("Execution model scope binding unavailable")
                        from .v10_closure import model_scope_mac
                        evidence["v10_model_scope"] = scope
                        evidence["v10_model_scope_mac"] = model_scope_mac(
                            self.worker.settings, run, op_id, scope, result)
                    if self.workflow_version in {"stage-agent-v9", "stage-agent-v10"} and kind == "tool_calls":
                        reserved_operation = db.get(OperationRecord, op_id)
                        reserved_result = reserved_operation.result if reserved_operation else None
                        mac = reserved_result.get("v9_request_mac") if isinstance(reserved_result, dict) else None
                        snapshot_mac = reserved_result.get("v9_snapshot_mac") if isinstance(reserved_result, dict) else None
                        if not isinstance(mac, str) or not isinstance(snapshot_mac, str):
                            raise AgentOperationNeedsReview("Tool request evidence binding unavailable")
                        evidence["v9_request_mac"] = mac
                        evidence["v9_snapshot_mac"] = snapshot_mac
                        from .review_evidence import v9_response_mac
                        if not isinstance(result, dict):
                            raise AgentOperationNeedsReview("Tool response evidence unavailable")
                        tool_operation = db.scalar(select(ToolOperation).where(
                            ToolOperation.run_id == run.id, ToolOperation.operation_record_id == op_id))
                        if tool_operation is None:
                            raise AgentOperationNeedsReview("Tool response scope unavailable")
                        evidence["v9_response_mac"] = v9_response_mac(
                            self.worker.settings, run, tool_operation.id, mac, result)
                    if result is not None:
                        evidence.update(binding_fingerprint=binding, response=deepcopy(result))
                    changed = finish_operation(db, op_id, "completed", result=evidence)
                if not changed:
                    raise AgentOperationNeedsReview("Operation state changed; review is required")
                if kind == "model_calls":
                    node = db.get(AgentRun, self._agent_run_id(identity.invocation_id))
                    if node is None or node.run_id != self.run_id:
                        raise AgentBoundaryError("Agent invocation record is unavailable")
                    node.status = "pending_review" if uncertain else "completed"
                    node.error_code = "RESULT_UNKNOWN" if uncertain else None
                    node.finished_at = now()
                    usage, raw = None, None
                    if result is not None:
                        message = result.get("message")
                        if message is None and isinstance(result.get("messages"), list) and result["messages"]:
                            message = result["messages"][0]
                        raw = message.get("usage_metadata") if isinstance(message, dict) else None
                        if identity.purpose == "context_summary" and result.get("artifact_id"):
                            artifact = db.get(ArtifactVersion, result["artifact_id"])
                            raw = artifact.metadata_json.get("usage") if artifact and artifact.run_id == self.run_id else None
                        keys = ("input_tokens", "output_tokens", "total_tokens")
                        if isinstance(raw, dict) and all(type(raw.get(key)) is int and raw[key] >= 0 for key in keys):
                            usage = {key: raw[key] for key in keys}
                    if result is not None or identity.purpose != "task" or self.workflow_version in {"stage-agent-v3", "stage-agent-v4", "stage-agent-v5", "stage-agent-v6", "stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
                        previous = run.usage or {}
                        complete = previous.get("usage_complete", True) and usage is not None
                        observed = previous.get("observed_tokens", {})
                        observed = {key: observed.get(key, 0) + (usage or {}).get(key, 0)
                                    for key in ("input_tokens", "output_tokens", "total_tokens")}
                        run.usage = {**{key: value if complete else None for key, value in observed.items()},
                                     "observed_tokens": observed, "usage_complete": complete,
                                     "model_calls": previous.get("model_calls", 0) + 1,
                                     "estimated_cost": None}
                    from .model_usage import normalize_usage
                    add_event(db, run, "agent.model_uncertain" if uncertain else "agent.model_completed",
                              {"agent_run_id": node.id, "role": role, "purpose": identity.purpose, "usage": usage,
                               "usage_details": normalize_usage(raw),
                               **({"diagnostic": diagnostic} if diagnostic is not None else {})})
                return result

    async def finish_invocation(self, kind: str, role: str, tool_name: str | None,
                                identity: InvocationIdentity, *, result: dict | None = None, result_factory=None):
        return await self._finish(kind, role, tool_name, identity, uncertain=False,
                                  result=result, result_factory=result_factory)

    async def mark_invocation_uncertain(self, kind: str, role: str, tool_name: str | None,
                                        identity: InvocationIdentity) -> None:
        await self._finish(kind, role, tool_name, identity, uncertain=True)

    async def mark_invocation_uncertain_with_diagnostics(
        self, kind: str, role: str, tool_name: str | None, identity: InvocationIdentity,
        *, error: BaseException, transport_mode: str,
    ) -> None:
        await self._finish(kind, role, tool_name, identity, uncertain=True,
                           diagnostic=_failure_diagnostic(error, transport_mode))
