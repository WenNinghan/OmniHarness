"""Dependency snapshot comparison; gateway and authenticators are test doubles."""

from types import SimpleNamespace

import pytest

from omniharness.correction_gateway_binding import CorrectionGatewayBinding
from omniharness.models import ToolOperation, WorkspaceSnapshot


@pytest.mark.parametrize("files,expected", [
    ([{"path": "src/a.py", "sha256": "written"}], True),
    ([{"path": "src/a.py", "sha256": "changed"}], False),
    ([], False),
])
def test_command_snapshot_must_contain_exact_dependency_patch(monkeypatch, files, expected):
    binding = object.__new__(CorrectionGatewayBinding)
    binding.worker = SimpleNamespace(settings=object())
    monkeypatch.setattr(binding, "_recorded", lambda *args: True)
    monkeypatch.setattr("omniharness.correction_gateway_binding.v9_snapshot_mac", lambda *args: "authenticated")
    prior = SimpleNamespace(parameters={"path": "src/a.py"}, result={"sha256": "written"})
    manifest = {"snapshot_id": "snapshot", "run_id": "run", "workspace_id": "workspace", "files": files}
    snapshot = SimpleNamespace(id="snapshot", run_id="run", workspace_id="workspace",
                               policy_version=1, manifest=manifest)

    class DB:
        def get(self, kind, identity):
            if kind is ToolOperation and identity == "patch-op":
                return prior
            if kind is WorkspaceSnapshot and identity == "snapshot":
                return snapshot
            return None

    run = SimpleNamespace(id="run", workspace_id="workspace", config_snapshot={"workspace": {"policy_version": 1}})
    phase = {"items": [{"action_id": "patch", "tool_call_id": "call-patch", "state": "RECORDED",
                         "tool_operation_id": "patch-op"}],
             "batch": [{"action": {"id": "patch"}, "tool_call": {"id": "call-patch", "name": "workspace_patch"}}]}
    entry = {"dependencies": ["patch"]}
    operation = SimpleNamespace(tool_name="run_command", snapshot_id="snapshot",
                                result={"v9_snapshot_mac": "authenticated"})
    assert binding._snapshot_matches_dependencies(DB(), run, phase, entry, operation) is expected
