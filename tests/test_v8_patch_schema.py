"""V8's narrowly scoped create-only compatibility and legacy contract."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from omniharness.m4_tools import PatchArguments, PatchArgumentsV8, WorkspaceToolGateway, tool_definitions
from omniharness.workspace_io import DictWorkspaceRegistry, WorkspaceError, WorkspaceGrant, WorkspaceIO


@pytest.mark.parametrize("extra", [{}, {"expected_sha256": None}, {"expected_sha256": "null"}])
def test_v8_only_normalizes_the_approved_create_values(extra):
    args = PatchArgumentsV8.model_validate({"path": "src/new.txt", "content": "new", **extra})
    assert args.model_dump(mode="json")["expected_sha256"] is None


@pytest.mark.parametrize("value", ["", " ", "NULL", "None", " null", "null ", True, False, 0, [], {}, "a" * 63, "A" * 64])
def test_v8_rejects_other_values(value):
    with pytest.raises(ValidationError):
        PatchArgumentsV8.model_validate({"path": "src/new.txt", "content": "new", "expected_sha256": value})


def test_v8_preserves_hash_and_forbids_extra_fields():
    digest = "a" * 64
    assert PatchArgumentsV8(path="a", content="b", expected_sha256=digest).expected_sha256 == digest
    with pytest.raises(ValidationError):
        PatchArgumentsV8.model_validate({"path": "a", "content": "b", "unexpected": "x"})


def test_old_patch_schema_and_tool_definition_remain_strict():
    for extra in ({}, {"expected_sha256": "null"}):
        with pytest.raises(ValidationError):
            PatchArguments.model_validate({"path": "a", "content": "b", **extra})
    assert PatchArguments(path="a", content="b", expected_sha256=None).expected_sha256 is None
    old = next(t for t in tool_definitions(interactive=True) if t.name == "workspace_patch")
    new = next(t for t in tool_definitions(interactive=True, patch_schema_v2=True) if t.name == "workspace_patch")
    assert "expected_sha256" in old.args_schema.model_json_schema()["required"]
    assert "expected_sha256" not in new.args_schema.model_json_schema()["required"]


def test_legacy_invalid_patch_error_does_not_echo_untrusted_values():
    gateway = WorkspaceToolGateway.__new__(WorkspaceToolGateway)
    gateway.host = SimpleNamespace(workflow_version="stage-agent-v7")
    gateway.workspace = {"tool_policy": {"tools": ["workspace_patch"]}}
    secret = "canary-should-not-be-in-the-error"
    request = SimpleNamespace(tool_call={"id": "call-id", "name": "workspace_patch",
                                         "args": {"path": secret, "content": secret,
                                                  "expected_sha256": secret}})
    response = asyncio.run(gateway.invoke(request))
    assert response.status == "error"
    assert "INVALID_ARGUMENTS" in response.content
    assert secret not in response.content


@pytest.mark.parametrize("extra", [{}, {"expected_sha256": None}, {"expected_sha256": "null"}])
def test_v8_create_intent_cannot_overwrite_existing_file(tmp_path, extra):
    root = tmp_path / "项目"
    (root / "src").mkdir(parents=True)
    path = root / "src/existing.txt"
    path.write_bytes(b"original")
    registry = DictWorkspaceRegistry([WorkspaceGrant("ws", "instance", str(root), write_allow=("src",))])
    io = WorkspaceIO(registry, tmp_path / "storage", protected_roots=(), require_write_lease=lambda *_: None)
    parameters = PatchArgumentsV8.model_validate({"path": "src/existing.txt", "content": "changed", **extra})
    with pytest.raises(WorkspaceError) as err:
        io.patch("ws", "instance", 1, operation_id="op", run_id="run", **parameters.model_dump())
    assert err.value.code == "version_conflict"
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize("extra", [{}, {"expected_sha256": None}, {"expected_sha256": "null"}])
def test_v8_create_intent_creates_only_missing_file(tmp_path, extra):
    root = tmp_path / "项目"
    (root / "src").mkdir(parents=True)
    registry = DictWorkspaceRegistry([WorkspaceGrant("ws", "instance", str(root), write_allow=("src",))])
    io = WorkspaceIO(registry, tmp_path / "storage", protected_roots=(), require_write_lease=lambda *_: None)
    parameters = PatchArgumentsV8.model_validate({"path": "src/new.txt", "content": "created", **extra})
    result = io.patch("ws", "instance", 1, operation_id="op", run_id="run", **parameters.model_dump())
    assert result["state"] == "completed"
    assert result["before_sha256"] is None
    assert (root / "src/new.txt").read_bytes() == b"created"
