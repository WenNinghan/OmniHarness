"""Authorized workspace tools; actual execution reuses the existing fenced ledger."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select

from .m3_agents import AgentBoundaryError, AgentBudgetExceeded, invocation_identity
from .m3_host import AgentOperationNeedsReview
from .m4_contract import M4_TOOL_NAMES, M4_TOOL_VERSION
from .mcp_tools import MCPBoundaryError, MCPRegistry, MCPResultUnknown
from .models import Approval, ArtifactVersion, Run, RunCommand, ToolOperation, WorkspaceSnapshot, now
from .sandbox import SandboxBoundaryError
from .services import ServiceError
from .workspace_io import WorkspaceError


class ToolSlotUnavailable(Exception):
    pass


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListArguments(ToolArguments):
    path: str = Field(default=".", max_length=4096)


class PathArguments(ToolArguments):
    path: str = Field(min_length=1, max_length=4096)


class ReadArguments(PathArguments):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=1, le=8000)


class SearchArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=50, ge=1, le=100)


class PatchArguments(PathArguments):
    content: str = Field(max_length=1_000_000)
    expected_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class PatchArgumentsV8(PathArguments):
    """V8 only: omitted/null/"null" express create-only, never replacement."""
    content: str = Field(max_length=1_000_000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("expected_sha256", mode="before")
    @classmethod
    def normalize_literal_null(cls, value):
        return None if value == "null" else value


class CommandArguments(ToolArguments):
    command: str | list[str]
    cwd: str = Field(default=".", max_length=4096)
    timeout_seconds: int = Field(default=120, ge=1, le=600)


    @model_validator(mode="after")
    def bounded_command(self):
        if isinstance(self.command, str):
            parts = [self.command]
        else:
            parts = self.command
        if (not 1 <= len(parts) <= 256 or not parts[0].strip() or sum(map(len, parts)) > 32768
                or any("\x00" in part for part in parts)
                or self.cwd.startswith("/") or "\\" in self.cwd or "\x00" in self.cwd
                or ".." in self.cwd.split("/")):
            raise ValueError("Command or working directory is outside the declared bounds")
        return self


class MCPArguments(ToolArguments):
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    tool_name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    arguments: dict = Field(default_factory=dict)


class ArtifactArguments(ToolArguments):
    artifact_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=1, le=8000)


class AskUserArguments(ToolArguments):
    question: str = Field(min_length=1, max_length=2000)
    reason: str = Field(default="", max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def valid_question(self):
        if (not self.question.strip() or any(not value.strip() or len(value) > 200 for value in self.options)
                or len(set(self.options)) != len(self.options)):
            raise ValueError("Invalid clarification question or options")
        return self


TOOL_SCHEMAS = {
    "workspace_list": (ListArguments, "列出授权原项目的相对目录，凭据和链接被排除。"),
    "workspace_read": (ReadArguments, "读取授权原项目UTF-8文件，返回内容及补丁需要的SHA256。"),
    "workspace_search": (SearchArguments, "在授权文件中搜索文本，返回位置和文件SHA256。"),
    "workspace_patch": (PatchArguments, "替换授权原项目单个文件。先读取取得expected_sha256；新文件填null。冲突不会覆盖。"),
    "run_command": (CommandArguments, "在最新的过滤项目快照中离线运行命令或测试。快照改动不回灌；每次命令新建独立快照。"),
    "mcp_call": (MCPArguments, "调用本机配置中明确授权的 MCP 工具；有副作用的调用需要具体审批。"),
    "artifact_read": (ArtifactArguments, "按字节分页读取当前任务已保存的完整工具输出。用工件ID与next_offset继续读取。"),
}


def tool_definitions(workspace=None, *, interactive=False, patch_schema_v2=False):
    async def gateway_required(**_arguments):
        raise AgentBoundaryError("Workspace tools require the authorized host gateway")
    definitions = dict(TOOL_SCHEMAS)
    if patch_schema_v2:
        definitions["workspace_patch"] = (PatchArgumentsV8,
            "修改已有文件前先读取并提供准确的 expected_sha256；仅新建不存在的文件可省略该字段或使用 JSON null。已存在的目标不会以新建方式覆盖。")
    if interactive:
        definitions["ask_user"] = (AskUserArguments,
            "仅当任务确实不能继续且缺少的信息必须由用户提供时，提出问题并等待用户回复；优先使用合理假设。")
    servers = (workspace or {}).get("mcp", {}).get("servers", [])
    if not servers:
        definitions.pop("mcp_call")
    else:
        catalog = [{"server_id": server["id"], "tools": server["tools"]} for server in servers if server["enabled"]]
        definitions["mcp_call"] = (MCPArguments, TOOL_SCHEMAS["mcp_call"][1]
            + " 已授权目录：" + json.dumps(catalog, ensure_ascii=False))
    return [StructuredTool.from_function(
        coroutine=gateway_required, name=name, description=description, args_schema=schema,
    ) for name, (schema, description) in definitions.items()]


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _message(call, result):
    return ToolMessage(tool_call_id=call["id"], name=call["name"],
                       content=json.dumps(result, ensure_ascii=False),
                       status="error" if result.get("error") else "success")


class WorkspaceToolGateway:
    def __init__(self, *, host, workspace: dict, broker, sandbox, artifacts, scheduler):
        self.host, self.workspace = host, workspace
        self.broker, self.sandbox, self.artifacts, self.scheduler = broker, sandbox, artifacts, scheduler
        self.patch_lock = asyncio.Lock()
        self.mcp = MCPRegistry(host.worker.settings, workspace.get("mcp", {}))
        if not workspace.get("grant", {}).get("expected_identity"):
            raise AgentBoundaryError("Workspace has no confirmed physical identity")

    def _requires_approval(self, name, parameters):
        if name == "mcp_call":
            _, policy = self.mcp.validate(**parameters)
            return policy.side_effect != "read"
        return name == "run_command" and self.workspace.get("tool_policy", {}).get("command_approval", False)

    def _transaction(self):
        return self.host.worker.transaction()

    async def _broker(self, action, **payload):
        return await self.broker.request(self.workspace["id"], self.workspace["policy_version"], action, **payload)

    async def _record(self, request, parameters, identity):
        from .m4_services import ensure_tool_request
        op_id = _hash({"run_id": self.host.run_id, "invocation": identity.invocation_id})
        snapshot = None
        async with self.host.serial:
            with self._transaction() as db:
                self.host._run(db, "execute", allow_supplements=True)
                if self.host.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
                    from .m4_invalid_audit import claim_valid_preparation
                    claim_valid_preparation(db, self.host, request, request.tool_call["name"])
                existing = db.get(ToolOperation, op_id)
                if existing is not None:
                    if (existing.parameters != parameters or existing.tool_name != request.tool_call["name"]
                            or existing.tool_version != M4_TOOL_VERSION):
                        raise AgentOperationNeedsReview("Tool request parameters changed")
                    return op_id, existing.binding_fingerprint
        if request.tool_call["name"] == "run_command":
            # Copying does not execute arbitrary code or receive original RW mounts.
            from .m4_contract import COLLAB_WORKFLOWS
            if self.host.workflow_version in COLLAB_WORKFLOWS:
                # A sibling's host patch cannot race our initial version copy.
                # This lock does not cover shell execution in its private copy.
                async with self.patch_lock:
                    snapshot = await self._broker("snapshot", run_id=self.host.run_id)
            else:
                snapshot = await self._broker("snapshot", run_id=self.host.run_id)
        binding = _hash({"request": identity.binding_fingerprint, "parameters": parameters,
                         "workspace": self.workspace, "tool_version": M4_TOOL_VERSION,
                         "snapshot": snapshot})
        async with self.host.serial:
            with self._transaction() as db:
                self.host._run(db, "execute", allow_supplements=True)
                operation = ensure_tool_request(
                    db, run_id=self.host.run_id, operation_id=op_id,
                    task_id=identity.task_id, tool_call_id=request.tool_call["id"],
                    tool_name=request.tool_call["name"], tool_version=M4_TOOL_VERSION,
                    parameters=parameters, binding_fingerprint=binding,
                    requires_approval=self._requires_approval(request.tool_call["name"], parameters),
                )
                if snapshot is not None:
                    row = WorkspaceSnapshot(id=snapshot["snapshot_id"], workspace_id=self.workspace["id"],
                                            run_id=self.host.run_id, task_id=identity.task_id,
                                            policy_version=self.workspace["policy_version"], manifest=snapshot,
                                            workdir=snapshot["workdir"])
                    db.add(row)
                    db.flush()
                    operation.snapshot_id = row.id
                if self.host.workflow_version in {"stage-agent-v9", "stage-agent-v10"}:
                    from .review_evidence import v9_request_mac, v9_snapshot_mac
                    run = db.get(Run, self.host.run_id)
                    frozen_snapshot = db.get(WorkspaceSnapshot, operation.snapshot_id) if operation.snapshot_id else None
                    operation.result = {"v9_request_mac": v9_request_mac(self.host.worker.settings, run, operation),
                                        "v9_snapshot_mac": v9_snapshot_mac(self.host.worker.settings, run, operation, frozen_snapshot)}
        return op_id, binding

    async def _authorize(self, op_id, binding):
        from .m4_services import ensure_approval, finish_tool_request
        async with self.host.serial:
            with self._transaction() as db:
                operation = db.get(ToolOperation, op_id)
                if operation.binding_fingerprint != binding:
                    raise AgentOperationNeedsReview("Tool approval binding changed")
                if operation.status in ("completed", "failed") and isinstance(operation.result, dict):
                    return operation.result
                if operation.status in ("dispatched", "running", "uncertain"):
                    raise AgentOperationNeedsReview("Tool execution requires reconciliation")
                requirement = db.scalar(select(RunCommand.id).where(
                    RunCommand.run_id == self.host.run_id, RunCommand.command_type == "append_requirement",
                    RunCommand.status == "pending").limit(1))
                approval = db.scalar(select(Approval).where(Approval.operation_id == op_id))
                if requirement:
                    if approval and approval.status in ("pending", "approved"):
                        approval.status, approval.decision_at = "expired", now()
                    result = {"error": "REQUIREMENTS_CHANGED", "message": "用户补充了要求，请先读取新要求并重新规划工具调用。"}
                    finish_tool_request(db, op_id, "failed", result=result, error_code="REQUIREMENTS_CHANGED")
                    return result
                needs_approval = self._requires_approval(operation.tool_name, operation.parameters)
                if not needs_approval:
                    return None
                approval = ensure_approval(db, operation)
                approval_id, status = approval.id, approval.status
        if status == "pending":
            self.host.dispatch_blocked = True
            await self.scheduler.wait_operations()
            signal = interrupt({"type": "approval", "run_id": self.host.run_id,
                                "approval_id": approval_id, "operation_id": op_id, "fingerprint": binding})
            if not isinstance(signal, dict) or signal.get("approval_id") != approval_id:
                raise AgentOperationNeedsReview("Approval resume identity is invalid")
            await self.host.check_control("tool_calls", "execute", None)
            return await self._authorize(op_id, binding)
        if status != "approved":
            result = {"error": "APPROVAL_REJECTED", "message": "该具体操作未获批准，请使用其他方案或说明阻碍。"}
            async with self.host.serial:
                with self._transaction() as db:
                    finish_tool_request(db, op_id, "failed", result=result, error_code="APPROVAL_REJECTED")
            return result
        return None

    async def invoke(self, request):
        from .m4_services import claim_tool_slot, finish_tool_request
        call = request.tool_call
        name = call.get("name")
        if name == "ask_user" and self.host.workflow_version in {"stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
            return await self._ask_user(request)
        if name not in M4_TOOL_NAMES or name not in self.workspace.get("tool_policy", {}).get("tools", M4_TOOL_NAMES):
            raise AgentBoundaryError("Tool is not granted")
        try:
            schema = (PatchArgumentsV8 if name == "workspace_patch" and self.host.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}
                      else TOOL_SCHEMAS[name][0])
            parameters = schema.model_validate(call.get("args", {})).model_dump(mode="json")
        except ValidationError as exc:
            if self.host.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
                from .m4_invalid_audit import audit_invalid
                return _message(call, await audit_invalid(self.host, request, name, exc))
            message = ("工具参数不符合格式。新建文件可省略 expected_sha256 或使用 JSON null；修改已有文件须先读取并提供准确 SHA-256。"
                       if name == "workspace_patch" and self.host.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}
                       else "工具参数不符合已声明格式。")
            return _message(call, {"error": "INVALID_ARGUMENTS", "message": message})
        task_scope = None
        from .m4_contract import COLLAB_WORKFLOWS
        if self.host.workflow_version in COLLAB_WORKFLOWS:
            from .m4_collaboration import current_task_scope, relative_file
            try:
                task_scope = current_task_scope()
                allowed_paths = task_scope["write_paths"]
                if name == "workspace_patch" and allowed_paths is not None:
                    path = relative_file(parameters["path"])
                    if path.casefold() not in {item.casefold() for item in allowed_paths}:
                        return _message(call, {"error": "TASK_WRITE_SCOPE", "message": "该文件不在当前子任务的写范围内。"})
                if name == "mcp_call" and allowed_paths is not None:
                    _, policy = self.mcp.validate(**parameters)
                    if policy.side_effect != "read":
                        return _message(call, {"error": "TASK_WRITE_SCOPE", "message": "有界文件任务不能执行范围不明的外部写操作。"})
            except (ValueError, RuntimeError, MCPBoundaryError):
                return _message(call, {"error": "TASK_WRITE_SCOPE", "message": "当前子任务的写边界无法验证。"})
        if name == "mcp_call":
            try:
                self.mcp.validate(**parameters)
            except MCPBoundaryError:
                return _message(call, {"error": "MCP_PERMISSION_DENIED", "message": "MCP 工具未获本地授权，或参数/配置版本已改变。"})
        await self.host.check_control("tool_calls", "execute", name)
        if self.host.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
            from .m4_invalid_audit import guard_valid_invocation
            await guard_valid_invocation(self.host, request, name)
        identity = invocation_identity(request, kind="tool_calls", role="execute", tool_name=name)
        op_id, binding = await self._record(request, parameters, identity)
        identity = replace(identity, binding_fingerprint=binding)
        saved = await self.host.replay_invocation("tool_calls", "execute", name, identity)
        if saved is not None:
            async with self.host.serial:
                with self._transaction() as db:
                    finish_tool_request(db, op_id, "failed" if saved.get("error") else "completed", result=saved,
                                        error_code=saved.get("error"))
            return _message(call, saved)
        authorized_result = await self._authorize(op_id, binding)
        if authorized_result is not None:
            return _message(call, authorized_result)
        def prepare(db):
            if self.host.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
                from .m4_invalid_audit import check_valid_reservation
                check_valid_reservation(db, self.host, request, name, require_claim=True)
            if not claim_tool_slot(db, self.host.run_id, op_id):
                raise ToolSlotUnavailable

        def on_reserved(db, ledger):
            operation = db.get(ToolOperation, op_id)
            if self.host.workflow_version in {"stage-agent-v9", "stage-agent-v10"}:
                import hmac

                from .review_evidence import v9_request_mac, v9_snapshot_mac
                run = db.get(Run, self.host.run_id)
                frozen_snapshot = db.get(WorkspaceSnapshot, operation.snapshot_id) if operation.snapshot_id else None
                saved = operation.result if isinstance(operation.result, dict) else {}
                expected = v9_request_mac(self.host.worker.settings, run, operation)
                expected_snapshot = v9_snapshot_mac(self.host.worker.settings, run, operation, frozen_snapshot)
                if (operation.parameters != parameters or not isinstance(saved.get("v9_request_mac"), str)
                        or not isinstance(saved.get("v9_snapshot_mac"), str)
                        or not hmac.compare_digest(saved["v9_request_mac"], expected)
                        or not hmac.compare_digest(saved["v9_snapshot_mac"], expected_snapshot)):
                    raise AgentOperationNeedsReview("Tool request evidence binding differs")
                ledger.result = {**(ledger.result or {}), "v9_request_mac": expected,
                                 "v9_snapshot_mac": expected_snapshot}
            operation.operation_record_id = ledger.operation_id
            operation.status, operation.started_at = "dispatched", now()
            correction = getattr(self.host, "_v10_correction_binding", None)
            if correction is not None:
                from .correction_gateway_binding import CorrectionGatewayBinding
                if type(correction) is not CorrectionGatewayBinding or correction.host is not self.host:
                    raise AgentOperationNeedsReview("Correction gateway binding differs")
                correction.mark_dispatched(db, request, identity, operation)

        # The slot, shared budget and execution ledger commit in one fenced transaction.
        while True:
            await self.host.check_control("tool_calls", "execute", name)
            try:
                allowed = await self.host.reserve_invocation("tool_calls", "execute", name, identity,
                                                             prepare=prepare, on_reserved=on_reserved)
                if not allowed:
                    raise AgentBudgetExceeded("Run tool budget exhausted")
                break
            except ToolSlotUnavailable:
                pass
            await asyncio.sleep(0.05)

        async def execute_and_save():
            try:
                if name == "workspace_patch":
                    async with self.patch_lock:
                        result = await self._execute(op_id, name, parameters)
                else:
                    result = await self._execute(op_id, name, parameters)
                if task_scope is not None:
                    result["host_task_id"] = task_scope["id"]

                def save(db):
                    payloads = result.pop("_artifact_payloads", [])
                    references = [self.artifacts.put(
                        db, run_id=self.host.run_id, operation_id=op_id, workspace_id=self.workspace["id"], **payload,
                    ) for payload in payloads]
                    result["artifacts"] = references
                    row = finish_tool_request(db, op_id, "failed" if result.get("error") else "completed",
                                              result=result, error_code=result.get("error"))
                    if name != "run_command":
                        row.slot_claimed, row.reclaimed_at = False, now()
                    return result
                return await self.host.finish_invocation("tool_calls", "execute", name, identity, result_factory=save)
            except BaseException:
                try:
                    await self.host.mark_invocation_uncertain("tool_calls", "execute", name, identity)
                    async with self.host.serial:
                        with self._transaction() as db:
                            finish_tool_request(db, op_id, "uncertain", error_code="RESULT_UNKNOWN")
                except BaseException:
                    pass  # Ownership loss leaves the durable dispatch for reconciliation.
                raise
        return _message(call, await self.scheduler.run_tracked(execute_and_save))

    async def _ask_user(self, request):
        """Interrupt inside the child graph so already-finished tool nodes stay checkpointed."""
        from .clarification_service import create_clarification
        from .m3_budget import ReservationResult, reserve_invocation
        from .m4_collaboration import current_task_scope
        from .models import Clarification
        call = request.tool_call
        try:
            params = AskUserArguments.model_validate(call.get("args", {})).model_dump(mode="json")
        except ValidationError as exc:
            if self.host.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
                from .m4_invalid_audit import audit_invalid
                return _message(call, await audit_invalid(self.host, request, "ask_user", exc))
            return _message(call, {"error": "INVALID_ARGUMENTS", "message": "提问参数或子任务范围无效。"})
        try:
            scope = current_task_scope()
        except (ValueError, RuntimeError):
            return _message(call, {"error": "INVALID_ARGUMENTS", "message": "提问参数或子任务范围无效。"})
        await self.host.check_control("tool_calls", "execute", "ask_user")
        if self.host.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
            from .m4_invalid_audit import guard_valid_invocation
            await guard_valid_invocation(self.host, request, "ask_user")
        identity = invocation_identity(request, kind="tool_calls", role="execute", tool_name="ask_user")
        self.host._verify_identity(identity)
        async with self.host.serial:
            with self._transaction() as db:
                run, role = self.host._run(db, "execute", allow_supplements=True)
                if self.host.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
                    from .m4_invalid_audit import (
                        assert_not_limited,
                        check_valid_reservation,
                        claim_valid_preparation,
                    )
                    assert_not_limited(db, run.id, self.host.workflow_version)
                    claim_valid_preparation(db, self.host, request, "ask_user")
                    check_valid_reservation(db, self.host, request, "ask_user", require_claim=True)
                binding = self.host._binding(identity, role, self.host.plugin_versions,
                                             "tool_calls", "execute", "ask_user")
                key = "tool:" + identity.invocation_id
                previous = db.scalar(select(Clarification.id).where(
                    Clarification.run_id == run.id, Clarification.request_key == key))
                reservation = reserve_invocation(db, run_id=run.id, kind="tool_calls",
                    invocation_id=identity.invocation_id, binding_fingerprint=binding)
                if reservation == ReservationResult.EXHAUSTED:
                    raise AgentBudgetExceeded("Run tool budget exhausted")
                if (reservation == ReservationResult.ALREADY_RESERVED) != (previous is not None):
                    raise AgentOperationNeedsReview("Question reservation differs from its durable record")
                row = create_clarification(db, run, key, scope["id"],
                                           params["question"], params["reason"], params["options"], stage="execute")
                question_id = row.id
        self.host.dispatch_blocked = True
        await self.scheduler.wait_operations()
        from .interrupt_timing import user_interrupt
        signal = interrupt(user_interrupt(self.host, kind="user_clarification",
                                          clarification_id=question_id))
        if (not isinstance(signal, dict) or signal.get("clarification_id") != question_id
                or not isinstance(signal.get("answer"), str) or not signal["answer"].strip()):
            raise AgentOperationNeedsReview("Clarification resume identity is invalid")
        if self.host.workflow_version in {"stage-agent-v9", "stage-agent-v10"}:
            from .clarification_context import verify_clarification_signal
            async with self.host.serial:
                with self._transaction() as db:
                    self.host._run(db, "execute", allow_supplements=True)
                    verify_clarification_signal(db, self.host.run_id, question_id, signal["answer"],
                                                question=params["question"])
        return _message(call, {"answer": signal["answer"]})

    async def _execute(self, op_id, name, parameters):
        if name == "artifact_read":
            try:
                with self._transaction() as db:
                    row = db.get(ArtifactVersion, parameters["artifact_id"])
                    if row is None:
                        return {"error": "ARTIFACT_NOT_FOUND"}
                    content, end = self.artifacts.read_text_page(row, run_id=self.host.run_id,
                                               offset=parameters["offset"], limit=parameters["limit"])
                    return {"artifact_id": row.id, "sha256": row.sha256,
                            "content": content, "offset": parameters["offset"],
                            "next_offset": end if end < row.size_bytes else None, "size_bytes": row.size_bytes}
            except (ServiceError, OSError) as exc:
                return {"error": getattr(exc, "code", "ARTIFACT_UNAVAILABLE")}
        if name == "run_command":
            return await self._command(op_id, parameters)
        if name == "mcp_call":
            try:
                result = await self.mcp.execute(**parameters)
            except MCPResultUnknown:
                raise AgentOperationNeedsReview("MCP operation result is uncertain") from None
            except MCPBoundaryError:
                return {"error": "MCP_NOT_DISPATCHED", "message": "MCP 连接或参数核对失败，未派发实际操作。"}
            encoded = json.dumps(result, ensure_ascii=False)
            artifact = {"kind": "mcp_result", "name": "mcp-result.json", "content": encoded}
            if len(encoded) <= 4000:
                return {**result, "_artifact_payloads": [artifact]}
            return {"preview": encoded[:4000], "truncated": True, "_artifact_payloads": [artifact]}
        try:
            arguments = dict(parameters)
            if name == "workspace_read":
                arguments.pop("offset")
                arguments.pop("limit")
            if name == "workspace_patch":
                arguments.update(operation_id=op_id, run_id=self.host.run_id)
            result = await self._broker(name.removeprefix("workspace_"), **arguments)
        except (WorkspaceError, ServiceError) as exc:
            if exc.code.lower() in ("native_result_unknown", "write_result_unknown", "operation_exists",
                                    "broker_result_unknown", "workspace_native_result_unknown",
                                    "workspace_write_result_unknown", "workspace_operation_exists"):
                raise AgentOperationNeedsReview("Workspace write result is unknown") from None
            return {"error": exc.code, "message": exc.message}
        artifact = {"kind": "tool_result", "name": f"{name}.json", "content": json.dumps(result, ensure_ascii=False)}
        if name == "workspace_read":
            content = result["content"]
            start, limit = parameters["offset"], parameters["limit"]
            end = min(len(content), start + limit)
            return {**result, "content": content[start:end], "offset": start, "total_characters": len(content),
                    "next_offset": end if end < len(content) else None, "_artifact_payloads": [artifact]}
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded) <= 4000:
            return {**result, "_artifact_payloads": [artifact]}
        # Full evidence remains immutable; model only sees a bounded preview.
        return {"preview": encoded[:4000], "truncated": True, "_artifact_payloads": [artifact],
                "sha256": result.get("sha256"), "path": result.get("path")}

    async def _command(self, op_id, parameters):
        async with self.host.serial:
            with self._transaction() as db:
                operation = db.get(ToolOperation, op_id)
                snapshot = db.get(WorkspaceSnapshot, operation.snapshot_id)
                if snapshot is None:
                    raise AgentBoundaryError("Command has no frozen snapshot")
                snapshot_path, snapshot_id = snapshot.workdir, snapshot.id
                fingerprint = operation.binding_fingerprint
        log_dir = Path(self.host.worker.settings.data_dir) / "tool-logs" / op_id
        log_dir.mkdir(parents=True, exist_ok=True)
        bound_sandbox_id = None

        async def log_sink(channel, content):
            with (log_dir / f"{channel}.log").open("ab") as stream:
                stream.write(content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())

        async def created(sandbox_id):
            nonlocal bound_sandbox_id
            async with self.host.serial:
                with self._transaction() as db:
                    row = db.get(ToolOperation, op_id)
                    if row.sandbox_id not in (None, sandbox_id) or row.binding_fingerprint != fingerprint:
                        raise AgentBoundaryError("Sandbox creation identity differs")
                    row.sandbox_id = sandbox_id
            bound_sandbox_id = sandbox_id

        async def started(sandbox_id, command_id):
            async with self.host.serial:
                with self._transaction() as db:
                    row = db.get(ToolOperation, op_id)
                    row.sandbox_id, row.command_id, row.status = sandbox_id, command_id, "running"

        async def reclaimed(sandbox_id):
            async with self.host.serial:
                with self._transaction() as db:
                    row = db.get(ToolOperation, op_id)
                    if row.sandbox_id != sandbox_id:
                        raise AgentBoundaryError("Sandbox reclamation identity differs")
                    row.reclaimed_at, row.slot_claimed = now(), False

        result, error = None, None
        try:
            execution_options = {}
            if self.workspace.get("creation_service") is not None:
                if getattr(self.sandbox, "creation_client", None) is None:
                    raise SandboxBoundaryError("Frozen creation service is required")

                async def dispatch_guard():
                    from .sandbox_creation_api import CreationRequest
                    from .sandbox_creation_authorization import authorize_command_dispatch
                    request = CreationRequest(run_id=self.host.run_id, operation_id=op_id,
                        operation_fingerprint=fingerprint, snapshot_path=snapshot_path,
                        timeout_seconds=parameters["timeout_seconds"])
                    async with self.host.serial:
                        with self._transaction() as db:
                            self.host._run(db, "execute")
                            authorize_command_dispatch(db, self.sandbox.config, request, bound_sandbox_id,
                                                       service_binding=self.workspace["creation_service"])

                execution_options = {"operation_fingerprint": fingerprint, "dispatch_guard": dispatch_guard}
            result = await self.sandbox.execute(
                run_id=self.host.run_id, operation_id=op_id, snapshot_path=snapshot_path,
                **parameters, log_sink=log_sink, started_sink=started, created_sink=created,
                reclaimed_sink=reclaimed, **execution_options,
            )
        except (SandboxBoundaryError, asyncio.CancelledError) as exc:
            error = exc
        artifacts = [{"kind": "command_log", "name": f"{channel}.log", "content": path.read_bytes(),
                      "metadata": {"snapshot_id": snapshot_id, "complete": error is None}}
                     for channel in ("stdout", "stderr") if (path := log_dir / f"{channel}.log").exists()]
        if error:
            async with self.host.serial:
                with self._transaction() as db:
                    for payload in artifacts:
                        self.artifacts.put(db, run_id=self.host.run_id, operation_id=op_id,
                                           workspace_id=self.workspace["id"], **payload)
            raise error
        return {**asdict(result), "snapshot_id": snapshot_id, "_artifact_payloads": artifacts,
                **({"error": "COMMAND_FAILED"} if result.exit_code else {})}
