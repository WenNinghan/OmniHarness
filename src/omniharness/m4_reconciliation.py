"""Explicit, evidence-bound retirement of unknown terminal tool operations.

Reconciliation never resumes a Run or replays an operation. A retired ledger is
deliberately not a completed ledger, and cannot be used as a successful replay.
"""
from copy import deepcopy
from hashlib import sha256
from typing import Literal

from opensandbox.exceptions import SandboxApiException
from opensandbox.manager import SandboxManager
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .m4_services import reclaim_tool_slot, release_workspace_lease, tool_output
from .models import OperationRecord, Run, ToolOperation, WorkspaceSnapshot, now
from .sandbox import SandboxConfig
from .services import TERMINAL, ServiceError, add_event, require
from .workspace_client import BrokerClient, read_service_key


class ReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_unverified: Literal[True]
    client_request_id: str = Field(min_length=1, max_length=100)


def _reject(code, message):
    raise ServiceError(code, message, 409)


def _operation(db, operation_id, body):
    operation = db.scalar(select(ToolOperation).where(ToolOperation.id == operation_id).with_for_update())
    if operation is None:
        raise ServiceError("NOT_FOUND", "工具操作不存在。", 404)
    run = require(db, Run, operation.run_id)
    if operation.binding_fingerprint != body.fingerprint:
        _reject("RECONCILIATION_BINDING_CHANGED", "操作参数或版本已变化，请重新查看记录。")
    if run.status not in TERMINAL:
        _reject("RECONCILIATION_RUN_ACTIVE", "请先取消任务并等待停止，再核对未知结果。")
    if operation.tool_name not in {"workspace_patch", "run_command"}:
        _reject("RECONCILIATION_UNSUPPORTED", "该工具暂无可自动核对的本地证据，记录仍保持未知。")
    if operation.status not in {"running", "dispatched", "uncertain", "reconciled"}:
        _reject("RECONCILIATION_NOT_UNKNOWN", "该操作不属于待核对的未知操作。")
    ledger = db.get(OperationRecord, operation.operation_record_id) if operation.operation_record_id else None
    if (ledger is None or ledger.run_id != run.id or ledger.node_id != "agent:execute:tool_calls"
            or ledger.status not in {"running", "uncertain", "reconciled"}):
        _reject("RECONCILIATION_LEDGER_MISMATCH", "缺少与该操作绑定的未知执行记录，未释放工作区。")
    return operation, run, ledger


def _view(operation, released):
    return {"operation": tool_output(operation),
            "reconciliation": deepcopy(operation.result["reconciliation"]), "workspace_released": released}


def _snapshot(operation, run):
    return {"run_id": run.id, "workspace_id": run.workspace_id,
            "workspace": deepcopy(run.config_snapshot.get("workspace", {})),
            "tool_name": operation.tool_name, "parameters": deepcopy(operation.parameters),
            "sandbox_id": operation.sandbox_id, "operation_record_id": operation.operation_record_id,
            "binding_fingerprint": operation.binding_fingerprint, "snapshot_id": operation.snapshot_id,
            "command_id": operation.command_id}


def _creation_request(db, operation, run):
    from .sandbox_creation_api import CreationRequest
    snapshot = db.get(WorkspaceSnapshot, operation.snapshot_id) if operation.snapshot_id else None
    workspace = run.config_snapshot.get("workspace", {})
    if (snapshot is None or snapshot.run_id != run.id or snapshot.workspace_id != run.workspace_id
            or snapshot.task_id != operation.task_id or snapshot.policy_version != workspace.get("policy_version")
            or not isinstance(operation.parameters, dict)):
        _reject("RECONCILIATION_BINDING_CHANGED", "缺少匹配的创建快照，保留占位。")
    try:
        return CreationRequest(run_id=run.id, operation_id=operation.id,
            operation_fingerprint=operation.binding_fingerprint, snapshot_path=snapshot.workdir,
            timeout_seconds=operation.parameters.get("timeout_seconds"))
    except ValueError:
        _reject("RECONCILIATION_BINDING_CHANGED", "创建参数不完整，保留占位。")


async def _sandbox_absent(settings, workspace, sandbox_id):
    frozen = workspace.get("sandbox", {})
    expected = {"endpoint": settings.sandbox_endpoint, "image": settings.sandbox_image,
                "snapshot_root": settings.sandbox_snapshot_root, "instance_id": settings.instance_id,
                "max_command_seconds": settings.sandbox_max_command_seconds}
    if frozen != expected:
        _reject("RECONCILIATION_SERVICE_CHANGED", "沙箱服务与运行时冻结配置不同，不能将其他服务的不存在作为回收证明。")
    if not sandbox_id:
        _reject("RECONCILIATION_SANDBOX_ID_MISSING", "没有保存沙箱 ID，不能证明在途命令已结束。")
    manager = None
    try:
        config = SandboxConfig(**frozen, api_key=read_service_key(settings.sandbox_key_file))
        manager = await SandboxManager.create(connection_config=config.connection())
        try:
            await manager.get_sandbox_info(sandbox_id)
        except SandboxApiException as exc:
            if exc.status_code == 404:
                return {"kind": "sandbox_absence", "sandbox_id": sandbox_id, "resource_reclaimed": True}
            raise
    except Exception:
        raise ServiceError("RECONCILIATION_PROBE_FAILED", "无法确认原沙箱已回收，保留未知记录与资源占位。", 409) from None
    finally:
        if manager is not None:
            try:
                await manager.close()
            except Exception:
                raise ServiceError("RECONCILIATION_PROBE_FAILED", "沙箱核对连接未正常结束，未更改操作记录。", 409) from None
    _reject("RECONCILIATION_SANDBOX_ACTIVE", "原沙箱仍然存在，不能释放该操作的占位。")


async def _patch_receipt(settings, workspace, operation_id, snapshot):
    if (workspace.get("broker_endpoint") != settings.workspace_broker_url
            or workspace.get("grant", {}).get("instance_id") != settings.instance_id):
        _reject("RECONCILIATION_SERVICE_CHANGED", "宿主服务与运行时冻结配置不同，不能使用其他服务的回执。")
    parameters = snapshot["parameters"]
    content = parameters.get("content")
    if not isinstance(content, str):
        _reject("RECONCILIATION_BINDING_CHANGED", "原写入内容不完整，不能核对文件版本。")
    receipt = await BrokerClient(settings).request(workspace["id"], workspace["policy_version"],
        "operation-result", operation_id=operation_id, run_id=snapshot["run_id"])
    expected = {"workspace_id": snapshot["workspace_id"], "instance_id": settings.instance_id,
                "run_id": snapshot["run_id"], "policy_version": workspace["policy_version"],
                "recovery_id": operation_id, "path": parameters.get("path"),
                "before_sha256": parameters.get("expected_sha256"),
                "sha256": sha256(content.encode("utf-8")).hexdigest(), "size": len(content.encode("utf-8")),
                "state": "completed", "recorded_completed": True, "matches_expected_result": True}
    if (any(receipt.get(key) != value for key, value in expected.items())
            or receipt.get("current_sha256") != expected["sha256"]):
        _reject("RECONCILIATION_PATCH_UNCONFIRMED", "回执、目标文件或版本未能完整对应，保留未知记录；未重新写入文件。")
    return {"kind": "patch_receipt", **expected, "current_sha256": receipt["current_sha256"]}


async def reconcile_tool(factory, settings, operation_id, body: ReconciliationRequest):
    with factory.begin() as db:
        operation, run, ledger = _operation(db, operation_id, body)
        if operation.status == "reconciled":
            recorded = (operation.result or {}).get("reconciliation", {})
            if (ledger.status != "reconciled" or recorded.get("client_request_id") != body.client_request_id
                    or recorded.get("fingerprint") != body.fingerprint):
                _reject("RECONCILIATION_ALREADY_RECORDED", "该操作已有核对记录，请查看原记录。")
            return _view(operation, release_workspace_lease(db, run.id))
        snapshot = _snapshot(operation, run)
        creation_request = (_creation_request(db, operation, run)
            if operation.tool_name == "run_command" and snapshot["workspace"].get("creation_service") else None)
    workspace = snapshot["workspace"]
    if not workspace or workspace.get("id") != snapshot["workspace_id"]:
        _reject("RECONCILIATION_WORKSPACE_MISMATCH", "缺少原运行的工作区授权快照。")
    if creation_request is not None:
        from .sandbox import SandboxBoundaryError
        from .sandbox_creation_client import CreationServiceClient
        try:
            receipt = await CreationServiceClient(settings, workspace["creation_service"]).reclaim(creation_request)
        except (SandboxBoundaryError, ValueError, OSError):
            _reject("RECONCILIATION_PROBE_FAILED", "创建服务未确认资源回收，保留未知记录与占位。")
        if snapshot["sandbox_id"] not in (None, receipt["sandbox_id"]):
            _reject("RECONCILIATION_BINDING_CHANGED", "回收证明与沙箱身份不一致。")
        evidence = {**receipt["evidence"], "creation_service_id": receipt["service_id"]}
    else:
        evidence = (await _sandbox_absent(settings, workspace, snapshot["sandbox_id"])
                    if snapshot["tool_name"] == "run_command"
                    else await _patch_receipt(settings, workspace, operation_id, snapshot))
    with factory.begin() as db:
        operation, run, ledger = _operation(db, operation_id, body)
        if operation.status == "reconciled":
            recorded = (operation.result or {}).get("reconciliation", {})
            if (ledger.status != "reconciled" or recorded.get("client_request_id") != body.client_request_id
                    or recorded.get("fingerprint") != body.fingerprint):
                _reject("RECONCILIATION_ALREADY_RECORDED", "该操作已有核对记录，请查看原记录。")
            return _view(operation, release_workspace_lease(db, run.id))
        if _snapshot(operation, run) != snapshot:
            _reject("RECONCILIATION_BINDING_CHANGED", "核对期间操作绑定发生变化，未释放工作区。")
        if creation_request is not None and _creation_request(db, operation, run) != creation_request:
            _reject("RECONCILIATION_BINDING_CHANGED", "核对期间创建快照变化，未释放工作区。")
        if creation_request is not None:
            operation.sandbox_id = evidence["sandbox_id"]
        reconciliation = {"fingerprint": body.fingerprint, "client_request_id": body.client_request_id,
            "verification": "UNVERIFIED", "outcome": "unknown" if operation.tool_name == "run_command" else "write_receipt_matched",
            "evidence": evidence, "acknowledged_at": now().isoformat(), "replayed": False}
        operation.status, ledger.status = "reconciled", "reconciled"
        operation.result = {**(operation.result or {}), "reconciliation": reconciliation}
        ledger.result = {**(ledger.result or {}), "reconciliation": deepcopy(reconciliation)}
        operation.error_code = ledger.error_code = "RESULT_UNVERIFIED"
        operation.finished_at = ledger.finished_at = now()
        reclaim_tool_slot(db, operation.id, sandbox_id=operation.sandbox_id if operation.tool_name == "run_command" else None)
        db.flush()
        db.refresh(operation)
        released = release_workspace_lease(db, run.id)
        add_event(db, run, "tool.reconciled", {**tool_output(operation), "workspace_released": released})
        return _view(operation, released)
