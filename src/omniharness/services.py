import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, func, literal_column, select, text, true, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import aliased

from .models import (
    AgentRun,
    Conversation,
    Message,
    ModelConfig,
    OperationRecord,
    Provider,
    RoleBinding,
    Run,
    RunBudget,
    RunCommand,
    RunEvent,
    StageBindingSet,
    Workspace,
    now,
)
from .schemas import (
    ModelConfigRequest,
    RoleBindingsRequest,
    RunCommandRequest,
    SendMessageRequest,
    StageBindingsRequest,
)

TERMINAL = {"completed", "cancelled", "failed", "interrupted"}
ACTIVE_STATES = {"running", "pause_requested", "paused", "pending_review", "cancel_requested", "awaiting_approval", "awaiting_user"}
OPEN_FOR_REQUIREMENT = {"queued", "running", "pause_requested", "paused", "awaiting_approval", "awaiting_user"}
# Agent execution remains opt-in and requires PostgreSQL plus a verified
# checkpoint schema. This gate records that the runtime is wired, not that
# every provider or deployment environment has been certified.
AGENT_RUNTIME_WIRED = True


def api_value(value):
    # SQLite drops tzinfo; database timestamps are always UTC. Never send an
    # ambiguous timestamp that browsers could interpret in their local zone.
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return value


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def require(db, model, identity):
    row = db.get(model, identity)
    if row is None:
        raise ServiceError("NOT_FOUND", "请求的记录不存在。", 404)
    return row


def model_output(db, model):
    p = require(db, Provider, model.provider_id)
    return {
        "id": model.id, "name": model.name, "adapter_type": p.adapter_type,
        "model_name": model.model_name, "base_url": p.base_url,
        "enabled": p.enabled, "key_configured": bool(p.encrypted_api_key),
        "version": model.version, "provider_version": p.version, "parameters": model.parameters,
        "timeout_seconds": model.timeout_seconds, "max_output_tokens": model.max_output_tokens,
        "context_policy": model.context_policy,
        "capabilities": model.capabilities, "tested_at": api_value(model.tested_at), "created_at": api_value(model.created_at),
    }


def save_model(db, body: ModelConfigRequest, secrets, settings, identity=None):
    from .providers import ProviderError, RuntimeModelConfig, validate_config

    try:
        validate_config(RuntimeModelConfig(
            id="validation-only", adapter_type=body.adapter_type, model_name=body.model_name,
            api_key="validation-only", base_url=body.base_url, parameters=body.parameters,
            timeout_seconds=body.timeout_seconds, max_output_tokens=body.max_output_tokens,
        ))
    except ProviderError as exc:
        raise ServiceError(exc.code, exc.public_message, 422) from None
    if body.adapter_type == "mock" and not settings.enable_demo:
        raise ServiceError("DEMO_DISABLED", "当前实例未启用模拟模式。")
    if identity:
        model = db.scalar(select(ModelConfig).where(ModelConfig.id == identity).with_for_update())
        if model is None:
            raise ServiceError("NOT_FOUND", "模型配置不存在。", 404)
        provider = db.scalar(select(Provider).where(Provider.id == model.provider_id).with_for_update())
        model.version += 1
        provider.version += 1
    else:
        provider = Provider()
        db.add(provider)
        provider.adapter_type = body.adapter_type
        db.flush()
        model = ModelConfig(provider_id=provider.id)
        db.add(model)
    provider.adapter_type = body.adapter_type
    provider.base_url = body.base_url
    provider.enabled = body.enabled
    if "api_key" in body.model_fields_set:
        provider.encrypted_api_key = secrets.encrypt(body.api_key.get_secret_value() if body.api_key else None)
    model.name, model.model_name = body.name, body.model_name
    model.parameters = body.parameters
    model.timeout_seconds, model.max_output_tokens = body.timeout_seconds, body.max_output_tokens
    if not identity or "context_policy" in body.model_fields_set:
        model.context_policy = deepcopy(body.context_policy)
    model.capabilities = {}
    model.tested_at = None
    db.flush()
    return model_output(db, model)


def snapshot_model(db, identity):
    model = require(db, ModelConfig, identity)
    provider = require(db, Provider, model.provider_id)
    if not provider.enabled:
        raise ServiceError("MODEL_DISABLED", "此模型配置已停用。")
    if provider.adapter_type in {"openai", "anthropic", "gemini"} and not provider.encrypted_api_key:
        raise ServiceError("MISSING_API_KEY", "此模型尚未配置 API Key，请在模型设置中添加。")
    snapshot = {
        "id": model.id, "adapter_type": provider.adapter_type, "model_name": model.model_name,
        "base_url": provider.base_url, "encrypted_api_key": provider.encrypted_api_key,
        "parameters": model.parameters, "timeout_seconds": model.timeout_seconds,
        "max_output_tokens": model.max_output_tokens, "version": model.version,
        "context_policy": deepcopy(model.context_policy or {}),
        "provider_version": provider.version,
    }
    snapshot["capabilities"] = frozen_model_capabilities(snapshot, model.capabilities)
    return snapshot


def frozen_model_capabilities(snapshot, capabilities):
    """Carry only evaluated protocol capabilities bound to this model snapshot."""
    from .providers import (
        NATIVE_AGENT_STREAM,
        freeze_json_mode_capability,
        freeze_summary_capability,
        summary_capability_binding,
    )

    capabilities = capabilities if isinstance(capabilities, dict) else {}
    result = {}
    if isinstance(capabilities.get("tools"), dict):
        result["tools"] = {"status": capabilities["tools"].get("status")}
        tools = capabilities["tools"]
        if (snapshot.get("adapter_type") == "openai-compatible" and tools.get("status") == "supported"
                and tools.get("transport") == NATIVE_AGENT_STREAM
                and tools.get("transport_binding") == summary_capability_binding(snapshot)):
            result["tools"].update(transport=NATIVE_AGENT_STREAM, transport_binding=tools["transport_binding"])
    summary = freeze_summary_capability(snapshot, capabilities.get("summary_non_thinking"))
    if summary:
        result["summary_non_thinking"] = summary
    json_mode = freeze_json_mode_capability(snapshot, capabilities.get("json_mode"))
    if json_mode is not None:
        result["json_mode"] = json_mode
    return result


def runtime_config(snapshot, secrets):
    from .providers import RuntimeModelConfig

    return RuntimeModelConfig(
        id=snapshot["id"], adapter_type=snapshot["adapter_type"], model_name=snapshot["model_name"],
        api_key=secrets.decrypt(snapshot.get("encrypted_api_key")), base_url=snapshot.get("base_url"),
        parameters=snapshot.get("parameters", {}), timeout_seconds=snapshot.get("timeout_seconds", 90),
        max_output_tokens=snapshot.get("max_output_tokens", 2048),
    )


def add_event(db, run, event_type, payload):
    row = RunEvent(
        run_id=run.id, sequence_number=run.next_event_sequence, event_type=event_type,
        payload={"mode": run.mode, **payload},
    )
    run.next_event_sequence += 1
    db.add(row)
    db.flush()
    return row


def event_output(row):
    return {
        "schema_version": 1, "event_id": row.id, "run_id": row.run_id,
        "sequence_number": row.sequence_number, "event_type": row.event_type,
        "created_at": api_value(row.created_at).isoformat(), "payload": row.payload,
    }


def conversation_output(row):
    return {k: api_value(getattr(row, k)) for k in ("id", "title", "created_at", "updated_at")}


def message_output(row, run=None):
    output = {k: api_value(getattr(row, k)) for k in (
        "id", "conversation_id", "run_id", "role", "content", "reasoning_content",
        "output_metadata", "status", "created_at",
    )}
    # Errors belong to the individual assistant message, including historical
    # failures after the user has moved on to a later Run.
    output["error_code"] = run.error_code if run is not None and row.role == "assistant" else None
    output["error_message"] = run.error_message if run is not None and row.role == "assistant" else None
    snapshot = run.config_snapshot if run is not None and isinstance(run.config_snapshot, dict) else {}
    output["history_scope"] = deepcopy(snapshot.get("history_scope"))
    output["workspace_id"] = run.workspace_id if run is not None else None
    output["run_status"] = run.status if run is not None else None
    return output


def message_is_context(row):
    """Only complete answers (and untouched legacy answers) enter chat history."""
    return row.role == "user" or (
        row.status == "completed"
        and (row.output_metadata or {}).get("output_status") in (None, "complete")
    )


def settle_message_output(message, status):
    """Persist terminal integrity without overwriting a more precise diagnosis."""
    metadata = dict(message.output_metadata or {})
    prior = metadata.get("output_status")
    if status == "completed":
        metadata.setdefault("output_status", "complete")
    elif status in {"cancelled", "interrupted", "paused", "pending_review", "failed"}:
        if prior in (None, "complete", "streaming", "paused"):
            metadata["output_status"] = {
                "cancelled": "cancelled", "interrupted": "interrupted", "paused": "paused",
                "pending_review": "unverified", "failed": "error",
            }[status]
    message.output_metadata = metadata
    return metadata


def agent_run_output(row, run=None):
    """Project only actual persisted AgentRun metadata, never runtime configuration."""
    output = {k: api_value(getattr(row, k)) for k in (
        "id", "run_id", "parent_agent_run_id", "role", "status", "depth", "model_config_id",
        "error_code", "created_at", "started_at", "finished_at",
    )}
    output["node_kind"] = "model_call"
    if run is not None:
        snapshot = run.config_snapshot if isinstance(run.config_snapshot, dict) else {}
        agent = snapshot.get("agent") or {}
        roles = agent.get("roles") or {}
        legacy_roles = {"coordinator": "think", "researcher": "plan", "coder": "execute", "reviewer": "review"}
        role = legacy_roles.get(row.role, row.role) if run.workflow_version == "deep-agents-v2" else row.role
        frozen = (roles.get(role) or {}).get("model") or {}
        if frozen.get("id") == row.model_config_id:
            output["model_name"] = frozen.get("model_name")
            output["selection_source"] = (roles.get(role) or {}).get("source")
    output.setdefault("model_name", None)
    output.setdefault("selection_source", None)
    return output


def run_output(run):
    from .providers import NATIVE_AGENT_STREAM, agent_streaming_enabled
    output = {k: api_value(getattr(run, k)) for k in (
        "id", "conversation_id", "model_config_id", "model_label", "status", "mode",
        "error_code", "error_message", "usage", "created_at", "started_at", "finished_at",
    )}
    snapshot = run.config_snapshot if isinstance(run.config_snapshot, dict) else {}
    output["workflow_type"] = "agent" if snapshot.get("workflow_type") == "agent" else "chat"
    output["workflow_version"] = run.workflow_version
    roles = (snapshot.get("agent") or {}).get("roles") or {}
    if run.workflow_version in {"stage-agent-v1", "stage-agent-v2", "stage-agent-v3", "stage-agent-v4", "stage-agent-v5", "stage-agent-v6", "stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10", "stage-agent-interactive-v1"}:
        output["stage_plan"] = [{"stage": stage, "model_config_id": (roles.get(stage, {}).get("model") or {}).get("id"),
                                 "model_name": (roles.get(stage, {}).get("model") or {}).get("model_name"),
                                 "source": roles.get(stage, {}).get("source")}
                                for stage in ("think", "plan", "dispatch", "execute", "review")]
    else:
        output["stage_plan"] = None
    output["workspace_id"] = run.workspace_id
    workspace = snapshot.get("workspace") or {}
    output["workspace"] = ({key: workspace.get(key) for key in ("id", "name", "policy_version", "tool_policy")}
                           if workspace else None)
    output["agent_strategy"] = (snapshot.get("agent") or {}).get("strategy", "staged")
    strategy = output["agent_strategy"]
    output["active_stages"] = (["execute"] if strategy == "single" else
                               ["plan", "execute", "review"] if strategy == "compact" else
                               ["think", "plan", "dispatch", "execute", "review"])
    output["execution_config"] = {
        "workflow_version": run.workflow_version,
        "budget": (snapshot.get("agent") or {}).get("budget"),
        "review_failure_policy": (snapshot.get("agent") or {}).get("review_failure_policy", "fail"),
        "review_revision_policy": (snapshot.get("agent") or {}).get("review_revision_policy", "off"),
        "models": {role: {**{key: (value.get("model") or {}).get(key) for key in (
            "id", "model_name", "version", "max_output_tokens", "context_policy",
        )}, "transport": NATIVE_AGENT_STREAM if agent_streaming_enabled(
            value.get("model") or {}, run.workflow_version) else "adapter-default"}
            for role, value in roles.items()},
        "sandbox": workspace.get("sandbox"),
    }
    outcome = run.outcome if isinstance(run.outcome, dict) else {}
    output["review_status"] = outcome.get("review_status")
    if run.workflow_version == "stage-agent-v10":
        contract = snapshot.get("action_contract") or {}
        output["action_contract"] = {
            "version": contract.get("version", 1),
            "explicit": deepcopy(contract.get("explicit")),
            "auto_correction_enabled": contract.get("auto_correction_enabled", False),
            "revisions": deepcopy(contract.get("revisions", [])),
        }
        outcome = run.outcome if isinstance(run.outcome, dict) else {}
        action_envelope = outcome.get("v10_actions")
        actions = action_envelope.get("actions") if isinstance(action_envelope, dict) else None
        output["action_candidates"] = [
            {key: deepcopy(action[key]) for key in ("id", "kind", "source", "path", "argv", "cwd", "step_id")
             if key in action}
            for action in actions if isinstance(action, dict)
            and action.get("run_id") == run.id and action.get("workspace_id") == run.workspace_id
        ] if isinstance(actions, list) else []
        output["action_outcome"] = deepcopy(outcome.get("v10_action_outcome"))
    return output


def enqueue(db, conversation_id: str, body: SendMessageRequest, settings=None, *, owner_challenge=None):
    from .history_scope import normalize_history_scope

    # Explicit agent requests must not become paid plain-chat runs. The M3
    # checkpoint and shared-agent execution paths are not yet product-wired.
    if body.workflow_type == "agent" and (not AGENT_RUNTIME_WIRED or settings is None or not settings.enable_agent):
        raise ServiceError("AGENT_RUNTIME_UNAVAILABLE", "Agent 执行尚未接入，请使用普通聊天。", 503)
    if body.workflow_type == "agent" and db.get_bind().dialect.name != "postgresql":
        raise ServiceError("AGENT_REQUIRES_POSTGRES", "Agent 持久运行需要 PostgreSQL。", 503)
    conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
    if conversation is None:
        raise ServiceError("NOT_FOUND", "对话不存在。", 404)
    requested_scope = normalize_history_scope(body)
    existing = db.scalar(select(Run).where(
        Run.conversation_id == conversation_id, Run.client_request_id == body.client_request_id
    ))
    if existing:
        message = db.scalar(select(Message).where(Message.run_id == existing.id, Message.role == "user"))
        existing_workflow = existing.config_snapshot.get("workflow_type", "chat")
        requested_overrides = ([item.model_dump() for item in sorted(body.stage_overrides, key=lambda item: item.stage)]
                               if body.stage_overrides is not None else None)
        prior_overrides = (existing.config_snapshot.get("agent") or {}).get("override_request")
        action_request = (existing.config_snapshot.get("action_contract") or {}).get("request")
        requested_actions = ([item.model_dump(exclude_none=True) for item in body.action_requirements]
                             if body.action_requirements is not None else None)
        if (message.content != body.content or existing.model_config_id != body.model_config_id
                or existing_workflow != body.workflow_type or requested_overrides != prior_overrides
                or existing.workspace_id != body.workspace_id
                or (existing.config_snapshot.get("history_scope") != requested_scope
                    if existing.config_snapshot.get("history_scope") is not None
                    else body.history_scope is not None)
                or (existing.config_snapshot.get("agent") or {}).get("strategy", "staged") != body.agent_strategy
                or (action_request != {"requirements": requested_actions,
                                       "auto_correction_enabled": body.auto_correction_enabled}
                    if existing.workflow_version == "stage-agent-v10" else
                    body.action_requirements is not None or body.auto_correction_enabled is not None)):
            raise ServiceError("REQUEST_ID_CONFLICT", "此请求 ID 已被不同内容使用。", 409)
        return {"conversation_id": conversation_id, "run_id": existing.id, "message_id": message.id}
    if requested_scope["kind"] == "workspace":
        workspace = db.scalar(select(Workspace).where(Workspace.id == requested_scope["workspace_id"]).with_for_update())
        if workspace is None or not workspace.enabled:
            raise ServiceError("HISTORY_SCOPE_INVALID", "历史范围中的项目不可用。", 422)
    source_ids = requested_scope["additional_run_ids"]
    if source_ids:
        sources = db.scalars(select(Run).where(Run.id.in_(source_ids), Run.conversation_id == conversation_id,
                                                Run.ordinal < conversation.next_ordinal,
                                                Run.status.in_(TERMINAL)).with_for_update()).all()
        if {source.id for source in sources} != set(source_ids):
            raise ServiceError("HISTORY_SOURCE_INVALID", "所选历史来源不属于当前会话或尚未结束。", 422)
    snapshot = snapshot_model(db, body.model_config_id)
    model = require(db, ModelConfig, body.model_config_id)
    run = Run(
        conversation_id=conversation_id, model_config_id=model.id,
        model_label=f"{model.name} · {model.model_name}", ordinal=conversation.next_ordinal,
        client_request_id=body.client_request_id, config_snapshot=snapshot,
        mode="mock" if snapshot["adapter_type"] == "mock" else "live", status="queued",
    )
    conversation.next_ordinal += 1
    conversation.updated_at = now()
    if conversation.title == "新对话":
        conversation.title = body.content.strip()[:50]
    db.add(run)
    db.flush()
    if body.workflow_type == "agent":
        run.config_snapshot = {**snapshot, "workflow_type": "agent"}
        if body.stage_overrides is None:
            freeze_stage_run_config(db, run, settings, strategy=body.agent_strategy)
        else:
            freeze_stage_run_config(db, run, settings, stage_overrides=body.stage_overrides, strategy=body.agent_strategy)
        if body.workspace_id:
            from .m4_services import freeze_workspace
            run.config_snapshot = {**run.config_snapshot, "agent": {
                **run.config_snapshot["agent"], "strategy": body.agent_strategy,
            }}
            freeze_workspace(db, run, settings, body.workspace_id)
            run.strategy_id = body.agent_strategy
    if run.workflow_version == "stage-agent-v10":
        from .action_contract import validate_explicit_actions
        requested_actions = ([item.model_dump(exclude_none=True) for item in body.action_requirements]
                             if body.action_requirements is not None else None)
        envelope = validate_explicit_actions(requested_actions, run_id=run.id, workspace_id=run.workspace_id,
                                             require_dependencies=True)
        if requested_actions and not envelope["assessed"]:
            raise ServiceError("ACTION_REQUIREMENTS_INVALID", "动作要求无法校验。", 422)
        run.config_snapshot = {**run.config_snapshot, "action_contract": {
            "request": {"requirements": requested_actions, "auto_correction_enabled": body.auto_correction_enabled},
            "explicit": envelope, "auto_correction_enabled": body.auto_correction_enabled is not False,
            "version": 1, "revisions": [],
        }}
        if settings is not None and settings.v10_preview and db.get_bind().dialect.name == "postgresql":
            from .v10_sealed_saver import SEALED_PLUGIN_VERSIONS, create_seal_origin
            try:
                seal_origin = create_seal_origin(settings, run.id, run.workspace_id)
            except Exception:
                raise ServiceError("CHECKPOINT_SEAL_UNAVAILABLE", "检查点封存不可用，无法创建此任务。", 503) from None
            snapshot = dict(run.config_snapshot)
            snapshot["agent"] = {**snapshot["agent"], "plugin_versions": deepcopy(SEALED_PLUGIN_VERSIONS)}
            snapshot["v10_checkpoint_seal_origin"] = seal_origin
            run.config_snapshot = snapshot
    elif body.action_requirements is not None or body.auto_correction_enabled is not None:
        raise ServiceError("ACTION_CONTRACT_UNAVAILABLE", "所选工作流不支持动作要求。", 422)
    run.config_snapshot = {**run.config_snapshot, "history_scope": deepcopy(requested_scope)}
    user_message = Message(conversation_id=conversation_id, run_id=run.id, role="user", content=body.content)
    assistant_message = Message(conversation_id=conversation_id, run_id=run.id, role="assistant", status="queued")
    db.add_all([user_message, assistant_message])
    db.flush()
    add_event(db, run, "run.created", {"message_id": assistant_message.id})
    if (run.workflow_version == "stage-agent-v10" and settings is not None and settings.v10_preview
            and owner_challenge is not None and db.get_bind().dialect.name == "postgresql"):
        # Qualification is best effort and must not block an ordinary Run. The
        # origin and notification commit with this new Run, never on an
        # idempotent existing-request branch above.
        try:
            with db.begin_nested():
                from .v10_owner_auth import ORIGIN_FIELD, make_run_origin
                from .v10_owner_runtime import _CREATED_CHANNEL
                origin = make_run_origin(settings, owner_challenge, run, user_message)
                run.config_snapshot = {**run.config_snapshot, ORIGIN_FIELD: origin}
                db.flush()
                db.execute(text("SELECT pg_notify(:channel, :payload)"),
                           {"channel": _CREATED_CHANNEL, "payload": json.dumps(origin, separators=(",", ":"))})
        except Exception:
            # No qualification may be inferred from failure; no secrets or
            # provider text are recorded in an event or ordinary log.
            pass
    return {"conversation_id": conversation_id, "run_id": run.id, "message_id": user_message.id}


def update_action_requirements(db, identity, body):
    """Revise only before dispatch, under the same Run lock used by workers."""
    from .action_contract import validate_explicit_actions
    from .models import ToolOperation

    run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
    if run is None:
        raise ServiceError("NOT_FOUND", "任务不存在。", 404)
    if run.workflow_version != "stage-agent-v10":
        raise ServiceError("ACTION_CONTRACT_UNAVAILABLE", "所选工作流不支持动作要求。", 422)
    if run.status != "queued" or run.started_at is not None:
        raise ServiceError("ACTION_CONTRACT_LOCKED", "任务已启动，请通过现有核对流程处理。", 409)
    for model, key in ((AgentRun, AgentRun.id), (ToolOperation, ToolOperation.id),
                       (OperationRecord, OperationRecord.operation_id)):
        if db.scalar(select(key).where(model.run_id == run.id).limit(1)) is not None:
            raise ServiceError("ACTION_CONTRACT_LOCKED", "任务已有执行或审批记录，不能修改。", 409)
    snapshot = deepcopy(run.config_snapshot or {})
    contract = snapshot.get("action_contract")
    if not isinstance(contract, dict) or contract.get("version") != body.expected_version:
        raise ServiceError("ACTION_CONTRACT_CONFLICT", "动作要求已变更，请刷新后重试。", 409)
    if body.expected_version >= 8:
        raise ServiceError("ACTION_CONTRACT_REVISION_LIMIT", "动作要求已达到修订上限。", 409)
    raw = [item.model_dump(exclude_none=True) for item in body.requirements]
    envelope = validate_explicit_actions(raw, run_id=run.id, workspace_id=run.workspace_id,
                                         require_dependencies=True)
    if raw and not envelope["assessed"]:
        raise ServiceError("ACTION_REQUIREMENTS_INVALID", "动作要求无法校验。", 422)
    prior = {"version": body.expected_version, "explicit": deepcopy(contract.get("explicit"))}
    contract["revisions"] = [*contract.get("revisions", []), prior]
    contract["version"] = body.expected_version + 1
    contract["explicit"] = envelope
    snapshot["action_contract"] = contract
    run.config_snapshot = snapshot
    db.flush()
    return run_output(run)["action_contract"]


def finish_cancelled(db, run, usage=None):
    """Finish a locked Run once, preserving partial output and recorded usage."""
    if run.status in TERMINAL:
        return
    run.status, run.finished_at = "cancelled", now()
    run.error_code, run.error_message = None, None
    from .clarification_service import cancel_clarifications
    cancel_clarifications(db, run)
    for operation in db.scalars(select(OperationRecord).where(
            OperationRecord.run_id == run.id, OperationRecord.status == "running").with_for_update()):
        operation.status, operation.error_code, operation.finished_at = "uncertain", "RESULT_UNKNOWN", now()
    for node in db.scalars(select(AgentRun).where(
            AgentRun.run_id == run.id, AgentRun.status == "running").with_for_update()):
        node.status, node.error_code, node.finished_at = "pending_review", "RESULT_UNKNOWN", now()
        add_event(db, run, "agent.model_uncertain", {"agent_run_id": node.id, "role": node.role})
    reject_pending_commands(db, run)
    if run.workspace_id:
        from .m4_services import expire_approvals, release_workspace_lease
        expire_approvals(db, run.id, status="cancelled")
        release_workspace_lease(db, run.id)
    if usage is not None:
        run.usage = usage
    assistant = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "assistant"))
    if assistant:
        assistant.status = "cancelled"
        settle_message_output(assistant, "cancelled")
    add_event(db, run, "run.cancelled", {"message_id": assistant.id if assistant else None, "usage": run.usage,
                                         "output_metadata": assistant.output_metadata if assistant else {}})


def cancel_run(db, identity):
    run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
    if run is None:
        raise ServiceError("NOT_FOUND", "任务不存在。", 404)
    if run.status in {"queued", "paused", "pending_review", "awaiting_approval", "awaiting_user"}:
        finish_cancelled(db, run)
    elif run.status in {"running", "pause_requested"}:
        run.status = "cancel_requested"
    return run_output(run)


def command_output(command, run_status):
    return {
        "run_id": command.run_id, "command_id": command.id,
        "sequence_number": command.sequence_number, "command_type": command.command_type,
        "status": command.status, "run_status": run_status,
    }


def submit_command(db, identity: str, body: RunCommandRequest):
    """Record a control intent. No state change happens here; the Worker consumes it."""
    run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
    if run is None:
        raise ServiceError("NOT_FOUND", "任务不存在。", 404)
    existing = db.scalar(select(RunCommand).where(
        RunCommand.run_id == identity, RunCommand.client_request_id == body.client_request_id
    ).with_for_update())
    if existing:
        same = (existing.command_type == body.command_type
                and existing.payload.get("content") == body.content)
        if not same:
            raise ServiceError("REQUEST_ID_CONFLICT", "此命令 ID 已被不同内容使用。", 409)
        return command_output(existing, run.status)
    if run.status in TERMINAL:
        raise ServiceError("RUN_TERMINAL", "任务已经结束，无法提交新的控制命令。", 409)
    highest = db.scalar(select(func.coalesce(func.max(RunCommand.sequence_number), 0))
                        .where(RunCommand.run_id == identity))
    command = RunCommand(
        run_id=identity, sequence_number=highest + 1, command_type=body.command_type,
        client_request_id=body.client_request_id,
        payload={"content": body.content} if body.content is not None else {},
        status="pending",
    )
    db.add(command)
    db.flush()
    return command_output(command, run.status)


def _apply_one(run, command):
    kind = command.command_type
    if kind == "pause":
        if run.status in {"queued", "awaiting_approval"}:
            run.status = "paused"
            return "applied", None
        if run.status == "running":
            run.status = "pause_requested"
            return "applied", None
        return "rejected", "INAPPLICABLE_STATE"
    if kind == "resume":
        if run.status == "paused":
            # No checkpoint yet: replaying a started run would repeat a paid model call.
            if run.started_at is not None:
                if run.config_snapshot.get("workflow_type") != "agent":
                    return "rejected", "CHECKPOINT_REQUIRED"
                # Agent Worker must verify a compatible saved checkpoint before
                # dispatch. Absence or uncertainty becomes pending_review.
            run.status = "queued"
            return "applied", None
        if run.status == "pause_requested":
            run.status = "running"
            return "applied", None
        return "rejected", "INAPPLICABLE_STATE"
    if run.status in OPEN_FOR_REQUIREMENT:
        return "applied", None
    return "rejected", "INAPPLICABLE_STATE"


def reject_pending_commands(db, run, *, emit_events=True):
    """Reject queued intents under the Run lock before its terminal event.

    The terminal status must already be set on ``run``; this makes each
    command event report the final state. Legacy terminal Runs can have
    unprocessed commands from older versions: settle those without adding
    events after their already-emitted terminal event.
    """
    pending = list(db.scalars(select(RunCommand).where(
        RunCommand.run_id == run.id, RunCommand.status == "pending"
    ).order_by(RunCommand.sequence_number).with_for_update()))
    results = []
    for command in pending:
        command.status = "rejected"
        command.applied_at = now()
        if emit_events:
            payload = {
                "command_id": command.id, "sequence_number": command.sequence_number,
                "command_type": command.command_type, "result": "rejected",
                "reason": "INAPPLICABLE_STATE", "run_status": run.status,
            }
            if command.command_type == "append_requirement":
                payload["content"] = command.payload.get("content")
            add_event(db, run, "run.command", payload)
        results.append(command_output(command, run.status))
    return results


def apply_commands(db, identity: str, *, defer_requirements: bool = False):
    """Consume pending commands in order inside one transaction with the state change."""
    run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
    if run is None:
        return []
    if run.status in TERMINAL:
        return reject_pending_commands(db, run, emit_events=False)
    query = select(RunCommand).where(
        RunCommand.run_id == identity, RunCommand.status == "pending"
    )
    if defer_requirements:
        query = query.where(RunCommand.command_type != "append_requirement")
    pending = list(db.scalars(query.order_by(RunCommand.sequence_number).with_for_update(skip_locked=True)))
    results = []
    for command in pending:
        result, reason = _apply_one(run, command)
        command.status = result
        command.applied_at = now()
        payload = {
            "command_id": command.id, "sequence_number": command.sequence_number,
            "command_type": command.command_type, "result": result,
            "reason": reason, "run_status": run.status,
        }
        if command.command_type == "append_requirement":
            payload["content"] = command.payload.get("content")
        add_event(db, run, "run.command", payload)
        results.append(command_output(command, run.status))
    db.flush()
    return results


# --- Shared atomic run budget (G2) -----------------------------------------

@dataclass(frozen=True)
class BudgetLimits:
    max_model_calls: int = 50
    max_tool_calls: int = 100
    max_revisions: int = 2
    max_active_seconds: int = 1800


BUDGET_COUNTERS = {
    "model_calls": ("model_calls", "max_model_calls"),
    "tool_calls": ("tool_calls", "max_tool_calls"),
    "revisions": ("revisions", "max_revisions"),
}


def budget_limits(settings):
    """Static configurable caps, frozen into the budget row on first use."""
    return BudgetLimits(
        max_model_calls=settings.budget_max_model_calls,
        max_tool_calls=settings.budget_max_tool_calls,
        max_revisions=settings.budget_max_revisions,
        max_active_seconds=settings.budget_max_active_seconds,
    )


def ensure_budget(db, run_id: str, limits: BudgetLimits):
    """Idempotently create the shared budget row, freezing the caps at creation."""
    if db.get(RunBudget, run_id) is not None:
        return
    values = {
        "run_id": run_id,
        "max_model_calls": limits.max_model_calls,
        "max_tool_calls": limits.max_tool_calls,
        "max_revisions": limits.max_revisions,
        "max_active_seconds": limits.max_active_seconds,
        "model_calls": 0, "tool_calls": 0, "revisions": 0, "active_seconds": 0,
        "updated_at": now(),
    }
    dialect_insert = (postgresql_insert if db.get_bind().dialect.name == "postgresql"
                      else sqlite_insert)
    db.execute(dialect_insert(RunBudget.__table__).values(**values).on_conflict_do_nothing())


def reserve_budget(db, run_id: str, kind: str, limits: BudgetLimits) -> bool:
    """Atomically reserve one unit of a shared counter.

    A single conditional UPDATE is the atomic point: the database re-checks
    ``counter < cap`` under the write lock, so main agent, subagents and other
    connection pools sharing this run_id can never overshoot. Returns True only
    for the caller that actually consumed a unit.
    """
    if kind not in BUDGET_COUNTERS:
        raise ValueError(f"unknown budget counter: {kind}")
    ensure_budget(db, run_id, limits)
    counter, cap = BUDGET_COUNTERS[kind]
    column, ceiling = getattr(RunBudget, counter), getattr(RunBudget, cap)
    result = db.execute(
        update(RunBudget)
        .where(RunBudget.run_id == run_id, column < ceiling)
        .values(**{counter: literal_column(counter) + 1, "updated_at": now()})
    )
    return result.rowcount == 1


def budget_output(db, run_id: str, limits: BudgetLimits):
    """Read-only snapshot. Reading never creates or mutates a budget row."""
    row = db.get(RunBudget, run_id)
    if row is None:
        state = {
            "max_model_calls": limits.max_model_calls, "model_calls": 0,
            "max_tool_calls": limits.max_tool_calls, "tool_calls": 0,
            "max_revisions": limits.max_revisions, "revisions": 0,
            "max_active_seconds": limits.max_active_seconds, "active_seconds": 0,
        }
        initialized, updated_at = False, None
    else:
        state = {
            "max_model_calls": row.max_model_calls, "model_calls": row.model_calls,
            "max_tool_calls": row.max_tool_calls, "tool_calls": row.tool_calls,
            "max_revisions": row.max_revisions, "revisions": row.revisions,
            "max_active_seconds": row.max_active_seconds, "active_seconds": row.active_seconds,
        }
        initialized = True
        updated_at = api_value(row.updated_at).isoformat() if row.updated_at else None
    return {
        "run_id": run_id, "initialized": initialized, **state,
        "budget_remaining": {
            "model_calls": max(0, state["max_model_calls"] - state["model_calls"]),
            "tool_calls": max(0, state["max_tool_calls"] - state["tool_calls"]),
            "revisions": max(0, state["max_revisions"] - state["revisions"]),
            "active_seconds": max(0, state["max_active_seconds"] - state["active_seconds"]),
        },
        "updated_at": updated_at,
    }


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def parameters_digest(parameters=None):
    return hashlib.sha256(_canonical(parameters or {}).encode("utf-8")).hexdigest()[:32]


def operation_id(run_id, node_id, parameters=None):
    payload = _canonical({
        "run_id": run_id, "node_id": node_id,
        "parameters_hash": parameters_digest(parameters),
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def begin_operation(db, run_id, node_id, parameters=None, workflow_version=None):
    """Atomically claim a side effect. Returns (row, executable).

    A re-entered LangGraph node computes the same operation_id, so the second
    call never receives permission to repeat the side effect.
    """
    op_id = operation_id(run_id, node_id, parameters)
    stamp = now()
    values = {
        "operation_id": op_id, "run_id": run_id, "node_id": node_id,
        "parameters_hash": parameters_digest(parameters), "status": "running", "attempts": 1,
        "workflow_version": workflow_version, "created_at": stamp, "started_at": stamp,
    }
    dialect_insert = (postgresql_insert if db.get_bind().dialect.name == "postgresql"
                      else sqlite_insert)
    inserted_id = db.execute(dialect_insert(OperationRecord.__table__).values(**values)
                             .on_conflict_do_nothing()
                             .returning(OperationRecord.operation_id)).scalar_one_or_none()
    row = db.get(OperationRecord, op_id)
    if row is None:
        raise ServiceError("OPERATION_CONFLICT", "副作用登记冲突，请重试。", 409)
    return row, inserted_id is not None


def finish_operation(db, op_id, status, result=None, error_code=None):
    if status not in {"completed", "failed"}:
        raise ValueError(f"unsupported terminal operation status: {status}")
    changed = db.execute(update(OperationRecord).where(
        OperationRecord.operation_id == op_id, OperationRecord.status == "running"
    ).values(status=status, result=result, error_code=error_code, finished_at=now()))
    return changed.rowcount == 1


def mark_uncertain(db, op_id, reason):
    """The side effect may have happened but its outcome is unknown: never replay."""
    changed = db.execute(update(OperationRecord).where(
        OperationRecord.operation_id == op_id, OperationRecord.status == "running"
    ).values(status="uncertain", error_code=str(reason)[:80], finished_at=now()))
    return changed.rowcount == 1


def may_retry(operation):
    """Only an explicitly failed operation may be retried; uncertain never replays."""
    return operation.status == "failed"


def operation_output(row):
    return {
        "operation_id": row.operation_id, "run_id": row.run_id, "node_id": row.node_id,
        "parameters_hash": row.parameters_hash, "status": row.status,
        "attempts": row.attempts, "result": row.result, "error_code": row.error_code,
        "workflow_version": row.workflow_version,
        "created_at": api_value(row.created_at).isoformat(),
        "started_at": api_value(row.started_at).isoformat() if row.started_at else None,
        "finished_at": api_value(row.finished_at).isoformat() if row.finished_at else None,
    }


ROLES = ("think", "plan", "execute", "review", "verifier")


def freeze_agent_run_config(db, run, settings, workflow_version, *, role_versions, plugin_versions):
    """Freeze Agent inputs on a new Run in its creation transaction.

    This helper does not make Agent mode available. Call it before committing
    Run creation; any error must abort that transaction. A single SQL statement
    reads every related model/binding from one database snapshot.
    """
    if (not run.id or run.status != "queued" or run.workflow_version or run.thread_id
            or not isinstance(run.config_snapshot, dict)
            or run.config_snapshot.get("workflow_type") != "agent"):
        raise ServiceError("AGENT_SNAPSHOT_INVALID_STATE", "任务状态不允许冻结 Agent 配置。", 409)
    if not isinstance(workflow_version, str) or not workflow_version or len(workflow_version) > 32:
        raise ServiceError("INVALID_WORKFLOW_VERSION", "工作流版本无效。", 422)
    if (not isinstance(role_versions, dict) or set(role_versions) != set(ROLES)
            or not all(isinstance(v, str) and v and len(v) <= 100 for v in role_versions.values())):
        raise ServiceError("INVALID_ROLE_VERSIONS", "角色版本不完整。", 422)
    if (not isinstance(plugin_versions, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) and v and len(k) <= 100 and len(v) <= 100
            for k, v in plugin_versions.items())):
        raise ServiceError("INVALID_PLUGIN_VERSIONS", "插件版本无效。", 422)
    if db.get(RunBudget, run.id) is not None:
        raise ServiceError("AGENT_BUDGET_ALREADY_INITIALIZED", "任务预算已经初始化，不能重新冻结。", 409)

    override_model, override_provider = aliased(ModelConfig), aliased(Provider)
    query = (select(ModelConfig, Provider, RoleBinding, override_model, override_provider)
             .select_from(ModelConfig)
             .join(Provider, Provider.id == ModelConfig.provider_id)
             .outerjoin(RoleBinding, true())
             .outerjoin(override_model, override_model.id == RoleBinding.model_config_id)
             .outerjoin(override_provider, override_provider.id == override_model.provider_id)
             .where(ModelConfig.id == run.model_config_id)
             .execution_options(populate_existing=True))
    rows = db.execute(query).all()
    if not rows:
        raise ServiceError("MODEL_NOT_FOUND", "默认模型配置不存在。", 404)

    def freeze_model(model, provider, requires_tools):
        if model is None or provider is None:
            raise ServiceError("MODEL_NOT_FOUND", "角色模型配置不存在。", 404)
        if not provider.enabled:
            raise ServiceError("MODEL_DISABLED", "角色模型配置已停用。", 422)
        is_local = False
        if provider.adapter_type == "openai-compatible" and provider.base_url:
            try:
                is_local = urlsplit(provider.base_url).hostname in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
            except ValueError:
                pass
        if (provider.adapter_type in {"openai", "anthropic", "gemini"}
                or provider.adapter_type == "openai-compatible" and not is_local) and not provider.encrypted_api_key:
            raise ServiceError("MISSING_API_KEY", "角色模型尚未配置 API Key。", 422)
        if requires_tools:
            probe = (model.capabilities or {}).get("tools", {})
            status = probe.get("status") if isinstance(probe, dict) else None
            if status != "supported":
                code = "MODEL_TOOL_CALLING_UNSUPPORTED" if status == "unsupported" else "MODEL_TOOL_CAPABILITY_UNKNOWN"
                raise ServiceError(code, "Agent 模型需要先通过工具调用能力测试。", 422)
        return {
            "id": model.id, "adapter_type": provider.adapter_type, "model_name": model.model_name,
            "base_url": provider.base_url, "encrypted_api_key": provider.encrypted_api_key,
            "parameters": deepcopy(model.parameters), "timeout_seconds": model.timeout_seconds,
            "max_output_tokens": model.max_output_tokens, "version": model.version,
            "provider_version": provider.version,
            "capabilities": {"tools": {"status": ((model.capabilities or {}).get("tools") or {}).get("status")}}
            if isinstance((model.capabilities or {}).get("tools"), dict) else {},
        }

    default_model, default_provider = rows[0][:2]
    by_role = {row[2].role: row for row in rows if row[2] is not None}
    frozen_roles = {}
    for role in ROLES:
        # Verifier is a non-model extension point in M3. Preserve its version
        # without validating an unused model override or requiring credentials.
        if role == "verifier":
            frozen_roles[role] = {"role_version": role_versions[role], "source": "non_model", "model": None}
            continue
        row = by_role.get(role)
        overridden = bool(row and row[2].enabled and row[2].model_config_id)
        role_model, role_provider = (row[3], row[4]) if overridden else (default_model, default_provider)
        frozen_roles[role] = {
            "role_version": role_versions[role], "source": "override" if overridden else "default",
            "model": freeze_model(role_model, role_provider, True),
        }
    limits = budget_limits(settings)
    run.config_snapshot = {
        **freeze_model(default_model, default_provider, True),
        "workflow_type": "agent",
        "agent": {
            "workflow_version": workflow_version,
            "role_binding_version": max((row[2].version for row in rows if row[2] is not None), default=0),
            "roles": frozen_roles, "plugin_versions": deepcopy(plugin_versions), "budget": asdict(limits),
        },
    }
    run.workflow_version = workflow_version
    run.thread_id = run.id
    ensure_budget(db, run.id, limits)


def validate_agent_run_versions(run, workflow_version, *, role_versions, plugin_versions):
    """Reject an incompatible saved workflow before any attempted recovery.

    Compare implementation versions supplied by the host, not today's editable
    model settings. Errors must not expose the encrypted snapshot.
    """
    snapshot = run.config_snapshot if isinstance(run.config_snapshot, dict) else {}
    agent = snapshot.get("agent") if isinstance(snapshot.get("agent"), dict) else {}
    roles = agent.get("roles") if isinstance(agent.get("roles"), dict) else {}
    from .stage_contract import STAGE_KEYS, STAGE_V1_WORKFLOW_VERSION, STAGE_WORKFLOW_VERSION

    expected_roles = STAGE_KEYS if workflow_version in {
        STAGE_V1_WORKFLOW_VERSION, STAGE_WORKFLOW_VERSION, "stage-agent-v3", "stage-agent-v4", "stage-agent-v5", "stage-agent-v6", "stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10", "stage-agent-interactive-v1",
    } else ROLES
    same_roles = (isinstance(role_versions, dict) and set(role_versions) == set(expected_roles)
                  and all(isinstance(roles.get(role), dict)
                          and roles[role].get("role_version") == role_versions[role] for role in expected_roles))
    if (not run.id or run.thread_id != run.id or not isinstance(workflow_version, str)
            or not workflow_version or run.workflow_version != workflow_version
            or snapshot.get("workflow_type") != "agent" or agent.get("workflow_version") != workflow_version
            or not same_roles or not isinstance(plugin_versions, dict)
            or not isinstance(agent.get("plugin_versions"), dict) or agent["plugin_versions"] != plugin_versions):
        raise ServiceError("AGENT_CONFIG_VERSION_MISMATCH", "任务运行配置与当前工作流或插件版本不兼容。", 409)


def role_binding_output(db):
    rows = list(db.scalars(select(RoleBinding)))
    version = max((row.version for row in rows), default=0)
    by_role = {row.role: row for row in rows}
    bindings = []
    for role in ROLES:
        row = by_role.get(role)
        model_id = row.model_config_id if row else None
        label = None
        if model_id:
            model = db.get(ModelConfig, model_id)
            if model:
                label = f"{model.name} · {model.model_name}"
        bindings.append({
            "role": role, "model_config_id": model_id,
            "model_label": label, "enabled": bool(row.enabled) if row else False,
        })
    stamp = max((row.updated_at for row in rows), default=None)
    return {
        "version": version, "bindings": bindings,
        "updated_at": api_value(stamp).isoformat() if stamp else None,
    }


def save_role_bindings(db, body: RoleBindingsRequest):
    """Replace the whole binding set atomically. Configuration only, no execution effect."""
    seen = set()
    for item in body.bindings:
        if item.role in seen:
            raise ServiceError("DUPLICATE_ROLE", "同一角色不能重复配置。", 409)
        seen.add(item.role)
        if item.model_config_id:
            model = db.get(ModelConfig, item.model_config_id)
            if model is None:
                raise ServiceError("MODEL_NOT_FOUND", "所选模型连接不存在。", 404)
            provider = db.get(Provider, model.provider_id)
            if provider is None or not provider.enabled:
                raise ServiceError("MODEL_DISABLED", "所选模型连接已停用。", 422)
    version = (db.scalar(select(func.max(RoleBinding.version))) or 0) + 1
    db.execute(delete(RoleBinding))
    stamp = now()
    for item in body.bindings:
        db.add(RoleBinding(
            role=item.role, model_config_id=item.model_config_id or None,
            enabled=bool(item.model_config_id), version=version, updated_at=stamp,
        ))
    db.flush()
    return role_binding_output(db)


def stage_binding_output(db):
    from .stage_contract import STAGE_KEYS

    row = db.get(StageBindingSet, "local")
    if row is None:
        raise ServiceError("STAGE_CONFIG_MISSING", "阶段配置未初始化，请检查数据库迁移。", 503)
    values = row.bindings or {}
    result = []
    for stage in STAGE_KEYS:
        model_id = values.get(stage)
        model = db.get(ModelConfig, model_id) if model_id else None
        result.append({"stage": stage, "model_config_id": model_id,
                       "model_label": f"{model.name} · {model.model_name}" if model else None})
    return {"version": row.version, "bindings": result,
            "updated_at": api_value(row.updated_at).isoformat() if row.updated_at else None}


def save_stage_bindings(db, body: StageBindingsRequest):
    from .stage_contract import STAGE_KEYS

    bindings = {item.stage: item.model_config_id or None for item in body.bindings}
    if len(bindings) != len(body.bindings) or set(bindings) != set(STAGE_KEYS):
        raise ServiceError("INVALID_STAGE_BINDINGS", "必须且只能为五个不同阶段各提供一项配置。", 422)
    for model_id in set(value for value in bindings.values() if value):
        model = db.get(ModelConfig, model_id)
        if model is None:
            raise ServiceError("MODEL_NOT_FOUND", "所选模型连接不存在。", 404)
        provider = db.get(Provider, model.provider_id)
        if provider is None or not provider.enabled:
            raise ServiceError("MODEL_DISABLED", "所选模型连接已停用。", 422)
    changed = db.execute(update(StageBindingSet).where(
        StageBindingSet.id == "local", StageBindingSet.version == body.expected_version,
    ).values(version=StageBindingSet.version + 1, bindings=bindings, updated_at=now()))
    if changed.rowcount != 1:
        raise ServiceError("STAGE_CONFIG_CONFLICT", "阶段配置已更改，请刷新后重试。", 409)
    db.flush()
    db.expire_all()
    return stage_binding_output(db)


def freeze_stage_run_config(db, run, settings, *, stage_overrides=None, strategy="staged"):
    """Freeze all stage selections and model versions for a newly created Run."""
    from .stage_contract import STAGE_KEYS, STAGE_VERSIONS

    if (not run.id or run.status != "queued" or run.workflow_version or run.thread_id
            or run.config_snapshot.get("workflow_type") != "agent"):
        raise ServiceError("AGENT_SNAPSHOT_INVALID_STATE", "任务状态不允许冻结 Agent 配置。", 409)
    binding_set = db.scalar(select(StageBindingSet).where(StageBindingSet.id == "local").with_for_update())
    if binding_set is None:
        raise ServiceError("STAGE_CONFIG_MISSING", "阶段配置未初始化，请检查数据库迁移。", 503)
    override_request = ([item.model_dump() for item in sorted(stage_overrides, key=lambda item: item.stage)]
                        if stage_overrides is not None else None)
    override_map = {item["stage"]: item for item in override_request} if override_request is not None else None
    if override_map is not None and (len(override_request) != len(STAGE_KEYS) or set(override_map) != set(STAGE_KEYS)):
        raise ServiceError("INVALID_STAGE_BINDINGS", "必须且只能为五个阶段各指定一个模型。", 422)
    selections = ({key: item["model_config_id"] for key, item in override_map.items()}
                  if override_map is not None else {} if strategy == "single" else binding_set.bindings or {})
    ids = {run.model_config_id} | {selections[k] for k in STAGE_KEYS if selections.get(k)}
    rows = db.execute(select(ModelConfig, Provider).join(Provider, Provider.id == ModelConfig.provider_id)
                      .where(ModelConfig.id.in_(ids)).order_by(ModelConfig.id).with_for_update()
                      .execution_options(populate_existing=True)).all()
    by_id = {model.id: (model, provider) for model, provider in rows}

    def snapshot(identity):
        pair = by_id.get(identity)
        if not pair:
            raise ServiceError("MODEL_NOT_FOUND", "阶段模型配置不存在。", 404)
        model, provider = pair
        if not provider.enabled:
            raise ServiceError("MODEL_DISABLED", "阶段模型配置已停用。", 422)
        is_local = False
        if provider.adapter_type == "openai-compatible" and provider.base_url:
            try:
                is_local = urlsplit(provider.base_url).hostname in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
            except ValueError:
                pass
        if (provider.adapter_type in {"openai", "anthropic", "gemini"}
                or provider.adapter_type == "openai-compatible" and not is_local) and not provider.encrypted_api_key:
            raise ServiceError("MISSING_API_KEY", "阶段模型尚未配置 API Key。", 422)
        value = {"id": model.id, "adapter_type": provider.adapter_type, "model_name": model.model_name,
                "base_url": provider.base_url, "encrypted_api_key": provider.encrypted_api_key,
                "parameters": deepcopy(model.parameters), "timeout_seconds": model.timeout_seconds,
                "max_output_tokens": model.max_output_tokens, "version": model.version,
                "context_policy": deepcopy(model.context_policy or {}),
                "provider_version": provider.version}
        value["capabilities"] = frozen_model_capabilities(value, model.capabilities)
        return value

    frozen = {}
    for stage in STAGE_KEYS:
        chosen = selections.get(stage)
        if override_map is not None:
            pair = by_id.get(chosen)
            expected = override_map[stage]
            if not pair:
                raise ServiceError("MODEL_NOT_FOUND", "阶段模型配置不存在。", 404)
            if pair[1].adapter_type == "mock":
                raise ServiceError("MOCK_CANDIDATE_UNSUPPORTED", "模拟模型不能作为真实 Agent 的阶段模型。", 422)
            if pair[0].version != expected["model_version"] or pair[1].version != expected["provider_version"]:
                raise ServiceError("MODEL_CONFIG_CHANGED", "阶段模型配置已更改，请刷新并重新确认。", 409)
        source = "task_override" if override_map is not None else ("override" if chosen else "default")
        frozen[stage] = {"role_version": STAGE_VERSIONS[stage], "source": source,
                         "model": snapshot(chosen or run.model_config_id)}
    limits = budget_limits(settings)
    selected_version = settings.stage_workflow_version
    review_policy = getattr(settings, "review_failure_policy", "fail") if settings is not None else "fail"
    revision_policy = getattr(settings, "review_revision_policy", "off") if settings is not None else "off"
    run.config_snapshot = {**snapshot(run.model_config_id), "workflow_type": "agent", "agent": {
        "workflow_version": selected_version, "role_binding_version": binding_set.version,
        "roles": frozen, "override_request": override_request, "plugin_versions": {}, "budget": asdict(limits),
        **({"review_failure_policy": review_policy} if review_policy != "fail" else {}),
        **({"review_revision_policy": revision_policy} if revision_policy != "off" else {})}}
    run.workflow_version, run.thread_id = selected_version, run.id
    ensure_budget(db, run.id, limits)
