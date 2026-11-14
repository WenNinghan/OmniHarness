"""User action dependencies are explicit, scoped and never model-supplied."""
import pytest
from pydantic import ValidationError

from omniharness.action_contract import validate_candidates, validate_explicit_actions
from omniharness.schemas import ActionRequirementsUpdate


def actions():
    return [
        {"client_id": "write", "kind": "workspace_file_write", "path": "src/out.txt", "depends_on": []},
        {"client_id": "test", "kind": "sandbox_command", "argv": ["pytest", "-q"],
         "cwd": ".", "depends_on": ["write"]},
    ]


def validated(items, **kwargs):
    return validate_explicit_actions(items, run_id="r1", workspace_id="w1", require_dependencies=True, **kwargs)


def test_dependency_graph_binds_stable_action_ids_and_not_input_order():
    raw = actions()
    result = validated(raw)
    assert result["version"] == 2 and result["assessed"]
    by_name = {item["client_id"]: item for item in result["actions"]}
    assert result["dependency_graph"] == {
        by_name["write"]["id"]: [], by_name["test"]["id"]: [by_name["write"]["id"]]}
    reversed_result = validated(list(reversed(raw)))
    assert reversed_result["dependency_graph"] == result["dependency_graph"]
    foreign = validate_explicit_actions(raw, run_id="other", workspace_id="w1", require_dependencies=True)
    assert set(result["dependency_graph"]).isdisjoint(foreign["dependency_graph"])


@pytest.mark.parametrize("items", [
    [{"kind": "workspace_file_write", "path": "a"}, {"kind": "workspace_file_write", "path": "b"}],
    [{"client_id": "a", "kind": "workspace_file_write", "path": "a", "depends_on": []},
     {"client_id": "b", "kind": "workspace_file_write", "path": "b", "depends_on": []}],
    [{"client_id": "a", "kind": "workspace_file_write", "path": "a", "depends_on": []},
     {"client_id": "b", "kind": "workspace_file_write", "path": "b"}],
    [{"client_id": "a", "kind": "workspace_file_write", "path": "a", "depends_on": ["outside"]},
     {"client_id": "b", "kind": "workspace_file_write", "path": "b", "depends_on": []}],
    [{"client_id": "a", "kind": "workspace_file_write", "path": "a", "depends_on": ["b"]},
     {"client_id": "b", "kind": "workspace_file_write", "path": "b", "depends_on": ["a"]}],
    [{"client_id": "a", "kind": "workspace_file_write", "path": "a", "depends_on": ["a"]},
     {"client_id": "b", "kind": "workspace_file_write", "path": "b", "depends_on": []}],
    [{"client_id": "a", "kind": "workspace_file_write", "path": "a", "depends_on": []},
     {"client_id": "a", "kind": "workspace_file_write", "path": "b", "depends_on": ["a"]}],
    [{"client_id": "a", "kind": "workspace_file_write", "path": "a", "depends_on": []},
     {"client_id": "b", "kind": "workspace_file_write", "path": "b", "depends_on": ["r2:a"]}],
])
def test_missing_ambiguous_or_invalid_dependencies_rejected(items):
    assert not validated(items)["assessed"]
    with pytest.raises(ValidationError):
        ActionRequirementsUpdate(expected_version=1, requirements=items)


def test_single_and_old_unannotated_actions_keep_v1_ids():
    one = [{"kind": "workspace_file_write", "path": "src/out.txt"}]
    value = validated(one)
    assert value["version"] == 1 and "dependency_graph" not in value
    assert value["actions"][0]["id"] == "b3a1b1f62f80fe115012f32191b21ff3314733267669ce7ba7301fb5d6625b38"
    old = [{"kind": "workspace_file_write", "path": "a"},
           {"kind": "sandbox_command", "argv": ["pytest"], "cwd": "."}]
    assert validate_explicit_actions(old, run_id="r1", workspace_id="w1")["version"] == 1
    assert not validated(old)["assessed"]


def test_model_candidate_cannot_supply_user_dependencies():
    raw = [{"kind": "workspace_file_write", "path": "a", "source_message_id": "m1",
            "source_excerpt": "create a", "client_id": "a", "depends_on": []}]
    result = validate_candidates(raw, run_id="r1", workspace_id="w1", source_run_id="r1",
                                 source_message_id="m1", source_text="create a")
    assert result["assessed"] is False
