"""Private file broker: an injected registry/IO adapter and a separate service key.

Only the application configuration API registers grants. The tool-facing broker
cannot register roots, change policy, choose interpreters, or run host commands.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import stat
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from omniharness.workspace_io import NativeWorkspaceIO, WorkspaceError, WorkspaceGrant, WorkspaceIO

_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}")


class GrantPayload(BaseModel):
    """The internal grant API is authoritative, but its response is still validated."""

    model_config = ConfigDict(extra="forbid", strict=True)
    workspace_id: str
    instance_id: str
    root: str
    version: int = Field(ge=1)
    read_allow: list[str]
    write_allow: list[str]
    excludes: list[str]
    enabled: bool
    expected_identity: dict | None

    def grant(self) -> WorkspaceGrant:
        values = self.model_dump()
        for field in ("read_allow", "write_allow", "excludes"):
            values[field] = tuple(values[field])
        return WorkspaceGrant(**values)


class LeasePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    allowed: bool


def _private_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("The trusted application URL must be an explicit origin")
    return value.rstrip("/")


class HttpWorkspaceRegistry:
    """No database access, caching, redirects, implicit retries or ambient credentials."""

    def __init__(
        self,
        application_url: str,
        *,
        instance_id: str,
        service_key: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self.origin = _private_origin(application_url)
        if not _IDENTITY.fullmatch(instance_id) or len(service_key) < 32:
            raise ValueError("An explicit instance and independent service credential are required")
        self.instance_id = instance_id
        self.client = httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(5),
            transport=transport,
            headers={"Authorization": "Bearer " + service_key, "X-Omni-Instance": instance_id},
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, suffix: str, *, missing_ok: bool = False) -> dict | None:
        try:
            with self.client.stream("GET", self.origin + "/api/internal/workspaces/" + suffix) as response:
                if response.status_code == 404 and missing_ok:
                    return None
                if response.status_code != 200:
                    raise WorkspaceError(
                        "registry_unavailable", "The trusted workspace registry rejected the request."
                    )
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > 65536:
                        raise WorkspaceError(
                            "registry_unavailable", "The workspace grant response is invalid."
                        )
                    content.extend(chunk)
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("Invalid grant response")
                return result
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise WorkspaceError(
                "registry_unavailable", "The trusted workspace registry is unavailable."
            ) from None

    def get(self, workspace_id: str, policy_version: int) -> WorkspaceGrant | None:
        if (
            not _IDENTITY.fullmatch(workspace_id)
            or isinstance(policy_version, bool)
            or not isinstance(policy_version, int)
            or policy_version < 1
        ):
            raise WorkspaceError("invalid_policy", "Invalid workspace identity or policy version.")
        payload = self._request(f"{workspace_id}/grant/{policy_version}", missing_ok=True)
        if payload is None:
            return None
        try:
            validated = GrantPayload.model_validate(payload)
        except ValidationError:
            raise WorkspaceError("registry_unavailable", "The workspace grant response is invalid.") from None
        if (
            validated.workspace_id != workspace_id
            or validated.version != policy_version
            or validated.instance_id != self.instance_id
        ):
            raise WorkspaceError("registry_unavailable", "The workspace grant does not match this request.")
        return validated.grant()

    def require_write_lease(self, instance_id: str, workspace_id: str, run_id: str) -> None:
        if (
            instance_id != self.instance_id
            or not _IDENTITY.fullmatch(workspace_id)
            or not _IDENTITY.fullmatch(run_id)
        ):
            raise WorkspaceError("lease_required", "A valid workspace write lease is required.")
        payload = self._request(f"{workspace_id}/lease/{run_id}")
        try:
            allowed = LeasePayload.model_validate(payload).allowed
        except ValidationError:
            raise WorkspaceError("registry_unavailable", "The workspace lease response is invalid.") from None
        if not allowed:
            raise WorkspaceError("lease_required", "This run does not hold the workspace write lease.")


class BrokerDeploymentProfile(BaseModel):
    """Trusted startup file, never accepted in a broker/tool HTTP request."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    instance_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    application_url: str
    service_key_file: str
    python_executable: str
    module_file: str
    framework_root: str
    native_storage_root: str
    native_protected_roots: list[str] = Field(min_length=1)
    mapped_native_storage_root: str
    snapshot_root: str
    bind_host: str = "172.17.0.1"
    port: int = Field(default=19092, ge=1024, le=65535)

    @field_validator("application_url")
    @classmethod
    def origin(cls, value: str) -> str:
        result = _private_origin(value)
        if urlsplit(result).hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("The WSL broker reads grants only from the local application")
        return result

    @field_validator("service_key_file", "python_executable", "mapped_native_storage_root", "snapshot_root")
    @classmethod
    def absolute_linux_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or "\\" in value
            or "\x00" in value
            or ".." in value.split("/")
            or PurePosixPath(value) == PurePosixPath("/")
        ):
            raise ValueError("An explicit non-root Linux path is required")
        return value

    @field_validator("framework_root", "native_storage_root", "module_file")
    @classmethod
    def absolute_windows_path(cls, value: str) -> str:
        path = PureWindowsPath(value)
        if (
            not path.is_absolute()
            or not re.fullmatch(r"[A-Za-z]:", path.drive)
            or ".." in path.parts
            or len(path.parts) < 2
            or "\x00" in value
        ):
            raise ValueError("An explicit drive-backed Windows path is required")
        return value

    @model_validator(mode="after")
    def fixed_boundaries(self):
        protected = [PureWindowsPath(self.absolute_windows_path(p)) for p in self.native_protected_roots]
        for path in (PureWindowsPath(self.framework_root), PureWindowsPath(self.native_storage_root)):
            if not any(root == path or root in path.parents for root in protected):
                raise ValueError("The framework and private storage must remain protected")
        if PureWindowsPath(self.framework_root) not in PureWindowsPath(self.module_file).parents:
            raise ValueError("The fixed native helper must belong to the protected framework")
        if not self.python_executable.endswith("/python.exe") or not self.module_file.endswith(
            "workspace_io.py"
        ):
            raise ValueError("The deployment must pin the native Python helper")
        if not self.snapshot_root.startswith("/var/lib/"):
            raise ValueError("Snapshots must use the WSL private filesystem, not a mounted host drive")
        native_storage = PureWindowsPath(self.native_storage_root)
        mapped_storage = PurePosixPath("/mnt", native_storage.drive[0].lower(), *native_storage.parts[1:])
        if PurePosixPath(self.mapped_native_storage_root) != mapped_storage:
            raise ValueError("The native private root and WSL mapping must name the same directory")
        if self.bind_host not in ("127.0.0.1", "172.17.0.1"):
            raise ValueError("Broker may bind only loopback or the dedicated Docker bridge")
        return self


def create_deployment_app(profile_file: str | None = None) -> FastAPI:
    """Uvicorn factory for the WSL host broker, configured before listening."""
    filename = profile_file or os.environ.get("OMNI_BROKER_PROFILE")
    if not filename or not Path(filename).is_absolute():
        raise ValueError("OMNI_BROKER_PROFILE must name an absolute trusted startup file")
    profile = BrokerDeploymentProfile.model_validate_json(Path(filename).read_text(encoding="utf-8"))
    if os.name != "posix" or not Path(profile.python_executable).is_file():
        raise ValueError("The fixed WSL-to-Windows Python helper is unavailable")
    private_root = Path(profile.snapshot_root)
    private_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    private_stat = private_root.stat()
    if (
        private_root.resolve() != private_root
        or private_stat.st_uid != os.geteuid()
        or stat.S_IMODE(private_stat.st_mode) & 0o077
    ):
        raise ValueError("The snapshot root is not owned by this trusted broker")
    if not Path(profile.mapped_native_storage_root).is_dir():
        raise ValueError("The native private storage has not been provisioned")
    key = Path(profile.service_key_file).read_text(encoding="utf-8").strip()
    registry = HttpWorkspaceRegistry(
        profile.application_url, instance_id=profile.instance_id, service_key=key
    )
    io = NativeWorkspaceIO(
        registry,
        python_executable=profile.python_executable,
        module_file=profile.module_file,
        native_storage_root=profile.native_storage_root,
        native_protected_roots=tuple(profile.native_protected_roots),
        mapped_native_storage_root=Path(profile.mapped_native_storage_root),
        snapshot_root=private_root,
        require_write_lease=registry.require_write_lease,
    )

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            registry.close()

    app = create_broker_app(io, instance_id=profile.instance_id, service_key=key, lifespan=lifespan)
    app.state.deployment_profile = profile
    return app


class WorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version: int = Field(ge=1)


class PathRequest(WorkspaceRequest):
    path: str = Field(min_length=1, max_length=4096)


class ListRequest(WorkspaceRequest):
    path: str = Field(default=".", min_length=1, max_length=4096)


class SearchRequest(WorkspaceRequest):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=100, ge=1, le=1000)


class PatchRequest(PathRequest):
    content: str = Field(max_length=16 * 1024 * 1024)
    expected_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class SnapshotRequest(WorkspaceRequest):
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class OperationRequest(SnapshotRequest):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class RollbackRequest(OperationRequest):
    expected_current_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def create_broker_app(
    io: WorkspaceIO | NativeWorkspaceIO,
    *,
    instance_id: str,
    service_key: str,
    lifespan=None,
) -> FastAPI:
    if not instance_id or len(service_key) < 32:
        raise ValueError(
            "Broker requires an instance ID and an independent service key of at least 32 characters."
        )

    def authenticate(
        authorization: Annotated[str | None, Header()] = None,
        x_omni_instance: Annotated[str | None, Header()] = None,
    ) -> None:
        if not authorization or not hmac.compare_digest(authorization, "Bearer " + service_key):
            raise WorkspaceError("unauthorized", "Broker authentication required.")
        if x_omni_instance != instance_id:
            raise WorkspaceError("instance_mismatch", "Broker belongs to another instance.")

    app = FastAPI(
        title="OmniHarness Workspace Broker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(authenticate)],
        lifespan=lifespan,
    )

    @app.exception_handler(WorkspaceError)
    async def workspace_error(_request: Request, exc: WorkspaceError):
        status = {
            "unauthorized": 401,
            "instance_mismatch": 403,
            "not_found": 404,
            "version_conflict": 409,
            "policy_conflict": 409,
            "operation_exists": 409,
            "already_rolled_back": 409,
            "recovery_corrupt": 409,
            "recovery_incomplete": 409,
            "file_busy": 409,
            "native_helper_unavailable": 503,
            "native_result_unknown": 409,
            "registry_unavailable": 503,
            "lease_required": 409,
        }.get(exc.code, 403)
        return JSONResponse(status_code=status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "instance_id": instance_id,
            "service": "workspace-broker",
            "schema_version": 1,
        }

    @app.post("/workspaces/{workspace_id}/probe")
    def probe(workspace_id: str, body: WorkspaceRequest):
        return io.probe(workspace_id, instance_id, body.policy_version)

    @app.post("/workspaces/{workspace_id}/read")
    def read(workspace_id: str, body: PathRequest):
        return io.read(workspace_id, instance_id, body.policy_version, body.path)

    @app.post("/workspaces/{workspace_id}/list")
    def listing(workspace_id: str, body: ListRequest):
        return io.list(workspace_id, instance_id, body.policy_version, body.path)

    @app.post("/workspaces/{workspace_id}/search")
    def search(workspace_id: str, body: SearchRequest):
        return io.search(workspace_id, instance_id, body.policy_version, body.query, body.limit)

    @app.post("/workspaces/{workspace_id}/patch")
    def patch(workspace_id: str, body: PatchRequest):
        return io.patch(workspace_id, instance_id, **body.model_dump())

    @app.post("/workspaces/{workspace_id}/snapshot")
    def snapshot(workspace_id: str, body: SnapshotRequest):
        return io.snapshot(workspace_id, instance_id, body.policy_version, body.run_id)

    @app.post("/workspaces/{workspace_id}/operation-result")
    def operation_result(workspace_id: str, body: OperationRequest):
        return io.operation_result(workspace_id, instance_id, **body.model_dump())

    @app.post("/workspaces/{workspace_id}/rollback")
    def rollback(workspace_id: str, body: RollbackRequest):
        return io.rollback(workspace_id, instance_id, **body.model_dump())

    return app
