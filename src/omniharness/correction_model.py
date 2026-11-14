"""One reserved correction model invocation. Never dispatches tools.

This is an isolated host component. The caller must have committed the unique
v10 correction reservation, retain its exact in-process drain, and supply the
original graph/executor/saver. Failures and uncertain commits cannot be retried.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from sqlalchemy import select, text

from .context_policy import context_budget, counter_for_policy, parse_context_configuration
from .correction_batch import InvalidCorrectionBatch, validate_correction_batch
from .correction_reservation import (
    CorrectionReservationUnavailable,
    correction_intent_mac,
    correction_invocation_id,
    remaining_active_budget,
    validate_execute_host,
)
from .m4_tools import tool_definitions
from .model_output import require_complete_output
from .model_usage import model_binding, normalize_usage
from .models import (
    Message,
    OperationRecord,
    Run,
    RunBudgetReservation,
    RunCommand,
    ToolOperation,
    Workspace,
    WorkspaceLease,
    WorkspacePolicy,
    now,
)
from .services import add_event, operation_id
from .v10_closure import pin_checkpoint_watermark
from .v10_drain import get_drained_execution, get_drained_model

_NODE = "v10-correction:model_calls"
_SYSTEM = (
    "你只可为用户确认的未执行动作返回工具调用。仅处理列出的动作，禁止额外调用、重复动作或声称目标已完成。"
    "如无法安全完成，说明原因；不得调用其他工具。"
)


def _request(run, message, role):
    envelope = (run.outcome or {}).get("v10_actions") or {}
    actions = envelope.get("actions") if envelope.get("assessed") is True else None
    if (
        not isinstance(actions, list)
        or not 1 <= len(actions) <= 2
        or any(
            not isinstance(item, dict)
            or item.get("source") != "user"
            or item.get("run_id") != run.id
            or item.get("workspace_id") != run.workspace_id
            or not isinstance(item.get("task_id"), str)
            or not item["task_id"]
            for item in actions
        )
    ):
        raise CorrectionReservationUnavailable("Validated correction actions unavailable")
    from .action_contract import validate_explicit_actions

    contract = run.config_snapshot.get("action_contract") or {}
    request = contract.get("request") or {}
    validated = validate_explicit_actions(
        request.get("requirements"), run_id=run.id, workspace_id=run.workspace_id, require_dependencies=True
    )
    unbound = {
        **envelope,
        "actions": [{key: value for key, value in item.items() if key != "task_id"} for item in actions],
    }
    if (
        validated.get("assessed") is not True
        or validated != contract.get("explicit")
        or unbound != validated
        or len({item["task_id"] for item in actions}) != 1
    ):
        raise CorrectionReservationUnavailable("Frozen action contract or dependencies changed")
    if len(actions) == 1 and validated.get("version") == 1:
        dependencies = {actions[0]["id"]: []}
    else:
        dependencies = validated.get("dependency_graph")
        if not isinstance(dependencies, dict) or set(dependencies) != {item["id"] for item in actions}:
            raise CorrectionReservationUnavailable("Explicit action dependencies unavailable")
    workspace = run.config_snapshot.get("workspace") or {}
    allowed = {
        "workspace_patch" if item["kind"] == "workspace_file_write" else "run_command" for item in actions
    }
    tools = [
        tool
        for tool in tool_definitions(workspace, interactive=False, patch_schema_v2=True)
        if tool.name in allowed
    ]
    if {tool.name for tool in tools} != allowed:
        raise CorrectionReservationUnavailable("Frozen correction tools unavailable")
    body = {
        "original_user_request": message.content,
        "missing_actions": actions,
        "action_dependencies": dependencies,
        "completed_actions": [],
        "do_not_repeat": [],
    }
    system = SystemMessage(content=_SYSTEM)
    human = HumanMessage(content=json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    messages = [system, human]
    policy, limits = parse_context_configuration(role["model"].get("context_policy"))
    schemas = tuple(convert_to_openai_tool(tool) for tool in tools)
    serialized = json.dumps(
        [item.model_dump(mode="json") for item in messages],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    tool_json = json.dumps(schemas, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if (
        len(messages) > policy.max_messages
        or len(serialized) + len(tool_json) > policy.max_characters
        or counter_for_policy(policy).count(messages, tools=schemas)
        > context_budget(policy, limits, output_tokens=role["model"]["max_output_tokens"]).input_limit
    ):
        raise CorrectionReservationUnavailable("Correction context exceeds frozen budget")
    return actions, tools, messages, serialized, dependencies


def _locked_workspace(db, run, settings):
    workspace = run.config_snapshot.get("workspace") or {}
    row = db.scalar(select(Workspace).where(Workspace.id == run.workspace_id).with_for_update())
    lease = db.scalar(
        select(WorkspaceLease).where(WorkspaceLease.workspace_id == run.workspace_id).with_for_update()
    )
    policy = db.scalar(
        select(WorkspacePolicy)
        .where(
            WorkspacePolicy.workspace_id == run.workspace_id,
            WorkspacePolicy.version == workspace.get("policy_version"),
        )
        .with_for_update()
    )
    sandbox = {
        "endpoint": settings.sandbox_endpoint,
        "image": settings.sandbox_image,
        "instance_id": settings.instance_id,
        "snapshot_root": settings.sandbox_snapshot_root,
        "max_command_seconds": settings.sandbox_max_command_seconds,
    }
    if (
        not settings.enable_tools
        or row is None
        or not row.enabled
        or lease is None
        or lease.run_id != run.id
        or policy is None
        or row.policy_version != workspace.get("policy_version")
        or policy.grant != workspace.get("grant")
        or policy.tool_policy != workspace.get("tool_policy")
        or row.host_path != (workspace.get("grant") or {}).get("root")
        or (row.probe or {}).get("identity") != workspace.get("probe_identity")
        or workspace.get("sandbox") != sandbox
        or workspace.get("broker_endpoint") != settings.workspace_broker_url
    ):
        raise CorrectionReservationUnavailable("Workspace authorization changed")
    return workspace, row


def load_recorded_correction_batch(db, run, settings):
    """Recheck the saved native response and return a detached host batch.

    Caller must hold the correct pinned owner transaction; this read-only
    verifier does not create a correction phase, authorize tools or recover a
    dispatched operation. Every user action/dependency is revalidated from
    the frozen contract and the exact response is checked against its MAC.
    """
    from .m4_invalid_audit import _key, _mac
    from .stage_contract import STAGE_ROLE_BINDINGS

    if run.workflow_version != "stage-agent-v10" or not run.workspace_id:
        raise CorrectionReservationUnavailable("Correction batch scope unavailable")
    invocation = correction_invocation_id(run.id)
    identity = operation_id(run.id, _NODE, {"invocation_id": invocation})
    row = db.get(OperationRecord, identity)
    reservation = db.get(RunBudgetReservation, (run.id, invocation))
    data = row.result if row is not None and isinstance(row.result, dict) else {}
    if (
        row is None
        or row.run_id != run.id
        or row.node_id != _NODE
        or row.workflow_version != run.workflow_version
        or row.status != "completed"
        or reservation is None
        or reservation.kind != "model_calls"
        or data.get("binding_fingerprint") != reservation.binding_fingerprint
        or data.get("dispatch_state") != "RECORDED"
        or data.get("batch_status") != "VALIDATED_NOT_DISPATCHED"
    ):
        raise CorrectionReservationUnavailable("Recorded correction response unavailable")
    user = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "user"))
    if user is None or data.get("intent_mac") != correction_intent_mac(settings, run, user):
        raise CorrectionReservationUnavailable("Recorded correction intent changed")
    roles = ((run.config_snapshot or {}).get("agent") or {}).get("roles") or {}
    role = roles.get(STAGE_ROLE_BINDINGS["execute"])
    if not isinstance(role, dict):
        raise CorrectionReservationUnavailable("Frozen execute role unavailable")
    actions, _, _, serialized, dependencies = _request(run, user, role)
    request_digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    raw = (data.get("response") or {}).get("message")
    signature = data.get("response_mac")
    if (
        not isinstance(raw, dict)
        or data.get("request_sha256") != request_digest
        or not isinstance(signature, str)
    ):
        raise CorrectionReservationUnavailable("Recorded response binding unavailable")
    expected = _mac(
        _key(settings, "stage-agent-v10"),
        "v10-correction-response",
        {"run_id": run.id, "operation_id": identity, "request_sha256": request_digest, "response": raw},
    )
    if not hmac.compare_digest(signature, expected):
        raise CorrectionReservationUnavailable("Recorded response authentication failed")
    try:
        response = AIMessage.model_validate(raw)
        require_complete_output(response, require_finish=True, allow_tools=True)
        if response.invalid_tool_calls:
            raise InvalidCorrectionBatch("Malformed native calls")
        return validate_correction_batch(
            missing_actions=actions, tool_calls=response.tool_calls, dependency_graph=dependencies
        )
    except Exception:
        raise CorrectionReservationUnavailable("Recorded correction batch is invalid") from None


def _mark_review(worker, run_id, op_id):
    """Best effort on the owner connection; a lost owner leaves DISPATCHED durable."""
    try:
        lock = getattr(worker, "lock", None)
        if lock is None or lock.lost or lock.connection is None:
            return
        with worker.transaction() as db:
            if db.connection() is not lock.connection:
                return
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            op = db.get(OperationRecord, op_id)
            if run is None or op is None or op.run_id != run_id or op.status != "running":
                return
            value = dict(op.result or {})
            value["dispatch_state"] = "UNCERTAIN"
            op.result, op.status, op.error_code, op.finished_at = value, "uncertain", "RESULT_UNKNOWN", now()
            run.status, run.error_code = "pending_review", "CORRECTION_RESULT_UNKNOWN"
            run.error_message = "纠错模型调用状态需要核对；不会自动重发。"
            assistant = db.scalar(
                select(Message).where(Message.run_id == run_id, Message.role == "assistant")
            )
            if assistant is not None:
                assistant.status = "pending_review"
            add_event(db, run, "run.pending_review", {"code": run.error_code, "message": run.error_message})
    except Exception:
        pass


async def dispatch_reserved_correction(worker, run_id, *, ticket, root_id, graph, executor, checkpointer):
    """Consume the unique reservation once and record the model response only.

    This never invokes a workspace tool. It does not accept a caller-supplied
    model, closure boolean, model response or model-generated dependency graph.
    Two-action dependencies must be explicit in the frozen user contract;
    missing or ambiguous dependency information prevents the model request.
    """
    invocation = correction_invocation_id(run_id)
    op_id = operation_id(run_id, _NODE, {"invocation_id": invocation})
    try:
        proof = get_drained_execution(
            worker, run_id, ticket=ticket, graph=graph, executor=executor, saver=checkpointer
        )
        model = get_drained_model(
            worker, run_id, ticket=ticket, graph=graph, executor=executor, saver=checkpointer
        )
        listener = getattr(worker, "_v10_owner_listener", None)
        from .v10_owner_runtime import V10OwnerListener

        if (
            not isinstance(listener, V10OwnerListener)
            or listener.worker is not worker
            or not await listener._live()
        ):
            raise CorrectionReservationUnavailable("Owner listener unavailable")
        with worker.transaction() as db:
            if db.connection() is not worker.lock.connection:
                raise CorrectionReservationUnavailable("Owner connection changed")
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            pin_checkpoint_watermark(db, ticket, root_id)
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            reservation = db.get(RunBudgetReservation, (run_id, invocation))
            op = db.scalar(
                select(OperationRecord).where(OperationRecord.operation_id == op_id).with_for_update()
            )
            if (
                not run
                or run.status != "running"
                or not op
                or op.run_id != run_id
                or op.node_id != _NODE
                or op.workflow_version != "stage-agent-v10"
                or op.status != "running"
                or not reservation
                or reservation.kind != "model_calls"
                or not isinstance(op.result, dict)
                or op.result.get("binding_fingerprint") != reservation.binding_fingerprint
                or op.result.get("purpose") != "v10_correction"
            ):
                raise CorrectionReservationUnavailable("Correction reservation unavailable")
            if op.result.get("dispatch_state") != "RESERVED":
                raise CorrectionReservationUnavailable("Correction model dispatch already claimed")
            if db.scalar(
                select(RunCommand.id)
                .where(RunCommand.run_id == run_id, RunCommand.status == "pending")
                .limit(1)
            ) or db.scalar(select(ToolOperation.id).where(ToolOperation.run_id == run_id).limit(1)):
                raise CorrectionReservationUnavailable("Run changed after correction reservation")
            workspace, row = _locked_workspace(db, run, worker.settings)
            role = validate_execute_host(db, run, proof)
            original = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
            if original is None:
                raise CorrectionReservationUnavailable("Original user request unavailable")
            if op.result.get("intent_mac") != correction_intent_mac(worker.settings, run, original):
                raise CorrectionReservationUnavailable("Frozen correction intent changed")
            actions, tools, messages, serialized, dependencies = _request(run, original, role)
            remaining = remaining_active_budget(db, run_id, proof, len(actions), model_already_reserved=True)
            timeout = min(remaining, role["model"]["timeout_seconds"])
            if timeout <= 0:
                raise CorrectionReservationUnavailable("Correction active budget exhausted")
            # Probe under current workspace/lease locks immediately before the
            # durable dispatch claim. The real gateway will recheck again.
            from .workspace_client import BrokerClient
            from .workspace_io import DEFAULT_EXCLUDES

            broker = getattr(worker, "workspace_broker", None) or BrokerClient(worker.settings)
            probe = await asyncio.wait_for(
                broker.request(run.workspace_id, row.policy_version, "probe"), timeout=2
            )
            if (
                not isinstance(probe, dict)
                or probe.get("identity") != workspace.get("probe_identity")
                or probe.get("workspace_id") != run.workspace_id
                or probe.get("policy_version") != row.policy_version
                or probe.get("root") != row.host_path
                or list(probe.get("read_allow", ())) != list(workspace["grant"]["read_allow"])
                or list(probe.get("write_allow", ())) != list(workspace["grant"]["write_allow"])
                or list(probe.get("excludes", ())) != [*DEFAULT_EXCLUDES, *workspace["grant"]["excludes"]]
            ):
                raise CorrectionReservationUnavailable("Workspace probe changed")
            if not listener.alive or worker.lock.lost:
                raise CorrectionReservationUnavailable("Owner presence changed")
            remaining = remaining_active_budget(db, run_id, proof, len(actions), model_already_reserved=True)
            timeout = min(remaining, role["model"]["timeout_seconds"])
            if timeout <= 0:
                raise CorrectionReservationUnavailable("Correction active budget exhausted")
            bound_model = model.bind_tools(tools)
            request_digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            op.result = {
                **op.result,
                "dispatch_state": "DISPATCHED",
                "request_sha256": request_digest,
                "model_binding": model_binding(role["model"]),
            }
        # A disconnected listener after commit makes the call uncertain and
        # forbids dispatch; the durable claim is never retried.
        if not await listener._live():
            raise CorrectionReservationUnavailable("Owner listener lost after claim")
        # Recheck controls and remaining active time immediately before sending.
        with worker.transaction() as db:
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            pin_checkpoint_watermark(db, ticket, root_id)
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            op = db.scalar(
                select(OperationRecord).where(OperationRecord.operation_id == op_id).with_for_update()
            )
            if (
                run is None
                or run.status != "running"
                or op is None
                or op.status != "running"
                or (op.result or {}).get("dispatch_state") != "DISPATCHED"
                or db.scalar(
                    select(RunCommand.id)
                    .where(RunCommand.run_id == run_id, RunCommand.status == "pending")
                    .limit(1)
                )
            ):
                raise CorrectionReservationUnavailable("Correction controls changed after claim")
            _locked_workspace(db, run, worker.settings)
            role = validate_execute_host(db, run, proof)
            user = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
            if (op.result or {}).get("intent_mac") != correction_intent_mac(worker.settings, run, user):
                raise CorrectionReservationUnavailable("Correction intent changed after claim")
            remaining = remaining_active_budget(db, run_id, proof, len(actions), model_already_reserved=True)
            timeout = min(remaining, role["model"]["timeout_seconds"])
            if timeout <= 0:
                raise CorrectionReservationUnavailable("Correction active budget exhausted")
        response = await asyncio.wait_for(bound_model.ainvoke(messages), timeout=timeout)
        if not isinstance(response, AIMessage):
            raise CorrectionReservationUnavailable("Native model response unavailable")
        raw = response.model_dump(mode="json")
        encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > 4 * 1024 * 1024:
            raise CorrectionReservationUnavailable("Native model response exceeds limit")
        try:
            require_complete_output(response, require_finish=True, allow_tools=True)
            if response.invalid_tool_calls:
                raise InvalidCorrectionBatch("Malformed native calls")
            validate_correction_batch(
                missing_actions=actions,
                tool_calls=response.tool_calls,
                dependency_graph=dependencies,
            )
            batch_status = "VALIDATED_NOT_DISPATCHED"
        except Exception:
            batch_status = "UNVERIFIED"
        with worker.transaction() as db:
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            op = db.scalar(
                select(OperationRecord).where(OperationRecord.operation_id == op_id).with_for_update()
            )
            if (
                not run
                or not op
                or op.status != "running"
                or not isinstance(op.result, dict)
                or op.result.get("dispatch_state") != "DISPATCHED"
                or op.result.get("request_sha256") != request_digest
            ):
                raise CorrectionReservationUnavailable("Correction response claim changed")
            from .m4_invalid_audit import _key, _mac

            response_mac = _mac(
                _key(worker.settings, "stage-agent-v10"),
                "v10-correction-response",
                {"run_id": run_id, "operation_id": op_id, "request_sha256": request_digest, "response": raw},
            )
            op.result = {
                **op.result,
                "dispatch_state": "RECORDED",
                "response": {"message": raw},
                "response_mac": response_mac,
                "batch_status": batch_status,
                "usage": normalize_usage(response.usage_metadata),
                "cost": None,
            }
            op.status, op.finished_at = "completed", now()
        return {"status": "RECORDED", "batch_status": batch_status, "operation_id": op_id, "cost": None}
    except asyncio.CancelledError:
        _mark_review(worker, run_id, op_id)
        raise
    except Exception:
        _mark_review(worker, run_id, op_id)
        return {"status": "PENDING_REVIEW", "operation_id": op_id, "cost": None}
