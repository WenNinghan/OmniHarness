import hashlib

import pytest

from omniharness.evidence_view import _project_handoff
from omniharness.handoff_contract import build_handoff_v7, build_handoff_v8, handoff_event_payload
from omniharness.m4_contract import (
    M4_PLUGIN_VERSIONS,
    M4_V8_PLUGIN_VERSIONS,
    WORKSPACE_WORKFLOWS,
    workspace_prompts,
    workspace_role_versions,
)


def test_v8_freeze_and_legacy_versions_are_distinct():
    assert "stage-agent-v8" in WORKSPACE_WORKFLOWS
    assert M4_V8_PLUGIN_VERSIONS == {**M4_PLUGIN_VERSIONS, "builtin-workspace-patch-schema": "2", "builtin-invalid-tool-audit": "1"}
    assert "builtin-workspace-patch-schema" not in M4_PLUGIN_VERSIONS
    assert workspace_role_versions("stage-agent-v8")["execute"] != workspace_role_versions("stage-agent-v7")["execute"]
    assert "可省略" in workspace_prompts("stage-agent-v8")["execute"]
    assert "可省略" not in workspace_prompts("stage-agent-v7")["execute"]


def test_v8_handoff_is_separately_bound_and_projected():
    report = "result"
    args = dict(run_id="run", workspace_id="workspace", source_task_id="first", target_task_id="next",
                goal_message_id="goal", artifact_id="7cfbdff1-0c04-4f09-bd4e-333c8664b2b0",
                artifact_sha256=hashlib.sha256(report.encode()).hexdigest(), report_text=report,
                operations=[], task_fingerprint="b" * 64)
    v7 = build_handoff_v7(workflow_version="stage-agent-v7", **args)
    v8 = build_handoff_v8(workflow_version="stage-agent-v8", **args)
    assert v7.binding_fingerprint != v8.binding_fingerprint
    assert v7.handoff_id != v8.handoff_id
    assert _project_handoff(handoff_event_payload(v8))["workflow_version"] == "stage-agent-v8"
    with pytest.raises(ValueError):
        build_handoff_v8(workflow_version="stage-agent-v7", **args)



@pytest.mark.parametrize("uncertain", [None, "operation"])
def test_invalid_tool_limit_preserves_uncertain_operations(monkeypatch, uncertain):
    from contextlib import contextmanager

    import omniharness.m3_worker as worker_module

    calls = []
    class DB:
        def scalar(self, statement):
            return uncertain
    class Worker:
        @contextmanager
        def transaction(self):
            yield DB()
    monkeypatch.setattr(worker_module, "_review", lambda *args, **kwargs: calls.append(("review", args, kwargs)))
    monkeypatch.setattr(worker_module, "_finish", lambda *args, **kwargs: calls.append(("finish", args, kwargs)))
    worker_module._invalid_tool_limit(Worker(), "run", started=1.0)
    assert calls[0][0] == ("review" if uncertain else "finish")
    assert "INVALID_TOOL_LIMIT" in calls[0][1]


@pytest.mark.parametrize("version,optional", [("stage-agent-v7", False), ("stage-agent-v8", True), ("stage-agent-v9", True), ("stage-agent-v10", True)])
def test_execution_agent_selects_versioned_schema_and_ask_user(monkeypatch, version, optional):
    from types import SimpleNamespace

    import omniharness.m4_runtime as runtime

    captured = {}
    monkeypatch.setattr(runtime, "create_agent", lambda **kwargs: captured.update(kwargs) or kwargs)
    monkeypatch.setattr(runtime, "WorkspaceToolGateway", lambda **kwargs: SimpleNamespace(**kwargs))
    # v10 consults frozen plugin versions; an unsealed host keeps the plain executor.
    host = SimpleNamespace(workflow_version=version, plugin_versions={})
    runtime.build_execution_agent(model=object(), host=host, workspace=None, broker=None,
                                  sandbox=None, artifacts=None, scheduler=object())
    tools = {tool.name: tool for tool in captured["tools"]}
    assert "ask_user" in tools
    required = tools["workspace_patch"].args_schema.model_json_schema().get("required", [])
    assert ("expected_sha256" not in required) is optional
