import ast
import json
from pathlib import Path

import pytest

from scripts.m4_formal_audited_archive import (
    archive,
    audited_elapsed_valid,
    validate_acceptance,
    validate_existing_result,
    validate_snapshot_audit,
    validate_terminal,
)


def test_unknown_single_model_and_full_50_are_mandatory():
    run = {"id": "run", "status": "cancelled", "finished_at": "2026-11-10T06:53:38+00:00"}
    operations = [{"operation_id": "done", "status": "completed"},
                  {"operation_id": "unknown", "status": "uncertain", "operation_kind": "model_calls"}]
    tools = [{"status": "completed", "reclaimed_at": "2026-11-10T06:50:00Z", "slot_claimed": False}]
    reservation = {"state": "unknown", "maximum": 50}
    kwargs = {"run_id": "run", "uncertain_id": "unknown", "model_calls": 1, "tool_calls": 1}
    validate_terminal(run, operations, tools, reservation, **kwargs)
    with pytest.raises(Exception):
        validate_terminal(run, operations, tools, {"state": "finished", "maximum": 50}, **kwargs)
    with pytest.raises(Exception):
        validate_terminal(run, operations, [{**tools[0], "slot_claimed": True}], reservation, **kwargs)
    with pytest.raises(Exception):
        validate_terminal(run, operations + [operations[-1]], tools, reservation, **kwargs)


def test_real_failed_one_of_five_is_kept_not_invented_pass():
    groups = [{"group": i, "status": "PASS" if i == 1 else "FAIL"} for i in range(1, 6)]
    result = {"status": "FAIL", "groups": groups, "groups_passed": 1, "groups_complete": True,
              "candidate_sha256": {"x": "hash"}, "image": "image", "exit_code": 1,
              "typescript_typecheck": "not_applicable", "sandbox": {"reclaimed": True}}
    validate_acceptance(result, {"x": "hash"}, "image", "python")
    with pytest.raises(Exception):
        validate_acceptance({**result, "status": "PASS"}, {"x": "hash"}, "image", "python")
    with pytest.raises(Exception):
        validate_acceptance(result, {"x": "different"}, "image", "python")
    with pytest.raises(Exception):
        validate_acceptance({**result, "groups_passed": 5}, {"x": "hash"}, "image", "python")


def test_real_five_of_five_pass_is_preserved():
    groups = [{"group": i, "status": "PASS"} for i in range(1, 6)]
    result = {"status": "PASS", "groups": groups, "groups_passed": 5, "groups_complete": True,
              "candidate_sha256": {"x": "hash"}, "image": "image", "exit_code": 0,
              "typescript_typecheck": "PASS", "sandbox": {"reclaimed": True}}
    validate_acceptance(result, {"x": "hash"}, "image", "typescript")
    with pytest.raises(Exception):
        validate_acceptance({**result, "sandbox": {"reclaimed": False}}, {"x": "hash"}, "image", "typescript")


def test_wall_time_needs_explicit_continuity_evidence():
    assert audited_elapsed_valid({"elapsed_time_valid": True})
    assert not audited_elapsed_valid({})
    assert not audited_elapsed_valid({"elapsed_time_valid": False})


def test_no_command_needs_no_snapshot_but_contradictory_evidence_fails():
    validate_snapshot_audit({}, [])
    validate_snapshot_audit({"snapshots": [], "known_db_snapshot_ids": []}, [])
    with pytest.raises(Exception):
        validate_snapshot_audit({"snapshots": [{"id": "unexpected"}]}, [])
    with pytest.raises(Exception):
        validate_snapshot_audit({"snapshot_verification": {"count": 1}}, [])


def test_existing_command_still_needs_matching_audited_snapshot():
    commands = [{"snapshot_id": "one"}]
    audit = {"snapshot_ids_match_database": True,
             "snapshots": [{"id": "one", "manifest_matches": True, "linux_present": True}]}
    validate_snapshot_audit(audit, commands)
    with pytest.raises(Exception):
        validate_snapshot_audit({**audit, "snapshots": []}, commands)


def test_result_written_before_interruption_is_idempotent_never_overwritten(tmp_path):
    path = tmp_path / "result.json"
    result = {"sequence": 25, "resolved": False, "acceptance": {"status": "FAIL"}}
    validate_existing_result(path, result)
    path.write_text(json.dumps(result), encoding="utf-8")
    validate_existing_result(path, result)
    with pytest.raises(Exception):
        validate_existing_result(path, {**result, "resolved": True})
    assert json.loads(path.read_text(encoding="utf-8")) == result


@pytest.mark.parametrize("sequence", [None, 0, 15, 106, True])
def test_unauthorized_sequence_rejected_before_access(tmp_path, sequence):
    with pytest.raises(Exception):
        archive(sequence, root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_script_does_not_dispatch_cancel_or_mutate_ledger():
    tree = ast.parse(Path("scripts/m4_formal_audited_archive.py").read_text(encoding="utf-8"))
    calls = [node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert not {"post", "put", "patch", "delete", "ainvoke", "invoke", "finish", "unknown", "start", "reserve"} & set(calls)
