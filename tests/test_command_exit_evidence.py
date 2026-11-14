from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy.orm.attributes import flag_modified
from test_m4_permissions_postgres import seed

from omniharness.command_exit_evidence import MAX_ASSOCIATIONS, collect_command_exit_evidence
from omniharness.models import ArtifactVersion, OperationRecord, Run, ToolOperation


def setup_command(factory, *, run_id=None, operation_id=None, exit_code=1):
    if run_id is None:
        run_id, operation_id = seed(factory, 1)[0]
    ledger_id, artifact_id = uuid4().hex * 2, str(uuid4())
    with factory.begin() as db:
        run = db.get(Run, run_id)
        operation = db.get(ToolOperation, operation_id)
        if operation is None:
            operation = ToolOperation(
                id=operation_id,
                run_id=run_id,
                task_id="single",
                tool_call_id=uuid4().hex,
                tool_name="run_command",
                tool_version="v1",
                parameters={},
                binding_fingerprint="a" * 64,
            )
            db.add(operation)
        reference = {"id": artifact_id, "run_id": run_id, "operation_id": operation_id, "kind": "command_log"}
        result = {
            "exit_code": exit_code,
            "sandbox_id": "sandbox-1",
            "command_id": "command-1",
            "artifacts": [reference],
        }
        ledger = OperationRecord(
            operation_id=ledger_id,
            run_id=run_id,
            node_id="agent:execute:tool_calls",
            parameters_hash="b" * 64,
            status="completed",
            result={"response": deepcopy(result)},
        )
        db.add(ledger)
        db.flush()
        operation.operation_record_id, operation.tool_name = ledger_id, "run_command"
        operation.status, operation.result = ("failed" if exit_code else "completed"), deepcopy(result)
        operation.sandbox_id, operation.command_id = "sandbox-1", "command-1"
        db.add(
            ArtifactVersion(
                id=artifact_id,
                run_id=run_id,
                operation_id=operation_id,
                workspace_id=run.workspace_id,
                kind="command_log",
                name="stderr.log",
                sha256="c" * 64,
                size_bytes=1,
                storage_path="unused",
                metadata_json={"complete": True},
            )
        )
    return run_id, operation_id, ledger_id, artifact_id


@pytest.fixture
def command(environment):
    factory = environment[2]
    return factory, setup_command(factory)


def collect(factory, run_id):
    with factory() as db:
        return collect_command_exit_evidence(db, run_id)


def test_failed_command_exit_one_is_bound_to_its_complete_log(command):
    factory, (run_id, operation_id, _ledger_id, artifact_id) = command
    assert collect(factory, run_id) == ({artifact_id: (operation_id, 1)}, False)


def test_success_and_failure_remain_distinct_for_same_run(command):
    factory, (run_id, failed_id, _, failed_artifact) = command
    _, success_id, _, success_artifact = setup_command(
        factory, run_id=run_id, operation_id=uuid4().hex * 2, exit_code=0
    )
    assert collect(factory, run_id) == (
        {failed_artifact: (failed_id, 1), success_artifact: (success_id, 0)},
        False,
    )


@pytest.mark.parametrize(
    "case",
    [
        "tool_status",
        "tool_name",
        "ledger_status",
        "ledger_node",
        "ledger_run",
        "tool_sandbox",
        "tool_command",
        "response_sandbox",
        "response_command",
        "response_exit",
        "exit_boolean",
        "exit_string",
        "missing_response",
        "missing_tool_result",
        "missing_artifacts",
        "partial_log",
        "kind",
        "artifact_run",
        "artifact_workspace",
        "reference_run",
        "reference_operation",
        "reference_kind",
        "ledger_reference",
        "duplicate_reference",
        "malformed_reference",
        "oversized_references",
        "malformed_uuid",
    ],
)
def test_missing_inconsistent_or_ambiguous_evidence_is_never_associated(command, case):
    factory, (run_id, operation_id, ledger_id, artifact_id) = command
    other_run = None
    if case in {"ledger_run", "artifact_run", "artifact_workspace"}:
        other_run = seed(factory, 1)[0][0]
    with factory.begin() as db:
        operation, ledger, artifact = (
            db.get(ToolOperation, operation_id),
            db.get(OperationRecord, ledger_id),
            db.get(ArtifactVersion, artifact_id),
        )
        result, envelope = deepcopy(operation.result), deepcopy(ledger.result)
        if case == "tool_status":
            operation.status = "uncertain"
        elif case == "tool_name":
            operation.tool_name = "workspace_read"
        elif case == "ledger_status":
            ledger.status = "uncertain"
        elif case == "ledger_node":
            ledger.node_id = "agent:execute:model_calls"
        elif case == "ledger_run":
            ledger.run_id = other_run
        elif case == "tool_sandbox":
            operation.sandbox_id = "other"
        elif case == "tool_command":
            operation.command_id = "other"
        elif case == "response_sandbox":
            envelope["response"]["sandbox_id"] = "other"
        elif case == "response_command":
            envelope["response"]["command_id"] = "other"
        elif case == "response_exit":
            envelope["response"]["exit_code"] = 0
        elif case == "exit_boolean":
            result["exit_code"] = envelope["response"]["exit_code"] = True
        elif case == "exit_string":
            result["exit_code"] = envelope["response"]["exit_code"] = "1"
        elif case == "missing_response":
            envelope.pop("response")
        elif case == "missing_tool_result":
            result = None
        elif case == "missing_artifacts":
            result.pop("artifacts")
        elif case == "partial_log":
            artifact.metadata_json = {"complete": False}
        elif case == "kind":
            artifact.kind = "tool_result"
        elif case == "artifact_run":
            artifact.run_id = other_run
        elif case == "artifact_workspace":
            artifact.workspace_id = db.get(Run, other_run).workspace_id
        elif case == "reference_run":
            result["artifacts"][0]["run_id"] = "different"
        elif case == "reference_operation":
            result["artifacts"][0]["operation_id"] = "different"
        elif case == "reference_kind":
            result["artifacts"][0]["kind"] = "tool_result"
        elif case == "ledger_reference":
            envelope["response"]["artifacts"] = []
        elif case == "duplicate_reference":
            result["artifacts"].append(deepcopy(result["artifacts"][0]))
        elif case == "malformed_reference":
            result["artifacts"].append({"id": "not-an-id"})
        elif case == "oversized_references":
            result["artifacts"] = [{"id": str(uuid4())} for _ in range(201)]
        elif case == "malformed_uuid":
            artifact.id = "not-uuid"
            result["artifacts"][0]["id"] = envelope["response"]["artifacts"][0]["id"] = artifact.id
        operation.result, ledger.result = result, envelope
        # JSON equality treats True == 1; force persistence for type-tampering tests.
        flag_modified(operation, "result")
        flag_modified(ledger, "result")
    assert collect(factory, run_id) == ({}, False)


def test_shared_ledger_is_ambiguous_even_with_a_matching_response(command):
    factory, (run_id, _, ledger_id, _) = command
    other_run, other_op = seed(factory, 1)[0]
    with factory.begin() as db:
        assert other_run != run_id
        db.get(ToolOperation, other_op).operation_record_id = ledger_id
    assert collect(factory, run_id) == ({}, False)


def test_candidate_limit_marks_truncation_and_never_returns_more_than_200(command):
    factory, (run_id, _, _, _) = command
    for _ in range(MAX_ASSOCIATIONS):
        setup_command(factory, run_id=run_id, operation_id=uuid4().hex * 2)
    values, truncated = collect(factory, run_id)
    assert truncated is True and len(values) == MAX_ASSOCIATIONS
    assert all(exit_code == 1 for _, exit_code in values.values())


def test_other_run_and_unfinished_operation_never_leak(command):
    factory, (run_id, operation_id, _, artifact_id) = command
    other_run, other_id, _, other_artifact = setup_command(factory)
    assert collect(factory, run_id) == ({artifact_id: (operation_id, 1)}, False)
    assert collect(factory, other_run) == ({other_artifact: (other_id, 1)}, False)
    assert collect(factory, str(uuid4())) == ({}, False)
