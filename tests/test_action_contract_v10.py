import pytest
from pydantic import ValidationError

from omniharness.action_contract import validate_candidates, validate_explicit_actions
from omniharness.m4_contract import (
    INTERACTIVE_V10_WORKFLOW_VERSION,
    M4_V9_PLUGIN_VERSIONS,
    M4_V10_PLUGIN_VERSIONS,
    V9_STAGE_VERSIONS,
    workspace_role_versions,
)
from omniharness.schemas import SendMessageRequest
from omniharness.settings import Settings


def candidate(**changes):
    return {"kind": "workspace_file_write", "path": "src/out.txt", "step_id": "s1",
            "source_message_id": "m1", "source_excerpt": "create src/out.txt", **changes}


def validate(raw, **changes):
    args = {"run_id": "r1", "workspace_id": "w1", "source_message_id": "m1",
            "source_text": "please create src/out.txt and run pytest", "source_run_id": "r1"}
    return validate_candidates(raw, **{**args, **changes})


def test_candidate_binds_scope_and_exact_source_without_treating_it_as_fact():
    result = validate([candidate()])
    assert result["assessed"] and result["actions"][0]["source"] == "planner"
    assert result["actions"][0]["run_id"] == "r1"
    assert result["actions"][0]["workspace_id"] == "w1"
    assert result == validate([candidate()])
    assert result["actions"][0]["id"] != validate([candidate()], run_id="r2", source_run_id="r2")["actions"][0]["id"]


@pytest.mark.parametrize("raw,changes,reason", [
    ([], {}, "no_actions"),
    ([candidate(source_excerpt="CREATE src/out.txt")], {}, "invalid_source_anchor"),
    ([candidate(source_message_id="other")], {}, "invalid_source_anchor"),
    ([candidate()], {"source_run_id": "other"}, "invalid_source_scope"),
    ([candidate(path="../secret")], {}, "invalid_action"),
    ([candidate(), candidate()], {}, "duplicate_actions"),
    ([candidate(task_id="invented")], {}, "invalid_action"),
])
def test_candidates_fail_closed(raw, changes, reason):
    assert validate(raw, **changes) == {"version": 1, "assessed": False, "reason": reason, "actions": []}


def test_explicit_commands_validate_exact_parameters_and_do_not_authorize_them():
    raw = [{"kind": "sandbox_command", "argv": ["pytest", "-q"], "cwd": "."}]
    result = validate_explicit_actions(raw, run_id="r1", workspace_id="w1")
    assert result["assessed"] and result["actions"][0]["source"] == "user"
    assert result["actions"][0]["argv"] == ["pytest", "-q"]
    assert not validate_explicit_actions([dict(raw[0], cwd="../other")], run_id="r1", workspace_id="w1")["assessed"]
    assert not validate_explicit_actions([dict(raw[0], argv=["pytest", ""])], run_id="r1", workspace_id="w1")["assessed"]


def test_request_scope_and_v10_opt_in_are_explicit():
    with pytest.raises(ValidationError):
        SendMessageRequest(content="hello", model_config_id="m", client_request_id="c", auto_correction_enabled=False)
    old = Settings(_env_file=None)
    assert old.workspace_workflow_version != INTERACTIVE_V10_WORKFLOW_VERSION
    preview = Settings(_env_file=None, enable_agent=True, enable_tools=True, v6_preview=True,
                       interactive_preview=True, instance_id="omni-v6-preview")
    assert preview.workspace_workflow_version == "stage-agent-v9"
    new = Settings(_env_file=None, enable_agent=True, enable_tools=True, v6_preview=True,
                   interactive_preview=True, v10_preview=True, instance_id="omni-v6-preview")
    assert new.workspace_workflow_version == INTERACTIVE_V10_WORKFLOW_VERSION
    assert workspace_role_versions("stage-agent-v9") == V9_STAGE_VERSIONS
    assert M4_V9_PLUGIN_VERSIONS != M4_V10_PLUGIN_VERSIONS
    assert "builtin-action-contract" not in M4_V9_PLUGIN_VERSIONS
    assert "builtin-native-execution-inventory" not in M4_V9_PLUGIN_VERSIONS
    assert M4_V10_PLUGIN_VERSIONS == {**M4_V9_PLUGIN_VERSIONS, "builtin-action-contract": "2",
                                      "builtin-native-execution-inventory": "1"}


def test_revision_endpoint_locks_after_start_and_keeps_prior_versions(environment):
    from omniharness.models import AgentRun, Run, Workspace

    _, _, factory, client = environment
    model = client.post("/api/v1/model-configs", json={
        "name": "test", "adapter_type": "mock", "model_name": "demo"}).json()
    conversation = client.post("/api/v1/conversations", json={}).json()
    with factory.begin() as db:
        workspace = Workspace(name="test", host_path="/fixture", probe={})
        db.add(workspace)
        db.flush()
        run = Run(conversation_id=conversation["id"], model_config_id=model["id"],
                  model_label="test", ordinal=1, client_request_id="test", status="queued", mode="mock",
                  workspace_id=workspace.id, workflow_version="stage-agent-v10", config_snapshot={
                      "workflow_type": "agent", "action_contract": {
                          "version": 1, "explicit": validate_explicit_actions(None, run_id="r", workspace_id="w"),
                          "auto_correction_enabled": True, "revisions": []}})
        db.add(run)
        db.flush()
        identity = run.id
    url = f"/api/v1/runs/{identity}/action-requirements"
    request = {"expected_version": 1, "requirements": [{"kind": "workspace_file_write", "path": "file.txt"}]}
    response = client.put(url, json=request)
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    assert response.json()["revisions"][0]["version"] == 1
    assert response.json()["explicit"]["actions"][0]["run_id"] == identity
    assert client.put(url, json=request).status_code == 409
    assert client.get(f"/api/v1/runs/{identity}").json()["action_contract"] == response.json()
    unbound = [{"kind": "workspace_file_write", "path": "new.txt"},
               {"kind": "sandbox_command", "argv": ["pytest"], "cwd": "."}]
    assert client.put(url, json={"expected_version": 2, "requirements": unbound}).status_code == 422
    linked = [{"client_id": "file", "kind": "workspace_file_write", "path": "new.txt", "depends_on": []},
              {"client_id": "test", "kind": "sandbox_command", "argv": ["pytest"],
               "cwd": ".", "depends_on": ["file"]}]
    updated = client.put(url, json={"expected_version": 2, "requirements": linked})
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 3 and updated.json()["explicit"]["version"] == 2
    assert updated.json()["explicit"]["actions"][1]["depends_on"] == ["file"]
    with factory.begin() as db:
        persisted = db.get(Run, identity)
        saved = response.json()["explicit"]["actions"][0]
        persisted.outcome = {"v10_actions": {"actions": [saved, {**saved, "run_id": "other"}]},
                             "v10_action_outcome": {"status": "UNVERIFIED"}}
        db.add(AgentRun(run_id=identity, role="execute", status="queued"))
    output = client.get(f"/api/v1/runs/{identity}").json()
    assert len(output["action_candidates"]) == 1
    assert output["action_outcome"]["status"] == "UNVERIFIED"
    assert client.put(url, json={**request, "expected_version": 3}).status_code == 409
