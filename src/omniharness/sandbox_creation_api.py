"""Authenticated creation API; production assembly lives in creation_service.

This service creates and reclaims resources; it never runs a workload command
or changes a tool slot. Authorization and evidence dependencies are explicit.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .sandbox import SandboxBoundaryError, SandboxConfig, SandboxExecutor
from .sandbox_creation_backend import SandboxCreationBackend
from .sandbox_creation_journal import CreationBinding, CreationBindingError, CreationJournal
from .sandbox_creation_supervisor import CreationAlreadyDispatched, CreationSupervisor


class CreationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    operation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_path: str = Field(min_length=1, max_length=4096)
    execd_token: SecretStr | None = None
    timeout_seconds: int = Field(strict=True, ge=1, le=600)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def candidate_binding(config: SandboxConfig, request: CreationRequest):
    if request.timeout_seconds > config.max_command_seconds:
        raise HTTPException(422, "Invalid creation parameters")
    try:
        path = SandboxExecutor(config)._snapshot(request.snapshot_path)
    except SandboxBoundaryError:
        raise HTTPException(422, "Invalid creation parameters") from None
    fingerprint = _digest({"operation_fingerprint": request.operation_fingerprint,
        "snapshot_path": path, "timeout_seconds": request.timeout_seconds})
    service_id = _digest({"endpoint": config.endpoint, "image": config.image,
        "snapshot_root": config.snapshot_root, "instance_id": config.instance_id,
        "max_command_seconds": config.max_command_seconds})
    return CreationBinding(config.instance_id, request.run_id, request.operation_id, fingerprint, service_id)


def create_candidate_app(config: SandboxConfig, journal: CreationJournal, service_key: str, *, authorize,
                         fence, backend=None, service_id=None, reclaimer=None):
    if not isinstance(service_key, str) or len(service_key) < 32 or not service_key.isascii():
        raise ValueError("An explicit internal service key is required")
    if not callable(authorize):
        raise ValueError("Explicit creation authorization is required")
    if not callable(getattr(fence, "reserve", None)):
        raise ValueError("Explicit creation fence is required")
    if service_id is not None and not re.fullmatch(r"[a-f0-9]{64}", service_id):
        raise ValueError("Invalid creation service identity")
    supervisor = CreationSupervisor(journal)
    backend = backend or SandboxCreationBackend(config)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def authenticate(key: Annotated[str | None, Header(alias="X-Creation-Service-Key")] = None,
                     identity: Annotated[str | None, Header(alias="X-Creation-Service-ID")] = None):
        if key is None or not key.isascii() or not hmac.compare_digest(key, service_key):
            raise HTTPException(401, "Unauthorized")
        if service_id is not None and identity != service_id:
            raise HTTPException(409, "Creation service identity differs")

    def response(request, record):
        result = {"state": record.state, "sandbox_id": record.sandbox_id}
        if service_id is not None:
            result.update(service_id=service_id,
                          **request.model_dump(mode="json", exclude={"execd_token"}))
        return result

    @app.post("/internal/create", dependencies=[Depends(authenticate)])
    async def create(request: CreationRequest):
        binding = candidate_binding(config, request)
        token = request.execd_token.get_secret_value() if request.execd_token is not None else ""
        if len(token) < 32 or len(token) > 128 or not all(c.isascii() and (c.isalnum() or c in "_-") for c in token):
            raise HTTPException(422, "Invalid creation parameters")
        try:
            claimed = authorize(request, lambda: supervisor.claim(binding))
            if claimed is not True:
                raise CreationAlreadyDispatched("Creation exists and cannot be replayed")
            async def dispatch():
                await fence.reserve(binding)
                return await backend.create(binding, snapshot_path=request.snapshot_path,
                    execd_token=token, timeout_seconds=request.timeout_seconds)
            result = await supervisor.create_claimed(binding, dispatch)
        except PermissionError:
            raise HTTPException(403, "Creation is not authorized") from None
        except (CreationAlreadyDispatched, CreationBindingError):
            raise HTTPException(409, "Creation already exists or binding changed") from None
        except Exception:
            raise HTTPException(503, "Creation outcome is unknown") from None
        return response(request, result)

    @app.post("/internal/status", dependencies=[Depends(authenticate)])
    async def status(request: CreationRequest):
        try:
            result = journal.lookup(candidate_binding(config, request))
        except CreationBindingError:
            raise HTTPException(409, "Creation binding changed") from None
        except Exception:
            raise HTTPException(503, "Creation journal is unavailable") from None
        if result is None:
            raise HTTPException(404, "Creation was not recorded")
        return response(request, result)

    if reclaimer is not None:
        @app.post("/internal/reclaim", dependencies=[Depends(authenticate)])
        async def reclaim(request: CreationRequest):
            binding = candidate_binding(config, request)
            try:
                record = journal.lookup(binding)
                if record is None or record.state != "CREATED":
                    raise CreationBindingError("Complete creation receipt required")
                evidence = await reclaimer(record)
                if journal.lookup(binding) != record:
                    raise CreationBindingError("Creation changed during reclamation")
                return {**response(request, record), "evidence": evidence}
            except CreationBindingError:
                raise HTTPException(409, "Creation binding is unavailable") from None
            except Exception:
                raise HTTPException(503, "Resource reclamation is unconfirmed") from None

    return app
