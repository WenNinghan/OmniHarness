"""Manual, SHA-verified rollback of completed workspace_patch operations."""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .m4_services import tool_output
from .models import Run, ToolOperation, Workspace, WorkspaceLease, now
from .services import TERMINAL, ServiceError, add_event, require
from .workspace_client import BrokerClient


class RollbackPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_manual_rollback: Literal[True]
    client_request_id: str = Field(min_length=1, max_length=100)


def _reject(code: str, message: str, status: int = 409) -> None:
    raise ServiceError(code, message, status)


def _load_eligible_patch(db, settings, operation_id: str, body: RollbackPatchRequest):
    operation = db.scalar(select(ToolOperation).where(ToolOperation.id == operation_id).with_for_update())
    if operation is None:
        _reject("NOT_FOUND", "工具操作不存在。", 404)
    if operation.tool_name != "workspace_patch":
        _reject("ROLLBACK_UNSUPPORTED", "仅支持对文件补丁操作执行人工回滚。")
    if operation.status != "completed" or not isinstance(operation.result, dict) or operation.result.get("error"):
        _reject("ROLLBACK_NOT_COMPLETED", "只能回滚已成功完成的补丁操作。")
    if operation.binding_fingerprint != body.fingerprint:
        _reject("ROLLBACK_BINDING_CHANGED", "操作参数或版本已变化，请刷新后再试。")
    run = require(db, Run, operation.run_id)
    if run.status not in TERMINAL:
        _reject("ROLLBACK_RUN_ACTIVE", "请等待当前任务结束后再执行人工回滚。")
    existing = operation.result.get("rollback")
    if isinstance(existing, dict):
        if (
            existing.get("client_request_id") == body.client_request_id
            and existing.get("rolled_back_from_sha256") == body.expected_sha256
        ):
            return operation, run, None, True
        _reject("PATCH_ALREADY_ROLLED_BACK", "该补丁已完成人工回滚。")
    if operation.result.get("sha256") != body.expected_sha256:
        _reject("ROLLBACK_VERSION_MISMATCH", "补丁版本校验不一致，未执行回滚。")
    if not run.workspace_id:
        _reject("WORKSPACE_DISABLED", "该操作未关联授权工作区。")
    workspace_row = db.get(Workspace, run.workspace_id)
    if workspace_row is None or not workspace_row.enabled:
        _reject("WORKSPACE_DISABLED", "工作区已停用或不存在，无法回滚。")
    if db.get(WorkspaceLease, run.workspace_id) is not None:
        _reject("WORKSPACE_LEASE_ACTIVE", "工作区当前正被活动任务占用，请等待任务结束后再回滚。")
    ws_snap = (run.config_snapshot or {}).get("workspace") or {}
    policy_version = ws_snap.get("policy_version")
    if (
        ws_snap.get("id") != run.workspace_id
        or not isinstance(policy_version, int)
        or policy_version < 1
        or ws_snap.get("broker_endpoint") != settings.workspace_broker_url
        or (ws_snap.get("grant") or {}).get("instance_id") != settings.instance_id
    ):
        _reject("ROLLBACK_SERVICE_CHANGED", "工作区服务配置与运行快照不一致，拒绝跨配置回滚。")
    return operation, run, (run.workspace_id, policy_version, run.id), False


async def rollback_patch(factory, settings, operation_id: str, body: RollbackPatchRequest) -> dict:
    with factory.begin() as db:
        operation, _run, target_info, already_replayed = _load_eligible_patch(db, settings, operation_id, body)
        if already_replayed:
            return {"operation": tool_output(operation), "rollback": deepcopy(operation.result["rollback"])}
        workspace_id, policy_version, run_id = target_info

    receipt = await BrokerClient(settings).request(
        workspace_id,
        policy_version,
        "rollback",
        operation_id=operation_id,
        run_id=run_id,
        expected_current_sha256=body.expected_sha256,
    )

    with factory.begin() as db:
        operation, run, _target_info, already_replayed = _load_eligible_patch(db, settings, operation_id, body)
        if already_replayed:
            return {"operation": tool_output(operation), "rollback": deepcopy(operation.result["rollback"])}
        rollback_record = {
            **receipt,
            "client_request_id": body.client_request_id,
            "rolled_back_at": now().isoformat(),
        }
        operation.result = {**deepcopy(operation.result), "rollback": rollback_record}
        db.flush()
        db.refresh(operation)
        add_event(
            db,
            run,
            "workspace.patch_rolled_back",
            {
                "operation_id": operation.id,
                "path": receipt["path"],
                "action": receipt["action"],
                "restored_sha256": receipt["restored_sha256"],
            },
        )
        return {"operation": tool_output(operation), "rollback": rollback_record}
