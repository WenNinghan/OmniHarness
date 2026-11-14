from dataclasses import asdict

import httpx
import pytest
from pydantic import ValidationError

from omniharness.workspace_broker import BrokerDeploymentProfile, HttpWorkspaceRegistry
from omniharness.workspace_io import WorkspaceError, WorkspaceGrant


def grant_payload(**changes):
    result = asdict(WorkspaceGrant("ws", "instance", r"G:\projects\sample", write_allow=(".",)))
    for name in ("read_allow", "write_allow", "excludes"):
        result[name] = list(result[name])
    return {**result, **changes}


def registry(handler):
    return HttpWorkspaceRegistry(
        "http://127.0.0.1:18787",
        instance_id="instance",
        service_key="x" * 40,
        transport=httpx.MockTransport(handler),
    )


def test_registry_fetches_exact_version_and_fresh_revocation_state():
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + "x" * 40
        assert request.headers["X-Omni-Instance"] == "instance"
        return httpx.Response(200, json=grant_payload(enabled=len(requests) == 1))

    with_registry = registry(handle)
    try:
        assert with_registry.get("ws", 1).enabled
        assert not with_registry.get("ws", 1).enabled
        assert all(r.url.path == "/api/internal/workspaces/ws/grant/1" for r in requests)
        assert with_registry.get("ws", 1).write_allow == (".",)
    finally:
        with_registry.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": "other"},
        {"instance_id": "other"},
        {"version": 2},
        {"enabled": "true"},
        {"version": True},
        {"python_executable": "untrusted"},
        {"read_allow": "."},
    ],
)
def test_registry_rejects_invalid_or_cross_instance_grants(changes):
    client = registry(lambda _: httpx.Response(200, json=grant_payload(**changes)))
    try:
        with pytest.raises(WorkspaceError, match="grant"):
            client.get("ws", 1)
    finally:
        client.close()


def test_missing_grant_and_redirect_never_use_fallback_or_forward_credentials():
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": "https://external.invalid/collect"})

    client = registry(handle)
    try:
        assert client.get("ws", 1) is None
        with pytest.raises(WorkspaceError):
            client.get("ws", 1)
        assert len(calls) == 2
        assert all(value.startswith("http://127.0.0.1:18787/") for value in calls)
    finally:
        client.close()


@pytest.mark.parametrize(
    "body", [{"allowed": False}, {"allowed": "true"}, {"allowed": 1}, {"allowed": True, "override": True}]
)
def test_write_lease_requires_strict_authoritative_boolean(body):
    client = registry(lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(WorkspaceError):
            client.require_write_lease("instance", "ws", "run")
    finally:
        client.close()


def test_write_lease_success_and_identifier_validation():
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"allowed": True})

    client = registry(handle)
    try:
        client.require_write_lease("instance", "ws", "run")
        assert calls == ["/api/internal/workspaces/ws/lease/run"]
        with pytest.raises(WorkspaceError):
            client.require_write_lease("other", "ws", "run")
        with pytest.raises(WorkspaceError):
            client.get("../other", 1)
        assert len(calls) == 1
    finally:
        client.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="SECRET MUST NOT APPEAR"),
        httpx.Response(200, content=b"x" * 65537),
        httpx.Response(200, text="not-json"),
    ],
)
def test_registry_errors_are_bounded_and_do_not_include_remote_body(response):
    client = registry(lambda _: response)
    try:
        with pytest.raises(WorkspaceError) as error:
            client.get("ws", 1)
        assert "SECRET MUST NOT APPEAR" not in str(error.value)
        assert error.value.code == "registry_unavailable"
    finally:
        client.close()


def profile_payload():
    return {
        "instance_id": "omni-m4-preview",
        "application_url": "http://127.0.0.1:18787",
        "service_key_file": "/mnt/g/example-repo/.omniharness/m4-preview/secrets/workspace-service.key",
        "python_executable": "/mnt/g/example-repo/.venv/Scripts/python.exe",
        "module_file": r"G:\example-repo\src\omniharness\workspace_io.py",
        "framework_root": r"G:\example-repo",
        "native_storage_root": r"G:\example-repo\.omniharness\m4-preview\native",
        "native_protected_roots": [r"G:\example-repo"],
        "mapped_native_storage_root": "/mnt/g/example-repo/.omniharness/m4-preview/native",
        "snapshot_root": "/var/lib/omniharness-m4-preview/snapshots",
    }


def test_trusted_profile_pins_helper_and_private_boundaries():
    profile = BrokerDeploymentProfile.model_validate(profile_payload())
    assert profile.port == 19092
    assert profile.native_storage_root.startswith(profile.framework_root)


@pytest.mark.parametrize(
    "changes",
    [
        {"native_protected_roots": [r"F:\unrelated"]},
        {"snapshot_root": "/mnt/g/private"},
        {"module_file": r"G:\other\workspace_io.py"},
        {"python_executable": "/bin/sh"},
        {"application_url": "https://external.invalid"},
        {"application_url": "http://user:password@127.0.0.1"},
        {"service_key_file": "/private/../key"},
        {"bind_host": "0.0.0.0"},
        {"native_storage_root": "G:\\"},
        {"mapped_native_storage_root": "/mnt/g/other"},
        {"shell": "powershell"},
    ],
)
def test_untrusted_or_widened_deployment_profiles_are_rejected(changes):
    with pytest.raises(ValidationError):
        BrokerDeploymentProfile.model_validate({**profile_payload(), **changes})


def test_broker_uses_lifespan_to_close_registry():
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    from omniharness.workspace_broker import create_broker_app

    events = []

    @asynccontextmanager
    async def lifespan(_app):
        events.append("opened")
        yield
        events.append("closed")

    app = create_broker_app(object(), instance_id="preview", service_key="x" * 32, lifespan=lifespan)
    with TestClient(app) as client:
        assert events == ["opened"]
        assert client.get("/health").status_code == 401
    assert events == ["opened", "closed"]
