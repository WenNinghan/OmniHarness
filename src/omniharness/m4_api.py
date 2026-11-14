"""Paired workspace/evidence API and separately authenticated broker grants."""
import hmac

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import Response
from sqlalchemy import select

from .artifacts import ArtifactStore
from .m4_reconciliation import ReconciliationRequest, reconcile_tool
from .m4_services import (
    approval_output,
    artifact_output,
    decide_approval,
    record_workspace_probe,
    register_workspace,
    tool_output,
    validate_workspace_lease,
    workspace_grant,
    workspace_output,
)
from .model_usage import model_binding, normalize_usage
from .models import Approval, ArtifactVersion, OperationRecord, Run, ToolOperation, Workspace
from .schemas import ApprovalDecisionRequest, WorkspaceEnabledRequest, WorkspaceRequest
from .services import ServiceError, api_value, require
from .workspace_client import BrokerClient, read_service_key


def create_tools_router(factory, settings):
    router = APIRouter(prefix="/api/v1")
    store = ArtifactStore(settings.data_dir / "artifacts")

    @router.get("/tools/status")
    async def status():
        ready = False
        if settings.enable_tools:
            try:
                await BrokerClient(settings).health()
                ready = True
            except (ValueError, OSError, ServiceError):
                pass
        return {"enabled": settings.enable_tools, "broker_ready": ready,
                "workspace_mode": "controlled_patch", "commands": "snapshot_only", "network": "deny"}

    @router.get("/workspaces")
    def workspaces():
        with factory() as db:
            return [workspace_output(db, row) for row in db.scalars(select(Workspace).order_by(Workspace.created_at))]

    @router.post("/workspaces", status_code=201)
    async def create_workspace(body: WorkspaceRequest):
        # Persist the pending grant first so the independent broker can fetch it.
        with factory.begin() as db:
            row = register_workspace(db, body, settings)
            identity, version = row.id, row.policy_version
        try:
            result = await BrokerClient(settings).request(identity, version, "probe")
            with factory.begin() as db:
                return record_workspace_probe(db, identity, result)
        except (ValueError, OSError, ServiceError):
            with factory.begin() as db:
                row = require(db, Workspace, identity)
                row.enabled, row.probe = False, {"status": "failed"}
            raise ServiceError("WORKSPACE_PROBE_FAILED", "目录探测未通过；已保留停用记录，请检查路径与宿主服务。", 409) from None

    @router.post("/workspaces/{identity}/probe")
    async def probe(identity: str):
        with factory() as db:
            row = require(db, Workspace, identity)
            version = row.policy_version
            if not row.enabled:
                raise ServiceError("WORKSPACE_DISABLED", "请先启用该工作区。", 409)
        result = await BrokerClient(settings).request(identity, version, "probe")
        with factory.begin() as db:
            return record_workspace_probe(db, identity, result)

    @router.patch("/workspaces/{identity}")
    def enabled(identity: str, body: WorkspaceEnabledRequest):
        with factory.begin() as db:
            row = require(db, Workspace, identity)
            row.enabled = body.enabled
            return workspace_output(db, row)

    @router.get("/workspaces/{identity}/files")
    async def files(identity: str, path: str = "."):
        with factory() as db:
            row = require(db, Workspace, identity)
            version = row.policy_version
        return await BrokerClient(settings).request(identity, version, "list", path=path)

    @router.get("/runs/{identity}/tools")
    def tools(identity: str):
        with factory() as db:
            require(db, Run, identity)
            return [tool_output(row) for row in db.scalars(select(ToolOperation).where(
                ToolOperation.run_id == identity).order_by(ToolOperation.created_at, ToolOperation.id))]

    @router.get("/runs/{identity}/operations")
    def operations(identity: str):
        with factory() as db:
            run = require(db, Run, identity)
            result = []
            for row in db.scalars(select(OperationRecord).where(OperationRecord.run_id == identity)
                                  .order_by(OperationRecord.created_at)):
                evidence = row.result or {}
                response = evidence.get("response") or {}
                node_parts = row.node_id.split(":")
                operation_kind = node_parts[2] if len(node_parts) > 2 and node_parts[0] == "agent" else None
                binding = evidence.get("model_binding")
                if not isinstance(binding, dict) and operation_kind == "model_calls":
                    # Legacy records have a host-created node role. New records
                    # carry the explicit frozen binding, including uncertain calls.
                    role = row.node_id.split(":")[1] if row.node_id.startswith("agent:") else None
                    frozen = ((run.config_snapshot.get("agent") or {}).get("roles") or {}).get(role, {})
                    binding = model_binding(frozen.get("model"))
                purpose = evidence.get("purpose", "task")
                recovery_of = None
                summary_metadata = {}
                messages = response.get("messages") or ([response["message"]] if "message" in response else [])
                usages = [item.get("usage_metadata") for item in messages if isinstance(item, dict)]
                if response.get("artifact_id"):
                    artifact = db.get(ArtifactVersion, response["artifact_id"])
                    if artifact and artifact.run_id == identity and artifact.kind == "context_summary":
                        usages = [artifact.metadata_json.get("usage")]
                        purpose = "context_summary"
                        recovery_of = artifact.metadata_json.get("recovery_of")
                        summary_metadata = {key: artifact.metadata_json.get(key)
                                            for key in ("output_status", "attempt")}
                        summary_metadata["artifact_id"] = artifact.id
                safe_usages = [{key: value for key, value in usage.items()
                               if key in {"input_tokens", "output_tokens", "total_tokens"}
                               and type(value) is int and value >= 0} if isinstance(usage, dict) else None
                              for usage in usages]
                result.append({"operation_id": row.operation_id, "node_id": row.node_id,
                    "operation_kind": operation_kind,
                    "status": row.status, "workflow_version": row.workflow_version, "error_code": row.error_code,
                    "created_at": api_value(row.created_at), "started_at": api_value(row.started_at),
                    "finished_at": api_value(row.finished_at), "usage": safe_usages,
                    "model_binding": binding,
                    "normalized_usage": [normalize_usage(item) for item in usages],
                    "purpose": purpose, "recovery_of": recovery_of, **summary_metadata})
            return result

    @router.get("/runs/{identity}/approvals")
    def approvals(identity: str):
        with factory() as db:
            require(db, Run, identity)
            return [approval_output(row, operation) for row, operation in db.execute(
                select(Approval, ToolOperation).join(ToolOperation).where(ToolOperation.run_id == identity)
                .order_by(Approval.requested_at))]

    @router.post("/approvals/{identity}/decision", status_code=202)
    def decision(identity: str, body: ApprovalDecisionRequest):
        with factory.begin() as db:
            return decide_approval(db, identity, body)

    @router.post("/tool-operations/{identity}/reconcile")
    async def reconcile(identity: str, body: ReconciliationRequest):
        return await reconcile_tool(factory, settings, identity, body)

    @router.get("/runs/{identity}/artifacts")
    def artifacts(identity: str):
        with factory() as db:
            require(db, Run, identity)
            return [artifact_output(row) for row in db.scalars(select(ArtifactVersion).where(
                ArtifactVersion.run_id == identity).order_by(ArtifactVersion.created_at))]

    @router.get("/runs/{identity}/evidence-view")
    def evidence_view(identity: str, cursor: str | None = Query(default=None, max_length=8192),
                      limit: int = Query(default=25, ge=1, le=100)):
        from .evidence_view import evidence_page

        # The containing router inherits the local session authentication in api.py.
        with factory.begin() as db:
            return evidence_page(db, settings, identity, cursor=cursor, limit=limit)

    @router.get("/runs/{identity}/artifacts/{artifact_id}/content")
    def run_artifact(identity: str, artifact_id: str, offset: int = Query(default=0, ge=0),
                     limit: int = Query(default=65536, ge=1, le=1024 * 1024)):
        with factory() as db:
            run = db.get(Run, identity)
            row = db.get(ArtifactVersion, artifact_id)
            permitted_kinds = {"task_result", "command_log", "tool_result", "mcp_result",
                               "review_reference_report_v1", "review_model_result_v1"}
            if run is not None and run.workflow_version in {"stage-agent-v9", "stage-agent-v10"}:
                permitted_kinds.update({"review_reference_report_v2", "review_model_result_v2"})
            if (run is None or row is None or not run.workspace_id or row.run_id != run.id
                    or row.workspace_id != run.workspace_id or row.kind not in permitted_kinds):
                # Do not disclose whether an out-of-scope artifact exists.
                raise ServiceError("NOT_FOUND", "工件不存在或不可访问。", 404)
            if row.operation_id:
                operation = db.get(ToolOperation, row.operation_id)
                if operation is None or operation.run_id != run.id:
                    raise ServiceError("NOT_FOUND", "工件不存在或不可访问。", 404)
            try:
                data, next_offset = store.read_text_page(row, run_id=run.id, offset=offset, limit=limit)
            except OSError:
                raise ServiceError("EVIDENCE_UNAVAILABLE", "工件当前不可读取。", 409) from None
            return Response(data, media_type="text/plain; charset=utf-8", headers={
                "X-Artifact-SHA256": row.sha256, "X-Artifact-Size": str(row.size_bytes),
                "X-Artifact-Next-Offset": str(next_offset),
            })

    @router.get("/artifacts/{identity}/content")
    def artifact(identity: str, offset: int = Query(default=0, ge=0),
                 limit: int = Query(default=65536, ge=1, le=1024 * 1024)):
        with factory() as db:
            row = require(db, ArtifactVersion, identity)
            data, next_offset = store.read_text_page(row, offset=offset, limit=limit)
            return Response(data, media_type="text/plain; charset=utf-8", headers={
                "X-Artifact-SHA256": row.sha256, "X-Artifact-Size": str(row.size_bytes),
                "X-Artifact-Next-Offset": str(next_offset),
            })

    return router


def create_broker_grants_router(factory, settings):
    def service_auth(authorization: str | None = Header(default=None),
                     x_omni_instance: str | None = Header(default=None)):
        try:
            expected = "Bearer " + read_service_key(settings.workspace_service_key_file)
        except (ValueError, OSError):
            raise ServiceError("SERVICE_UNAVAILABLE", "宿主服务尚未配置。", 503) from None
        if (not authorization or not hmac.compare_digest(authorization, expected)
                or x_omni_instance != settings.instance_id):
            raise ServiceError("SERVICE_AUTH_REQUIRED", "需要独立的宿主服务认证。", 401)

    router = APIRouter(prefix="/api/internal/workspaces", dependencies=[Depends(service_auth)])

    @router.get("/{identity}/grant/{version}")
    def grant(identity: str, version: int):
        with factory() as db:
            return workspace_grant(db, identity, version, settings.instance_id)

    @router.get("/{identity}/lease/{run_id}")
    def lease(identity: str, run_id: str):
        with factory() as db:
            return {"allowed": validate_workspace_lease(db, identity, run_id)}

    return router
