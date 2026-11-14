"""No-retry client for a Run's frozen creation/reclamation service."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx

from .sandbox import SandboxBoundaryError, SandboxResultUnknown
from .workspace_client import read_service_key


def creation_service_binding(settings):
    url = settings.sandbox_creation_url
    identity = settings.sandbox_creation_service_id
    if not url and not identity and settings.sandbox_creation_key_file is None:
        return None
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or not re.fullmatch(r"[a-f0-9]{64}", identity) or settings.sandbox_creation_key_file is None):
        raise SandboxBoundaryError("Incomplete creation service configuration")
    return {"url": url.rstrip("/"), "service_id": identity}


class CreationServiceClient:
    def __init__(self, settings, frozen, *, transport=None):
        expected = creation_service_binding(settings)
        if not expected or frozen != expected:
            raise SandboxBoundaryError("Frozen creation service configuration changed")
        self.binding = expected
        self._key = read_service_key(settings.sandbox_creation_key_file)
        self._transport = transport

    async def _request(self, action, request):
        # SecretStr's JSON encoding is redacted. Reveal only for the intended
        # authenticated create request, never for logs/status/reclamation.
        body = request.model_dump(mode="json", exclude={"execd_token"})
        if action == "create" and request.execd_token is not None:
            body["execd_token"] = request.execd_token.get_secret_value()
        try:
            async with httpx.AsyncClient(transport=self._transport, trust_env=False, follow_redirects=False,
                                         timeout=90) as client:
                response = await client.post(self.binding["url"] + "/internal/" + action, json=body,
                    headers={"X-Creation-Service-Key": self._key,
                             "X-Creation-Service-ID": self.binding["service_id"]})
                if response.status_code != 200:
                    raise ValueError("Unconfirmed service result")
                result = response.json()
            if (not isinstance(result, dict) or result.get("service_id") != self.binding["service_id"]
                    or result.get("run_id") != request.run_id or result.get("operation_id") != request.operation_id
                    or result.get("operation_fingerprint") != request.operation_fingerprint
                    or result.get("snapshot_path") != request.snapshot_path
                    or result.get("timeout_seconds") != request.timeout_seconds
                    or result.get("state") not in {"DISPATCHED", "CREATED"}):
                raise ValueError("Service receipt binding differs")
            sandbox_id = result.get("sandbox_id")
            if result["state"] == "CREATED" and (not isinstance(sandbox_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sandbox_id)):
                raise ValueError("Invalid sandbox identity")
            if result["state"] == "DISPATCHED" and sandbox_id is not None:
                raise ValueError("Invalid creation state")
            return result
        except Exception:
            raise SandboxResultUnknown("Creation service result is unconfirmed; no retry was made") from None

    async def create(self, request):
        result = await self._request("create", request)
        if result["state"] != "CREATED":
            raise SandboxResultUnknown("Creation receipt is unavailable")
        return result["sandbox_id"]

    async def status(self, request):
        return await self._request("status", request)

    async def reclaim(self, request):
        result = await self._request("reclaim", request)
        evidence = result.get("evidence")
        if (result["state"] != "CREATED" or not isinstance(evidence, dict)
                or evidence.get("kind") != "bound_resource_reclamation"
                or evidence.get("sandbox_id") != result["sandbox_id"]
                or evidence.get("resource_reclaimed") is not True
                or evidence.get("outcome") != "unknown" or evidence.get("replayed") is not False):
            raise SandboxResultUnknown("Resource reclamation is unconfirmed")
        return result
