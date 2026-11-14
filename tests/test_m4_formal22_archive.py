import ast
import json
from pathlib import Path

import pytest

from scripts.m4_formal22_archive import RUN, UNCERTAIN, validate_existing_result, validate_terminal


def evidence():
    run = {"id": RUN, "status": "cancelled", "finished_at": "2026-11-10T06:53:38+00:00"}
    operations = [{"operation_id": str(i), "status": "completed"} for i in range(35)]
    operations.append({"operation_id": UNCERTAIN, "status": "uncertain", "operation_kind": "model_calls"})
    tools = [{"status": "completed", "reclaimed_at": "2026-11-10T06:50:00Z", "slot_claimed": False}
             for _ in range(24)]
    reservation = {"state": "unknown", "maximum": 50}
    return run, operations, tools, reservation


def test_exact_terminal_and_unknown_reservation_are_required():
    validate_terminal(*evidence())
    for index, replacement in ((0, {"id": RUN, "status": "pending_review", "finished_at": None}),
                               (3, {"state": "finished", "maximum": 50}),
                               (3, {"state": "unknown", "maximum": 1})):
        args = list(evidence())
        args[index] = replacement
        with pytest.raises(Exception):
            validate_terminal(*args)


def test_unknown_tool_or_different_unknown_operation_is_rejected():
    run, operations, tools, reservation = evidence()
    operations[-1]["operation_id"] = "other"
    with pytest.raises(Exception):
        validate_terminal(run, operations, tools, reservation)
    run, operations, tools, reservation = evidence()
    tools[0]["slot_claimed"] = True
    with pytest.raises(Exception):
        validate_terminal(run, operations, tools, reservation)


def test_interrupted_between_result_and_report_is_idempotent_but_no_overwrite(tmp_path):
    result = tmp_path / "result.json"
    record = {"sequence": 22, "run_status": "cancelled", "resolved": False}
    validate_existing_result(result, record)
    result.write_text(json.dumps(record), encoding="utf-8")
    validate_existing_result(result, record)
    with pytest.raises(Exception, match="cannot be replaced"):
        validate_existing_result(result, {**record, "resolved": True})
    assert json.loads(result.read_text(encoding="utf-8")) == record


def test_archive_script_has_no_cancel_model_dispatch_or_ledger_mutation():
    tree = ast.parse(Path("scripts/m4_formal22_archive.py").read_text(encoding="utf-8"))
    calls = [node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert not {"post", "put", "patch", "delete", "ainvoke", "invoke", "finish", "unknown", "start", "reserve"} & set(calls)
