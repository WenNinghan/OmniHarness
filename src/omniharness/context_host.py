"""Durable, budgeted summaries shared by ordinary chat and Agent boundaries.

No model runs inside a database transaction. Completed evidence is replayable;
an uncertain dispatch is not. Source messages remain in their original store.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage, message_to_dict
from langsmith import tracing_context
from sqlalchemy import select

from .artifacts import ArtifactStore
from .context_policy import (
    ContextPolicyV2,
    ContextPolicyV3,
    context_budget,
    counter_for_policy,
    parse_context_configuration,
)
from .context_runtime import (
    ContextInput,
    ContextPreparationError,
    ContextScope,
    MessageSource,
    StoredSummary,
    SummaryRequest,
    count_summary_output,
    prepare_context,
    public_message,
)
from .m3_agents import AgentBoundaryError, AgentBudgetExceeded, InvocationIdentity
from .m3_budget import ReservationResult, reserve_invocation
from .m3_host import AgentCancellationRequested, AgentControlPending, AgentOperationNeedsReview
from .model_output import extract_output, output_issue
from .models import (
    ArtifactVersion,
    Message,
    ModelConfig,
    OperationRecord,
    Provider,
    Run,
    RunBudgetReservation,
    RunEvent,
)
from .providers import (
    ProviderError,
    _safe_error,
    agent_streaming_enabled,
    build_chat_model,
    freeze_summary_capability,
)
from .services import (
    add_event,
    apply_commands,
    begin_operation,
    finish_operation,
    mark_uncertain,
    operation_id,
    runtime_config,
)


def model_fingerprint(snapshot: dict) -> str:
    """Opaque frozen binding; never log the snapshot or decrypted credentials."""
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class _ChatSummaryBoundary:
    """Small chat adapter for the same invocation ledger, without Agent roles."""
    def __init__(self, worker, run_id, snapshot):
        self.worker, self.run_id, self.snapshot = worker, run_id, deepcopy(snapshot)
        self.serial = asyncio.Lock()

    def _run(self, db, *, controls=False):
        # SQLite direct execution is the existing explicit local-test mode.
        if self.worker.engine.dialect.name == "postgresql" and self.worker.lock is None:
            raise AgentBoundaryError("A live Worker execution lock is required")
        row = db.scalar(select(Run).where(Run.id == self.run_id).with_for_update())
        if (row is None or row.config_snapshot.get("workflow_type") == "agent"
                or model_fingerprint(row.config_snapshot) != model_fingerprint(self.snapshot)):
            raise AgentBoundaryError("Chat summary frozen configuration differs")
        if not controls:
            from .history_scope import require_current_scope_authorized
            require_current_scope_authorized(db, row)
            apply_commands(db, row.id, defer_requirements=True)
            if row.status == "cancel_requested":
                raise AgentCancellationRequested("Run cancellation requested")
            if row.status != "running":
                raise AgentControlPending("Run is not currently executable")
            model = db.get(ModelConfig, row.model_config_id)
            provider = db.get(Provider, model.provider_id) if model else None
            if provider is None or not provider.enabled:
                raise AgentBoundaryError("The frozen model is disabled")
        return row

    def _binding(self, identity):
        if identity.thread_id != self.run_id or identity.purpose != "context_summary":
            raise AgentBoundaryError("Summary invocation scope differs")
        return model_fingerprint({"request": identity.binding_fingerprint, "purpose": identity.purpose,
                                  "model": self.snapshot})

    def _operation_id(self, identity):
        return operation_id(self.run_id, "chat:context_summary", {"invocation_id": identity.invocation_id})

    async def check_control(self, *_):
        async with self.serial:
            with self.worker.transaction() as db:
                self._run(db)

    async def replay_invocation(self, kind, role, tool_name, identity):
        del role, tool_name
        async with self.serial:
            with self.worker.transaction() as db:
                self._run(db)
                binding = self._binding(identity)
                reserved = db.get(RunBudgetReservation, {"run_id": self.run_id,
                                                        "invocation_id": identity.invocation_id})
                if reserved is None:
                    return None
                row = db.get(OperationRecord, self._operation_id(identity))
                if (reserved.kind != kind or reserved.binding_fingerprint != binding or row is None
                        or row.status != "completed" or not isinstance(row.result, dict)
                        or row.result.get("binding_fingerprint") != binding
                        or not isinstance(row.result.get("response"), dict)):
                    raise AgentOperationNeedsReview("Saved summary dispatch has no replayable result")
                return deepcopy(row.result["response"])

    async def reserve_invocation(self, kind, role, tool_name, identity, *, on_reserved=None):
        del role, tool_name
        async with self.serial:
            with self.worker.transaction() as db:
                self._run(db)
                binding = self._binding(identity)
                outcome = reserve_invocation(db, run_id=self.run_id, kind=kind,
                    invocation_id=identity.invocation_id, binding_fingerprint=binding)
                if outcome == ReservationResult.EXHAUSTED:
                    return False
                if outcome != ReservationResult.RESERVED:
                    raise AgentOperationNeedsReview("Existing summary dispatch cannot be repeated")
                row, executable = begin_operation(db, self.run_id, "chat:context_summary",
                    {"invocation_id": identity.invocation_id}, "context-v1")
                if not executable:
                    raise AgentOperationNeedsReview("Existing summary operation cannot be repeated")
                row.result = {"binding_fingerprint": binding}
                if on_reserved:
                    on_reserved(db, row)
                return True

    async def finish_invocation(self, kind, role, tool_name, identity, *, result_factory):
        del role, tool_name
        async with self.serial:
            with self.worker.transaction() as db:
                self._run(db, controls=True)
                binding = self._binding(identity)
                reserved = db.get(RunBudgetReservation, {"run_id": self.run_id,
                                                        "invocation_id": identity.invocation_id})
                if reserved is None or reserved.kind != kind or reserved.binding_fingerprint != binding:
                    raise AgentOperationNeedsReview("Summary reservation differs")
                result = result_factory(db)
                if not finish_operation(db, self._operation_id(identity), "completed", result={
                    "binding_fingerprint": binding, "response": deepcopy(result),
                }):
                    raise AgentOperationNeedsReview("Summary completion requires review")
                return result

    async def mark_invocation_uncertain(self, kind, role, tool_name, identity):
        del kind, role, tool_name
        async with self.serial:
            with self.worker.transaction() as db:
                self._run(db, controls=True)
                mark_uncertain(db, self._operation_id(identity), "SUMMARY_RESULT_UNKNOWN")


def _usage(message: AIMessage) -> dict | None:
    # Only standard numeric usage crosses the boundary, never provider metadata.
    usage = message.usage_metadata
    if not usage:
        return None
    result = {name: usage[name] for name in ("input_tokens", "output_tokens", "total_tokens")
              if type(usage.get(name)) is int and usage[name] >= 0}
    for name in ("input_token_details", "output_token_details"):
        if isinstance(usage.get(name), dict):
            result[name] = {key: value for key, value in usage[name].items()
                            if key in {"audio", "cache_read", "cache_creation", "reasoning"}
                            and type(value) is int and value >= 0}
    return result or None


class DurableSummaryExecutor:
    def __init__(self, worker, run_id: str, snapshot: dict, *, host=None, model_factory=None):
        self.worker, self.run_id, self.snapshot = worker, run_id, deepcopy(snapshot)
        self.host = host or _ChatSummaryBoundary(worker, run_id, snapshot)
        if self.host.run_id != run_id:
            raise AgentBoundaryError("Summary host belongs to another Run")
        def default_factory(config):
            if agent_streaming_enabled(self.snapshot, getattr(self.host, "workflow_version", ""), config):
                return build_chat_model(config, native_streaming=True)
            return build_chat_model(config)
        self.model_factory = model_factory or default_factory
        self.store = ArtifactStore(worker.settings.data_dir / "artifacts")

    def _restore(self, response: dict, request: SummaryRequest, *, incomplete=False) -> StoredSummary:
        with self.worker.transaction() as db:
            row = db.get(ArtifactVersion, response.get("artifact_id"))
            if (row is None or row.run_id != self.run_id or row.kind != "context_summary"
                    or row.operation_id != response.get("operation_id") or row.sha256 != response.get("sha256")):
                raise AgentOperationNeedsReview("Summary artifact is unavailable or belongs to another scope")
            # ArtifactStore verifies the full hash on each bounded read.
            data = b"".join(self.store.read(row, run_id=self.run_id, offset=offset,
                            limit=min(1024 * 1024, row.size_bytes - offset))
                            for offset in range(0, row.size_bytes, 1024 * 1024))
            value = json.loads(data)
        if (value.get("schema_version") != 1 or value.get("scope") != asdict(request.scope)
                or value.get("invocation_id") != request.invocation_id
                or value.get("binding_fingerprint") != request.binding_fingerprint
                or value.get("sources") != json.loads(json.dumps([asdict(item) for item in request.sources]))):
            raise AgentOperationNeedsReview("Summary evidence binding differs")
        if value["output_status"] != "complete" and not incomplete:
            raise ContextPreparationError("SUMMARY_INCOMPLETE", "Saved summary did not complete; history is unchanged")
        return StoredSummary(response["artifact_id"], request.invocation_id, request.binding_fingerprint,
                             value["text"], tuple(MessageSource(item["message_id"], item["sha256"],
                             tuple(item["artifact_refs"])) for item in value["sources"]),
                             value["usage"], value["finish_reason"], value["output_status"],
                             value.get("recovery_of"), value.get("output_tokens"), value.get("request_binding"))

    async def execute(self, request: SummaryRequest) -> StoredSummary:
        if (request.scope.run_id != self.run_id or request.scope.model_config_id != self.snapshot["id"]
                or request.scope.model_fingerprint != model_fingerprint(self.snapshot)
                or request.purpose != "context_summary"):
            raise AgentBoundaryError("Summary request does not match its frozen model")
        with self.worker.transaction() as db:
            run = db.get(Run, self.run_id)
            if run is None:
                raise AgentBoundaryError("Summary Run is unavailable")
            frozen = run.config_snapshot
            if frozen.get("workflow_type") == "agent":
                role_key = getattr(self.host, "role_bindings", {}).get(request.scope.role)
                frozen = (frozen.get("agent", {}).get("roles", {}).get(role_key) or {}).get("model")
            if not isinstance(frozen, dict) or model_fingerprint(frozen) != model_fingerprint(self.snapshot):
                raise AgentBoundaryError("Summary model does not match its frozen role")
        policy, limits = parse_context_configuration(self.snapshot.get("context_policy"))
        if (request.output_tokens != policy.summary_output_tokens
                or request.attempt != 0 or request.recovery_of is not None
                or request.policy_version != policy.version
                or request.policy_fingerprint != model_fingerprint(asdict(policy))):
            raise AgentBoundaryError("Summary request policy differs from its frozen configuration")
        first = await self._execute_attempt(request, policy)
        if first.output_status == "complete":
            return first
        known_tokens = (first.usage or {}).get("output_tokens")
        if (not isinstance(policy, ContextPolicyV2) or policy.summary_recovery_attempts != 1
                or first.finish_reason != "length" or type(known_tokens) is not int
                or known_tokens < request.output_tokens):
            raise ContextPreparationError("SUMMARY_INCOMPLETE", "Saved summary did not complete; history is unchanged")
        recovery_budget = context_budget(policy, limits, output_tokens=policy.summary_recovery_output_tokens)
        # Recount the exact same public prompt with the same frozen heuristic.
        tokens = counter_for_policy(policy).count(list(request.messages))
        if tokens > recovery_budget.input_limit:
            raise ContextPreparationError("SUMMARY_RECOVERY_WINDOW", "Summary recovery cannot fit the frozen window")
        binding = model_fingerprint({"request_binding": request.binding_fingerprint,
                                     "recovery_of": request.invocation_id, "attempt": 1,
                                     "output_tokens": policy.summary_recovery_output_tokens})
        retry = replace(request, invocation_id=model_fingerprint({"recovery_of": request.invocation_id, "attempt": 1}),
                        binding_fingerprint=binding, output_tokens=policy.summary_recovery_output_tokens,
                        input_tokens=tokens, input_limit=recovery_budget.input_limit, attempt=1,
                        recovery_of=request.invocation_id)
        recovered = await self._execute_attempt(retry, policy, request_binding=request.binding_fingerprint)
        if recovered.output_status != "complete":
            raise ContextPreparationError("SUMMARY_INCOMPLETE", "The one permitted summary recovery did not complete")
        return recovered

    async def _execute_attempt(self, request: SummaryRequest, policy, *, request_binding=None) -> StoredSummary:
        v2 = isinstance(policy, ContextPolicyV2)
        identity = InvocationIdentity(self.run_id, request.invocation_id, request.binding_fingerprint,
                                      checkpoint_ns=request.scope.checkpoint_ns, task_id=request.scope.task_id,
                                      purpose="context_summary")
        args = ("model_calls", request.scope.role, None, identity)
        scheduler = getattr(self.host, "scheduler", None)
        if scheduler:
            await scheduler.ensure_active()
        await self.host.check_control(*args[:3])
        saved = await self.host.replay_invocation(*args)
        if saved is not None:
            return self._restore(saved, request, incomplete=v2)
        # Construction and validation before reservation have no remote side effect.
        try:
            config = replace(runtime_config(self.snapshot, self.worker.secrets), max_output_tokens=request.output_tokens)
            capability = freeze_summary_capability(self.snapshot,
                (self.snapshot.get("capabilities") or {}).get("summary_non_thinking"))
            disable_thinking = (v2 and policy.summary_thinking == "disable_if_verified"
                                and capability.get("status") == "supported")
            if disable_thinking:
                config = replace(config, parameters={**config.parameters, "thinking": {"type": "disabled"}})
            model = self.model_factory(config)
        except Exception as exc:
            raise _safe_error(exc) from None
        operation = None

        def reserved(db, row):
            nonlocal operation
            operation = row.operation_id
            if request.attempt == 1:
                # reserve_invocation already holds the shared RunBudget row
                # lock. All hosts/roles compete for this one durable Run slot.
                used = db.scalar(select(RunEvent.id).where(RunEvent.run_id == self.run_id,
                    RunEvent.event_type == "context.summary_recovery_reserved").limit(1))
                if used is not None:
                    raise ContextPreparationError("SUMMARY_RECOVERY_USED", "This Run already used its summary recovery")
                add_event(db, db.get(Run, self.run_id), "context.summary_recovery_reserved", {
                    "invocation_id": request.invocation_id, "operation_id": operation,
                    "recovery_of": request.recovery_of, "attempt": 1,
                    "output_tokens": request.output_tokens,
                })
            add_event(db, db.get(Run, self.run_id), "context.summary_started", {
                "purpose": request.purpose, "role": request.scope.role, "model_config_id": request.scope.model_config_id,
                "invocation_id": request.invocation_id, "operation_id": operation,
                "input_tokens": request.input_tokens, "input_limit": request.input_limit,
                "output_tokens": request.output_tokens, "counting_method": request.counting_method,
                "counting_is_estimate": request.counting_is_estimate, "source_count": len(request.sources),
                "attempt": request.attempt, "recovery_of": request.recovery_of,
                "thinking_mode": "disabled_verified" if disable_thinking else "inherited",
            })

        if not await self.host.reserve_invocation(*args, on_reserved=reserved):
            raise AgentBudgetExceeded("Shared model call budget exhausted before summary")

        async def dispatch_and_save():
            try:
                with tracing_context(enabled=False):
                    message = await model.ainvoke(list(request.messages), config={"callbacks": []})
                if not isinstance(message, AIMessage):
                    raise ProviderError("SUMMARY_INVALID", "摘要模型返回了无效消息。")
                output = extract_output(message)
                issue = output_issue(text=output.text, reasoning=output.reasoning,
                                     finish_reason=output.finish_reason, refusal=output.refusal,
                                     require_finish=True, has_tools=bool(message.tool_calls or message.invalid_tool_calls))
                status = issue.status if issue else "complete"
                output_count, output_estimated = count_summary_output(output.text, _usage(message), counter_for_policy(policy))
                if output_count > request.output_tokens:
                    status = "summary_output_limit"
                evidence = {
                    "schema_version": 1, "scope": asdict(request.scope), "purpose": request.purpose,
                    "invocation_id": request.invocation_id, "binding_fingerprint": request.binding_fingerprint,
                    "policy_version": request.policy_version, "policy_fingerprint": request.policy_fingerprint,
                    "sources": [asdict(item) for item in request.sources], "artifact_refs": request.artifact_refs,
                    "parent_summary_refs": request.parent_summary_refs,
                    "input_messages": [message_to_dict(public_message(item)) for item in request.messages],
                    "input_tokens": request.input_tokens, "input_limit": request.input_limit,
                    "output_tokens": request.output_tokens, "counting_method": request.counting_method,
                    "counting_is_estimate": request.counting_is_estimate,
                    "text": output.text, "usage": _usage(message), "finish_reason": output.finish_reason,
                    "output_status": status, "cost": None,
                    "output_count": output_count, "output_counting_is_estimate": output_estimated,
                    "attempt": request.attempt, "recovery_of": request.recovery_of,
                    "request_binding": request_binding,
                    "thinking_mode": "disabled_verified" if disable_thinking else "inherited",
                }

                def finish(db):
                    artifact = self.store.put(db, run_id=self.run_id, kind="context_summary", name="context-summary.json",
                        content=json.dumps(evidence, ensure_ascii=False, allow_nan=False), operation_id=operation,
                        metadata={key: evidence[key] for key in ("scope", "purpose", "invocation_id", "sources",
                            "binding_fingerprint", "policy_version", "parent_summary_refs", "usage", "output_status",
                            "attempt", "recovery_of", "thinking_mode")})
                    response = {"artifact_id": artifact["id"], "operation_id": operation, "sha256": artifact["sha256"]}
                    add_event(db, db.get(Run, self.run_id), "context.summary_completed", {
                        **response, "role": request.scope.role, "model_config_id": request.scope.model_config_id,
                        "output_status": status, "usage": evidence["usage"], "cost": None,
                        "source_count": len(request.sources), "purpose": request.purpose,
                    })
                    return response

                saved = await self.host.finish_invocation(*args, result_factory=finish)
            except BaseException as exc:
                # A lost fence cannot write; never reconnect to mark it elsewhere.
                from .worker import WorkerOwnershipLost
                if isinstance(exc, WorkerOwnershipLost):
                    raise
                try:
                    await self.host.mark_invocation_uncertain(*args)
                except WorkerOwnershipLost:
                    raise
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit, AgentBoundaryError)):
                    raise
                raise _safe_error(exc) from None
            return saved

        saved = await scheduler.run_tracked(dispatch_and_save) if scheduler else await dispatch_and_save()
        return self._restore(saved, request, incomplete=v2)


class _ContextBoundaryModel(BaseChatModel):
    """Middleware profile only; all generation goes through the durable executor."""
    @property
    def _llm_type(self):
        return "omni-context-boundary"

    def _generate(self, *args, **kwargs):
        raise AgentBoundaryError("Context summaries must use the durable model boundary")


async def prepare_chat_context(worker, run_id: str, history, snapshot: dict, *, model_factory=None):
    """Preserve Message IDs and current user input; return messages without system.

    The original database history is untouched. The caller handles pause/cancel
    boundary exceptions before reserving the final, ordinary streaming request.
    """
    messages = []
    for item in history:
        if isinstance(item, BaseMessage):
            messages.append(item)
        elif isinstance(item, dict) and item.get("role") in {"user", "assistant"}:
            cls = HumanMessage if item["role"] == "user" else AIMessage
            messages.append(cls(id=item.get("id"), content=item["content"]))
        else:
            raise ContextPreparationError("CONTEXT_INVALID", "Chat context contains an unsupported message")
    policy, limits = parse_context_configuration(snapshot.get("context_policy"))
    protected = (messages.pop(),) if messages and isinstance(messages[-1], HumanMessage) else ()
    executor = DurableSummaryExecutor(worker, run_id, snapshot, model_factory=model_factory)
    with tracing_context(enabled=False):
        prepared = await prepare_context(ContextInput(
            ContextScope(run_id, "chat", snapshot["id"], model_fingerprint(snapshot)),
            tuple(messages), suffix_messages=protected, policy=policy, model_limits=limits,
            main_output_tokens=snapshot.get("max_output_tokens", 100_000),
        ), model=_ContextBoundaryModel(), summary_executor=executor)
    return prepared


async def prepare_agent_context(host, role: str, messages, *, model, system_message=None,
                                tool_schemas=(), checkpoint_ns="", task_id="", protected_messages=()):
    """Prepare a stage/tool graph input; caller checkpoints before main dispatch."""
    async with host.serial:
        with host.worker.transaction() as db:
            _, role_snapshot = host._run(db, role, allow_supplements=True)
            snapshot = deepcopy(role_snapshot["model"])
            source_goal = db.scalar(select(Message).where(Message.run_id == host.run_id, Message.role == "user"))
            if source_goal is None:
                raise ContextPreparationError("CONTEXT_SOURCE_ID", "Persisted task goal is unavailable")
            goal_id, goal_content = source_goal.id, source_goal.content
            clarifications = None
            if getattr(host, "workflow_version", None) in {"stage-agent-v9", "stage-agent-v10"}:
                from .clarification_context import verified_clarification_messages
                clarifications = verified_clarification_messages(db, host.run_id, messages)
    policy, limits = parse_context_configuration(snapshot.get("context_policy"))
    messages = list(messages)
    if clarifications is not None:
        from .clarification_context import POLICY, POLICY_ID, PREFIX
        # State messages are checked against the DB before replacement. Only
        # freshly projected host records are placed in the protected prefix.
        messages = [m for m in messages if not (isinstance(getattr(m, "id", None), str)
                    and (m.id.startswith(PREFIX) or m.id == POLICY_ID))]
    # The caller must supply the actual host-created objects, not IDs provided
    # by state or the model. This keeps a review evidence packet intact.
    protected_objects = tuple(protected_messages)
    if any(not any(message is trusted for message in messages) for trusted in protected_objects):
        raise ContextPreparationError("CONTEXT_SOURCE_ID", "Protected context source is unavailable")
    pending, history, prefix, instructions = [], [], [], []
    last_progress = max((index for index, message in enumerate(messages)
                         if isinstance(message, (AIMessage, ToolMessage))), default=-1)
    for index, message in enumerate(messages):
        if any(message is trusted for trusted in protected_objects):
            pending.append(message)
        elif (isinstance(message, HumanMessage) and (message.id == goal_id or message.content == goal_content)
                and not prefix):
            if message.id == goal_id and message.content != goal_content:
                raise ContextPreparationError("CONTEXT_SOURCE_ID", "Task goal differs from its persisted source")
            prefix.append(message)
        elif (role == "execute" and isinstance(message, HumanMessage) and isinstance(message.id, str)
              and (message.id.startswith("omni-task:")
                   or (message.id.startswith("omni-command:") and index < last_progress))):
            # Keep the task and already-delivered requirements verbatim ahead
            # of progress. Re-appending them after a ToolMessage reissues the
            # task on every model turn and can restart completed work.
            instructions.append(message)
        elif (isinstance(message, HumanMessage) and isinstance(message.id, str)
              and (message.id.startswith(("omni-command:", "omni-task:"))
                   or message.id == f"omni-stage:{role}"
                   or (clarifications is not None and message.id in {
                       "omni-interaction:preflight", "omni-interaction:final"})
                   or (clarifications is not None and message.id.startswith("omni-interaction:task:")))):
            # Earlier stage instructions already have answers in history.
            # Moving them after those answers makes them look like new work
            # and can elicit think+plan+dispatch together instead of one JSON.
            pending.append(message)
        else:
            history.append(message)
    if not prefix:
        # Stage/task handoffs may carry only a derived instruction. Keep the
        # original saved user objective too, never infer it from old history.
        prefix.append(HumanMessage(id=goal_id, content=goal_content))
    prefix.extend(instructions)
    if clarifications is not None:
        prefix.append(HumanMessage(id=POLICY_ID, content=POLICY))
        prefix.extend(clarifications)
    factory = getattr(host.worker, "agent_model_factory", None)
    executor = DurableSummaryExecutor(host.worker, host.run_id, snapshot, host=host,
        model_factory=(lambda config: factory(role, config)) if factory else None)
    v3 = isinstance(policy, ContextPolicyV3)
    soft_summary = True
    if v3:
        # After one degraded summary for this role, only the hard guard may
        # request another summary; otherwise every turn would pay again.
        async with host.serial:
            with host.worker.transaction() as db:
                soft_summary = not any(event.payload.get("role") == role for event in db.scalars(
                    select(RunEvent).where(RunEvent.run_id == host.run_id,
                                           RunEvent.event_type == "context.summary_degraded")))
    with tracing_context(enabled=False):
        prepared = await prepare_context(ContextInput(
            ContextScope(host.run_id, role, snapshot["id"], model_fingerprint(snapshot), checkpoint_ns, task_id),
            tuple(history), prefix_messages=tuple(prefix), suffix_messages=tuple(pending),
            system_message=system_message, tool_schemas=tuple(tool_schemas), policy=policy, model_limits=limits,
            main_output_tokens=snapshot.get("max_output_tokens", 100_000),
            native_tool_messages=tuple(message for message in messages
                                       if isinstance(message, AIMessage) and message.tool_calls),
            soft_summary=soft_summary,
        ), model=model, summary_executor=executor)
    if v3:
        await record_context_decisions(host, role, prepared)
    return prepared


_MAX_EVENT_IDS = 50


async def record_context_decisions(host, role: str, prepared) -> None:
    """Persist bounded, ID-only evidence of v3 masking and degradation.

    Masked stubs replace older tool results in graph state, exactly like an
    applied summary does; ToolOperation rows and artifacts stay unchanged.
    An already-masked stub is never masked again, so each event lists only
    the newly masked results of that turn.
    """
    degraded = [item.split(":", 1)[1] for item in prepared.decisions if item.startswith("summary_degraded:")]
    masked = list(prepared.masked_message_ids)
    if not degraded and not masked:
        return
    async with host.serial:
        with host.worker.transaction() as db:
            run = db.scalar(select(Run).where(Run.id == host.run_id).with_for_update())
            if run is None:
                raise AgentBoundaryError("Run is unavailable")
            if masked:
                add_event(db, run, "context.tool_results_masked", {
                    "role": role, "masked_count": len(masked), "message_ids": masked[-_MAX_EVENT_IDS:],
                    "estimated_tokens_saved": prepared.masked_tokens_saved,
                    "counting_method": prepared.counting_method, "original_records_unchanged": True,
                })
            for code in degraded:
                add_event(db, run, "context.summary_degraded", {
                    "role": role, "code": code, "input_tokens": prepared.input_tokens,
                    "input_limit": prepared.input_limit, "counting_method": prepared.counting_method,
                    "original_records_unchanged": True,
                })
