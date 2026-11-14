"""M4 durable authorization and evidence; execution belongs to the Worker."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import func, select, text

from .models import (
    Approval,
    ArtifactVersion,
    OperationRecord,
    Run,
    RunCommand,
    ToolOperation,
    Workspace,
    WorkspaceLease,
    WorkspacePolicy,
    now,
)
from .services import TERMINAL, ServiceError, add_event, api_value, parameters_digest, require
from .workspace_io import DEFAULT_EXCLUDES, WorkspaceGrant


def _require_preview_fixture(settings, path):
    """Keep the isolated preview off projects leased by another instance.

    This lexical check is deliberately strict; the existing native broker
    independently rejects reparse points and confirms physical identity.
    """
    if settings.v6_preview:
        expected = r"G:\OmniHarness-V6-Preview-Fixture".casefold()
        if not isinstance(path, str) or path.replace("/", "\\").rstrip("\\").casefold() != expected:
            raise ServiceError("PREVIEW_WORKSPACE_RESTRICTED", "V6 预览仅允许使用专用测试项目。", 403)


def _json_value(value):
    return api_value(value).isoformat() if isinstance(value, datetime) else value


def _exclusive(db, number: int):
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


def workspace_output(db, row):
    policy = db.get(WorkspacePolicy, (row.id, row.policy_version))
    grant = policy.grant if policy else {}
    return {"id": row.id, "name": row.name, "host_path": row.host_path,
            "policy_version": row.policy_version, "enabled": row.enabled,
            "ready": bool(row.enabled and row.physical_key and row.probe.get("identity")),
            "read_allow": grant.get("read_allow", []), "write_allow": grant.get("write_allow", []),
            "excludes": list(DEFAULT_EXCLUDES) + list(grant.get("excludes", [])),
            "tool_policy": policy.tool_policy if policy else {}, "probe": row.probe,
            "created_at": api_value(row.created_at)}


def register_workspace(db, body, settings):
    from .m4_contract import M4_TOOL_NAMES, M4_TOOL_VERSION
    if not settings.enable_tools:
        raise ServiceError("TOOLS_UNAVAILABLE", "工作区工具服务尚未启用。", 503)
    _require_preview_fixture(settings, body.host_path)
    row = Workspace(name=body.name.strip(), host_path=body.host_path, policy_version=1,
                    enabled=True, probe={})
    if not row.name:
        raise ServiceError("INVALID_NAME", "请填写项目名称。", 422)
    db.add(row)
    db.flush()
    grant = WorkspaceGrant(workspace_id=row.id, instance_id=settings.instance_id, root=body.host_path,
                           read_allow=tuple(body.read_allow), write_allow=tuple(body.write_allow),
                           excludes=tuple(body.excludes))
    db.add(WorkspacePolicy(workspace_id=row.id, version=1, grant=asdict(grant), tool_policy={
        "command_approval": body.command_approval, "tools": list(M4_TOOL_NAMES),
        "tool_version": M4_TOOL_VERSION, "network": "deny", "snapshot_writeback": False,
    }))
    db.flush()
    return row


def record_workspace_probe(db, workspace_id, result):
    _exclusive(db, 7243301401)
    row = require(db, Workspace, workspace_id)
    identity = result.get("identity") if isinstance(result, dict) else None
    if (not isinstance(identity, dict) or identity.get("file_id") is None
            or result.get("workspace_id") != row.id or result.get("policy_version") != row.policy_version):
        raise ServiceError("WORKSPACE_PROBE_INVALID", "宿主目录身份未能确认。", 503)
    root = str(result.get("root", "")).replace("\\", "/").rstrip("/").casefold()
    physical_key = parameters_digest({k: identity.get(k) for k in ("volume_serial", "device", "file_id")})
    for other in db.scalars(select(Workspace).where(Workspace.id != row.id, Workspace.physical_key.is_not(None))):
        other_path = str(other.probe.get("root", "")).replace("\\", "/").rstrip("/").casefold()
        if physical_key == other.physical_key or (root and other_path and (
                root.startswith(other_path + "/") or other_path.startswith(root + "/"))):
            raise ServiceError("WORKSPACE_OVERLAP", "该目录与已登记项目相同或重叠，请使用已有工作区。", 409)
    if row.physical_key and row.physical_key != physical_key:
        raise ServiceError("WORKSPACE_IDENTITY_CHANGED", "原项目目录身份已改变，请重新登记。", 409)
    row.physical_key, row.probe = physical_key, deepcopy(result)
    # The grant becomes immutable as soon as registration has been verified.
    policy = require(db, WorkspacePolicy, (row.id, row.policy_version))
    if policy.grant.get("expected_identity") not in (None, identity):
        raise ServiceError("WORKSPACE_IDENTITY_CHANGED", "原项目目录身份已改变。", 409)
    policy.grant = {**policy.grant, "expected_identity": deepcopy(identity)}
    db.flush()
    return workspace_output(db, row)


def workspace_grant(db, workspace_id, version, instance_id):
    row = require(db, Workspace, workspace_id)
    policy = require(db, WorkspacePolicy, (workspace_id, version))
    if policy.grant.get("instance_id") != instance_id:
        raise ServiceError("INSTANCE_MISMATCH", "工作区属于其他实例。", 403)
    return {**deepcopy(policy.grant), "enabled": row.enabled}


def freeze_workspace(db, run, settings, workspace_id, *, workflow_version=None):
    # Ordinary instances continue to freeze v4. Only the explicitly enabled,
    # dedicated preview selects v6 for newly created workspace Runs.
    from .m4_contract import (
        COLLAB_WORKFLOWS,
        INTERACTIVE_COMPAT_WORKFLOW_VERSION,
        INTERACTIVE_V9_WORKFLOW_VERSION,
        INTERACTIVE_V10_WORKFLOW_VERSION,
        M4_PLUGIN_VERSIONS,
        M4_V8_PLUGIN_VERSIONS,
        M4_V9_PLUGIN_VERSIONS,
        M4_V10_PLUGIN_VERSIONS,
        workspace_role_versions,
    )
    from .mcp_tools import load_mcp_configuration
    from .sandbox import SandboxConfig
    from .sandbox_creation_client import creation_service_binding
    from .workspace_client import read_service_key
    selected_version = settings.workspace_workflow_version if workflow_version is None else workflow_version
    if selected_version not in COLLAB_WORKFLOWS:
        raise ServiceError("WORKFLOW_VERSION_INVALID", "不支持所选工作流版本。", 422)
    role_versions = workspace_role_versions(selected_version)
    if not settings.enable_tools:
        raise ServiceError("TOOLS_UNAVAILABLE", "真实工具服务尚未就绪。", 503)
    row = require(db, Workspace, workspace_id)
    if not row.enabled or not row.physical_key or not row.probe.get("identity"):
        raise ServiceError("WORKSPACE_NOT_READY", "工作区未通过目录探测或已停用。", 409)
    _require_preview_fixture(settings, row.host_path)
    policy = require(db, WorkspacePolicy, (workspace_id, row.policy_version))
    agent = deepcopy(run.config_snapshot["agent"])
    tools = agent["roles"]["execute"]["model"].get("capabilities", {}).get("tools", {})
    if tools.get("status") != "supported":
        raise ServiceError("TOOL_MODEL_UNVERIFIED", "执行阶段模型需先通过工具调用能力测试。", 422)
    try:
        SandboxConfig(endpoint=settings.sandbox_endpoint, api_key=read_service_key(settings.sandbox_key_file),
                      image=settings.sandbox_image, snapshot_root=settings.sandbox_snapshot_root,
                      instance_id=settings.instance_id, max_command_seconds=settings.sandbox_max_command_seconds)
        read_service_key(settings.workspace_service_key_file)
        creation_service = creation_service_binding(settings)
        if creation_service is not None:
            read_service_key(settings.sandbox_creation_key_file)
    except Exception:
        raise ServiceError("TOOLS_UNAVAILABLE", "沙箱或宿主文件服务配置不完整。", 503) from None
    agent["workflow_version"] = selected_version
    agent["plugin_versions"] = deepcopy(M4_V10_PLUGIN_VERSIONS if selected_version == INTERACTIVE_V10_WORKFLOW_VERSION else
                                         M4_V9_PLUGIN_VERSIONS if selected_version == INTERACTIVE_V9_WORKFLOW_VERSION else
                                         M4_V8_PLUGIN_VERSIONS if selected_version == INTERACTIVE_COMPAT_WORKFLOW_VERSION else M4_PLUGIN_VERSIONS)
    for role, value in agent["roles"].items():
        value["role_version"] = role_versions[role]
    run.workspace_id, run.workflow_version = row.id, selected_version
    run.config_snapshot = {**run.config_snapshot, "agent": agent, "workspace": {
        "id": row.id, "name": row.name, "policy_version": row.policy_version,
        "grant": deepcopy(policy.grant), "tool_policy": deepcopy(policy.tool_policy),
        "probe_identity": deepcopy(row.probe["identity"]), "sandbox": {
            "endpoint": settings.sandbox_endpoint,
            "image": settings.sandbox_image, "instance_id": settings.instance_id,
            "snapshot_root": settings.sandbox_snapshot_root,
            "max_command_seconds": settings.sandbox_max_command_seconds,
        },
        "broker_endpoint": settings.workspace_broker_url,
        "mcp": load_mcp_configuration(settings),
        **({"creation_service": creation_service} if creation_service is not None else {}),
    }}


def acquire_workspace_lease(db, run_id, *, owner_epoch=None):
    run = require(db, Run, run_id)
    if not run.workspace_id:
        return True
    workspace = db.scalar(select(Workspace).where(Workspace.id == run.workspace_id).with_for_update())
    if not workspace or not workspace.enabled:
        raise ServiceError("WORKSPACE_DISABLED", "工作区已停用。", 409)
    lease = db.get(WorkspaceLease, workspace.id)
    if lease and lease.run_id != run_id:
        return False
    if not lease:
        db.add(WorkspaceLease(workspace_id=workspace.id, run_id=run_id, owner_epoch=owner_epoch))
    else:
        lease.owner_epoch = owner_epoch
    db.flush()
    return True


def validate_workspace_lease(db, workspace_id, run_id):
    lease, run, workspace = db.get(WorkspaceLease, workspace_id), db.get(Run, run_id), db.get(Workspace, workspace_id)
    return bool(lease and lease.run_id == run_id and run and run.workspace_id == workspace_id
                and run.status in {"running", "pause_requested"} and workspace and workspace.enabled)


def release_workspace_lease(db, run_id):
    # Unknown side effects must be reconciled before another writer can start.
    if db.scalar(select(ToolOperation.id).where(ToolOperation.run_id == run_id,
            (ToolOperation.status.in_(["dispatched", "running", "uncertain"])) | ToolOperation.slot_claimed).limit(1)):
        return False
    if db.scalar(select(OperationRecord.operation_id).where(OperationRecord.run_id == run_id,
            OperationRecord.node_id == "agent:execute:tool_calls",
            OperationRecord.status.in_(["running", "uncertain"])).limit(1)):
        return False
    for lease in db.scalars(select(WorkspaceLease).where(WorkspaceLease.run_id == run_id).with_for_update()):
        db.delete(lease)
    return True


def ensure_tool_request(db, *, run_id, operation_id, task_id, tool_call_id, tool_name,
                        tool_version, parameters, binding_fingerprint, requires_approval,
                        agent_run_id=None):
    run = require(db, Run, run_id)
    existing = db.get(ToolOperation, operation_id)
    binding = dict(run_id=run_id, task_id=task_id, tool_call_id=tool_call_id, tool_name=tool_name,
                   tool_version=tool_version, parameters=parameters, binding_fingerprint=binding_fingerprint)
    if existing:
        if any(getattr(existing, key) != value for key, value in binding.items()):
            raise ServiceError("TOOL_BINDING_CONFLICT", "工具请求与已保存的参数版本不一致。", 409)
        return existing
    if run.status != "running":
        raise ServiceError("RUN_NOT_EXECUTABLE", "任务当前不能派发工具。", 409)
    row = ToolOperation(id=operation_id, **binding, agent_run_id=agent_run_id,
                        status="awaiting_approval" if requires_approval else "authorized")
    db.add(row)
    db.flush()
    add_event(db, run, "tool.requested", tool_output(row))
    if requires_approval:
        ensure_approval(db, row)
    return row


def ensure_approval(db, operation):
    existing = db.scalar(select(Approval).where(Approval.operation_id == operation.id))
    if existing:
        if existing.fingerprint != operation.binding_fingerprint:
            raise ServiceError("APPROVAL_BINDING_CONFLICT", "审批与操作版本不一致。", 409)
        return existing
    approval = Approval(operation_id=operation.id, fingerprint=operation.binding_fingerprint, status="pending")
    db.add(approval)
    db.flush()
    add_event(db, require(db, Run, operation.run_id), "approval.requested", approval_output(approval, operation))
    return approval


def decide_approval(db, identity, body):
    approval = require(db, Approval, identity)
    operation = require(db, ToolOperation, approval.operation_id)
    run = db.scalar(select(Run).where(Run.id == operation.run_id).with_for_update())
    db.refresh(approval, with_for_update=True)
    decision = {"approve": "approved", "reject": "rejected"}[body.decision]
    if body.fingerprint != approval.fingerprint or body.fingerprint != operation.binding_fingerprint:
        raise ServiceError("APPROVAL_STALE", "操作参数已改变，请查看新的审批。", 409)
    if approval.decision_request_id == body.client_request_id and approval.status == decision:
        return approval_output(approval, operation)
    if approval.status != "pending" or run.status in TERMINAL | {"cancel_requested", "pending_review"}:
        raise ServiceError("APPROVAL_INACTIVE", "此审批已结束或任务已停止。", 409)
    if db.scalar(select(RunCommand.id).where(RunCommand.run_id == run.id,
            RunCommand.status == "pending", RunCommand.command_type == "append_requirement").limit(1)):
        raise ServiceError("APPROVAL_REQUIREMENTS_CHANGED", "有新的补充要求，需先由任务重新规划操作。", 409)
    approval.status, approval.decision_at = decision, now()
    approval.decision_request_id = body.client_request_id
    add_event(db, run, "approval.decided", approval_output(approval, operation))
    # Deliberately no Run transition or tool execution in an HTTP request.
    return approval_output(approval, operation)


def expire_approvals(db, run_id, *, status="expired"):
    run = require(db, Run, run_id)
    rows = db.execute(select(Approval, ToolOperation).join(ToolOperation).where(
        ToolOperation.run_id == run_id, ToolOperation.status == "awaiting_approval",
        Approval.status.in_(["pending", "approved"]))).all()
    for approval, operation in rows:
        approval.status, approval.decision_at = status, now()
        add_event(db, run, "approval.decided", approval_output(approval, operation))


def claim_tool_slot(db, run_id, operation_id):
    _exclusive(db, 7243301402)
    row = require(db, ToolOperation, operation_id)
    if row.run_id != run_id:
        raise ServiceError("TOOL_BINDING_CONFLICT", "操作不属于当前任务。", 409)
    if row.slot_claimed:
        raise ServiceError("TOOL_SLOT_UNCERTAIN", "已有在途执行占用，请先核对。", 409)
    if db.scalar(select(func.count()).select_from(ToolOperation).where(ToolOperation.slot_claimed)) >= 2:
        return False
    row.slot_claimed = True
    db.flush()
    return True


def reclaim_tool_slot(db, operation_id, *, sandbox_id=None):
    row = require(db, ToolOperation, operation_id)
    if sandbox_id is not None and row.sandbox_id != sandbox_id:
        raise ServiceError("SANDBOX_IDENTITY_CONFLICT", "回收证明与沙箱身份不一致。", 409)
    row.slot_claimed, row.reclaimed_at = False, now()


def finish_tool_request(db, operation_id, status, result=None, error_code=None):
    if status not in {"completed", "failed", "uncertain", "rejected", "cancelled"}:
        raise ValueError("Invalid tool result state")
    row = require(db, ToolOperation, operation_id)
    if row.status in {"completed", "failed", "rejected", "cancelled"}:
        if row.status != status or row.result != result:
            raise ServiceError("TOOL_RESULT_CONFLICT", "已保存的工具结果不允许覆盖。", 409)
        return row
    row.status, row.result, row.error_code, row.finished_at = status, deepcopy(result), error_code, now()
    add_event(db, require(db, Run, row.run_id), "tool." + status, tool_output(row))
    return row


def tool_output(row):
    output = {key: _json_value(getattr(row, key)) for key in (
        "id", "run_id", "task_id", "tool_call_id", "tool_name", "tool_version", "parameters",
        "binding_fingerprint", "status", "result", "error_code", "sandbox_id", "command_id",
        "snapshot_id", "slot_claimed", "reclaimed_at", "created_at", "started_at", "finished_at",
    )}
    # V9's transient keyed request binding is internal; expose the same null
    # pending result as previous workflows in API and event projections.
    result = row.result
    if (row.status in {"authorized", "awaiting_approval", "dispatched", "running"}
            and isinstance(result, dict) and set(result) in ({"v9_request_mac"}, {"v9_request_mac", "v9_snapshot_mac"})
            and all(isinstance(value, str) for value in result.values())):
        output["result"] = None
    return output


def approval_output(row, operation):
    return {"id": row.id, "operation_id": row.operation_id, "run_id": operation.run_id,
            "fingerprint": row.fingerprint, "status": row.status,
            "requested_at": _json_value(row.requested_at), "decision_at": _json_value(row.decision_at),
            "operation": tool_output(operation)}


def artifact_output(row: ArtifactVersion):
    return {key: _json_value(getattr(row, key)) for key in (
        "id", "run_id", "operation_id", "workspace_id", "kind", "name", "sha256",
        "size_bytes", "metadata_json", "created_at",
    )}
