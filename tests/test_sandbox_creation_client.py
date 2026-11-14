import json
from types import SimpleNamespace

import httpx
import pytest

from omniharness.sandbox import SandboxBoundaryError, SandboxResultUnknown
from omniharness.sandbox_creation_api import CreationRequest
from omniharness.sandbox_creation_client import CreationServiceClient, creation_service_binding


@pytest.fixture
def client_settings(tmp_path):
    key = tmp_path / "service.key"
    key.write_text("key-" * 12)
    return SimpleNamespace(sandbox_creation_url="http://creation:19083", sandbox_creation_service_id="a" * 64,
                           sandbox_creation_key_file=key)


@pytest.fixture
def request_body():
    return CreationRequest(run_id="run", operation_id="op", operation_fingerprint="b" * 64,
        snapshot_path="/snapshots/fixture", timeout_seconds=120, execd_token="x" * 32)


@pytest.mark.asyncio
async def test_lost_create_reply_never_retries(client_settings, request_body):
    requests = []

    async def handler(request):
        requests.append(request)
        assert json.loads(request.content)["execd_token"] == "x" * 32
        raise httpx.ReadTimeout("private gateway detail")

    client = CreationServiceClient(client_settings, creation_service_binding(client_settings),
                                   transport=httpx.MockTransport(handler))
    with pytest.raises(SandboxResultUnknown, match="no retry") as error:
        await client.create(request_body)
    assert "private" not in str(error.value) and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["service_id", "operation_fingerprint", "snapshot_path", "sandbox_id"])
async def test_reclamation_rejects_mismatching_receipts(client_settings, request_body, changed):
    async def handler(request):
        assert "execd_token" not in json.loads(request.content)
        result = {**request_body.model_dump(mode="json", exclude={"execd_token"}), "service_id": "a" * 64,
                  "state": "CREATED", "sandbox_id": "sandbox-1", "evidence": {
                      "kind": "bound_resource_reclamation", "sandbox_id": "sandbox-1",
                      "resource_reclaimed": True, "outcome": "unknown", "replayed": False}}
        result[changed] = "mismatch"
        return httpx.Response(200, json=result)

    client = CreationServiceClient(client_settings, creation_service_binding(client_settings),
                                   transport=httpx.MockTransport(handler))
    with pytest.raises(SandboxResultUnknown):
        await client.reclaim(request_body)


def test_frozen_service_never_falls_back_to_current_endpoint(client_settings):
    with pytest.raises(SandboxBoundaryError):
        CreationServiceClient(client_settings, {"url": "http://old:19083", "service_id": "a" * 64})
