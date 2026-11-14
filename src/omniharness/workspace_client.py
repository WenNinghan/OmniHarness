"""Private broker client. Endpoints and credentials come only from host settings."""
from pathlib import Path

import httpx

from .services import ServiceError


def read_service_key(path: Path | None) -> str:
    if path is None:
        raise ValueError("Service credential is not configured")
    value = Path(path).read_text(encoding="utf-8").strip()
    if len(value) < 32 or any(ord(c) < 33 for c in value):
        raise ValueError("Service credential is invalid")
    return value


class BrokerClient:
    ACTIONS = frozenset({"probe", "read", "list", "search", "patch", "snapshot", "operation-result"})

    def __init__(self, settings):
        self.url = settings.workspace_broker_url.rstrip("/")
        self.headers = {"Authorization": "Bearer " + read_service_key(settings.workspace_service_key_file),
                        "X-Omni-Instance": settings.instance_id}

    async def health(self):
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
            try:
                response = await client.get(self.url + "/health", headers=self.headers)
                response.raise_for_status()
                value = response.json()
                if value.get("instance_id") != self.headers["X-Omni-Instance"] or value.get("status") != "ok":
                    raise ValueError
                return value
            except Exception:
                raise ServiceError("BROKER_UNAVAILABLE", "宿主文件服务未就绪。", 503) from None

    async def request(self, workspace_id, policy_version, action, **payload):
        if action not in self.ACTIONS or not isinstance(workspace_id, str) or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in workspace_id):
            raise ServiceError("BROKER_REQUEST_INVALID", "宿主文件请求不符合约定。", 422)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=120) as client:
            try:
                response = await client.post(f"{self.url}/workspaces/{workspace_id}/{action}",
                    headers=self.headers, json={"policy_version": policy_version, **payload})
                if response.status_code != 200:
                    # Broker codes are static; do not expose arbitrary upstream text.
                    known = {"version_conflict", "policy_conflict", "file_busy", "operation_exists",
                             "native_result_unknown", "unsafe_link", "path_denied", "workspace_disabled",
                             "workspace_identity_changed", "write_lease_required", "file_too_large", "not_text"}
                    code = response.json().get("error", {}).get("code")
                    raise ServiceError("WORKSPACE_" + code.upper() if code in known else "WORKSPACE_OPERATION_REJECTED",
                                       "宿主文件操作未能完成，请检查授权、文件版本和操作记录。", 409)
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError
                return result
            except ServiceError:
                raise
            except Exception:
                # A patch may have reached the broker: never retry here.
                raise ServiceError("BROKER_RESULT_UNKNOWN", "宿主操作结果未确认，未自动重试。", 503) from None
