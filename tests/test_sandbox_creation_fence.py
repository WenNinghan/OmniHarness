import hashlib
import json

import httpx
import pytest

from omniharness.sandbox_creation_fence import CreationFenceError, DockerCreationFence
from omniharness.sandbox_creation_journal import CreationBinding, CreationRecord


@pytest.mark.asyncio
async def test_engine_marker_forbids_second_claim_even_with_changed_parameters(monkeypatch):
    containers = {}
    engine = "engine-1"
    image = "sha256:" + "a" * 64
    binding = CreationBinding("instance-1", "run-1", "b" * 64, "c" * 64, "d" * 64)

    async def handler(request):
        if request.url.path == "/v1.41/info":
            return httpx.Response(200, json={"ID": engine})
        if request.url.path == "/v1.41/containers/create":
            name = request.url.params["name"]
            if name in containers:
                return httpx.Response(409)
            body = json.loads(request.content)
            identity = hashlib.sha256(name.encode()).hexdigest()
            containers[name] = {"Name": "/" + name, "Id": identity, "Image": image,
                "Config": {"Labels": body["Labels"]}, "State": {"Status": "created"}}
            return httpx.Response(201, json={"Id": identity})
        if request.url.path == "/v1.41/containers/json":
            return httpx.Response(200, json=[])
        for name, obj in containers.items():
            if request.url.path in ("/v1.41/containers/" + obj["Id"] + "/json",
                                    "/v1.41/containers/" + name + "/json"):
                return httpx.Response(200, json=obj)
        return httpx.Response(404)

    monkeypatch.setattr("omniharness.sandbox_creation_fence.httpx.AsyncHTTPTransport",
                        lambda **_: httpx.MockTransport(handler))
    fence = DockerCreationFence(socket_path="/test/docker.sock", engine_id=engine, image=image)
    receipt = await fence.reserve(binding)
    assert receipt["engine_id"] == engine and len(containers) == 1
    assert await fence.verify_absence(CreationRecord(binding, "CREATED", "sandbox-1"))
    other = CreationBinding("instance-1", "run-1", "b" * 64, "e" * 64, "d" * 64)
    with pytest.raises(CreationFenceError):
        await fence.reserve(other)
    assert len(containers) == 1
    containers.clear()
    assert not await fence.verify_absence(CreationRecord(binding, "CREATED", "sandbox-1"))


@pytest.mark.asyncio
async def test_unknown_engine_is_never_used_for_a_claim(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ID": "another-engine"})

    monkeypatch.setattr("omniharness.sandbox_creation_fence.httpx.AsyncHTTPTransport",
                        lambda **_: httpx.MockTransport(handler))
    fence = DockerCreationFence(socket_path="/test/docker.sock", engine_id="engine-1",
                                 image="sha256:" + "a" * 64)
    with pytest.raises(CreationFenceError):
        await fence.reserve(CreationBinding("i", "r", "o", "b" * 64, "c" * 64))
    assert calls == ["/v1.41/info"]
