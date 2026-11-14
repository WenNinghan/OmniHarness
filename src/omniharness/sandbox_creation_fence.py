"""Candidate engine-side one-shot marker; requires an independently protected engine.

Markers are retained and must never be pruned. This module does not defend
against Docker storage rollback, marker deletion, or a privileged Docker user.
It does not authorize a sandbox, execute a command, or release a tool slot.
"""
from __future__ import annotations

import hashlib
import json
import re

import httpx

from .sandbox import operation_metadata
from .sandbox_creation_journal import CreationBinding, CreationRecord


class CreationFenceError(RuntimeError):
    pass


def _marker(binding):
    value = (binding.instance_id + "\0" + binding.operation_id).encode()
    name = "omni-create-fence-" + hashlib.sha256(value).hexdigest()
    labels = {"omni_fence_type": "creation-v1", "omni_fence_instance": binding.instance_id,
              "omni_fence_operation": binding.operation_id, "omni_fence_run": binding.run_id,
              "omni_fence_binding": binding.fingerprint, "omni_fence_service": binding.service_fingerprint}
    return name, labels


class DockerCreationFence:
    """Claim a persistent, never-started Docker name before sandbox creation."""

    def __init__(self, *, socket_path: str, engine_id: str, image: str):
        if (not socket_path.startswith("/") or "\x00" in socket_path
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", engine_id)
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)):
            raise CreationFenceError("Invalid fence configuration")
        self.socket_path, self.engine_id, self.image = socket_path, engine_id, image

    async def reserve(self, binding: CreationBinding):
        # Name uses the operation identity only: a changed parameter binding
        # cannot claim a second name for an already-dispatched operation.
        name, labels = _marker(binding)
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path, retries=0)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=15,
                                         trust_env=False, follow_redirects=False) as client:
                info = await client.get("/v1.41/info")
                if info.status_code != 200 or info.json().get("ID") != self.engine_id:
                    raise CreationFenceError("Fence engine identity is unavailable")
                response = await client.post("/v1.41/containers/create", params={"name": name}, json={
                    "Image": self.image, "Cmd": ["/bin/true"], "Labels": labels, "NetworkDisabled": True,
                    "HostConfig": {"NetworkMode": "none", "CapDrop": ["ALL"], "ReadonlyRootfs": True,
                                   "PidsLimit": 8}})
                if response.status_code != 201:
                    # Includes conflict and lost-response ambiguity: no retry.
                    raise CreationFenceError("Fence claim is not confirmed")
                identity = response.json().get("Id")
                if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
                    raise CreationFenceError("Fence identity is invalid")
                inspect = await client.get("/v1.41/containers/" + identity + "/json")
                obj = inspect.json() if inspect.status_code == 200 else {}
                saved_labels = obj.get("Config", {}).get("Labels") or {}
                if (obj.get("Name") != "/" + name or obj.get("Id") != identity
                        or obj.get("Image") != self.image
                        or any(saved_labels.get(key) != value for key, value in labels.items())
                        or obj.get("State", {}).get("Status") != "created"):
                    raise CreationFenceError("Fence verification failed")
                return {"marker_id": identity, "marker_name": name, "engine_id": self.engine_id}
        except CreationFenceError:
            raise
        except Exception:
            raise CreationFenceError("Fence claim is not confirmed") from None

    async def verify_absence(self, record: CreationRecord) -> bool:
        """Return true only for a CREATED record and an intact marker with no resources.

        This is not a fence against a privileged Docker actor or marker pruning.
        The caller must also establish that the upstream creation has completed.
        """
        if record.state != "CREATED" or not record.sandbox_id or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", record.sandbox_id):
            return False
        name, labels = _marker(record.binding)
        metadata = operation_metadata(record.binding.instance_id, record.binding.run_id,
                                      record.binding.operation_id)
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path, retries=0)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=15,
                                         trust_env=False, follow_redirects=False) as client:
                async def marker_ok():
                    response = await client.get("/v1.41/containers/" + name + "/json")
                    obj = response.json() if response.status_code == 200 else {}
                    saved = obj.get("Config", {}).get("Labels") or {}
                    return (obj.get("Name") == "/" + name and obj.get("Image") == self.image
                            and obj.get("State", {}).get("Status") == "created"
                            and all(saved.get(key) == value for key, value in labels.items()))

                info = await client.get("/v1.41/info")
                if info.status_code != 200 or info.json().get("ID") != self.engine_id or not await marker_ok():
                    return False
                sandbox = await client.get("/v1.41/containers/sandbox-" + record.sandbox_id + "/json")
                if sandbox.status_code != 404:
                    return False
                for filters in ([key + "=" + value for key, value in metadata.items()],
                                ["opensandbox.io/egress-sidecar-for=" + record.sandbox_id]):
                    response = await client.get("/v1.41/containers/json", params={
                        "all": "1", "filters": json.dumps({"label": filters})})
                    if response.status_code != 200 or response.json() != []:
                        return False
                return await marker_ok()
        except Exception:
            return False
