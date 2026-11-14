"""M3 checkpoint slice A: idempotent operation ledger and uncertain-outcome semantics."""
from types import SimpleNamespace

import pytest
from conftest import create_chat
from sqlalchemy import inspect, select

from omniharness.cli import migrate
from omniharness.models import OperationRecord
from omniharness.services import (
    begin_operation,
    finish_operation,
    mark_uncertain,
    may_retry,
    operation_id,
    operation_output,
    parameters_digest,
)


def test_operation_id_is_stable_and_bound_to_parameters():
    first = operation_id("run-1", "model-node", {"prompt": "a"})
    assert len(first) == 64
    assert first == operation_id("run-1", "model-node", {"prompt": "a"})
    assert first != operation_id("run-1", "model-node", {"prompt": "b"})
    assert first != operation_id("run-2", "model-node", {"prompt": "a"})
    assert first != operation_id("run-1", "tool-node", {"prompt": "a"})
    assert parameters_digest({"b": 2, "a": 1}) == parameters_digest({"a": 1, "b": 2})


def test_migration_0003_creates_operation_table(environment):
    settings, engine, _, _ = environment
    assert "operation_records" in inspect(engine).get_table_names()
    migrate(settings)
    assert "operation_records" in inspect(engine).get_table_names()


def test_begin_operation_is_idempotent_across_sessions(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    with factory.begin() as db:
        first, executable = begin_operation(db, queued["run_id"], "model-node", {"prompt": "a"})
        assert executable is True and first.status == "running"
    with factory.begin() as db:
        again, executable = begin_operation(db, queued["run_id"], "model-node", {"prompt": "a"})
        assert executable is False
        assert again.operation_id == first.operation_id and again.attempts == 1
    with factory() as db:
        assert len(list(db.scalars(select(OperationRecord)))) == 1


def test_different_parameters_create_distinct_operations(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    with factory.begin() as db:
        first, first_ok = begin_operation(db, queued["run_id"], "model-node", {"prompt": "a"})
        second, second_ok = begin_operation(db, queued["run_id"], "model-node", {"prompt": "b"})
    assert first_ok and second_ok
    assert first.operation_id != second.operation_id
    with factory() as db:
        assert len(list(db.scalars(select(OperationRecord)))) == 2


def test_completed_operation_never_replays(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    with factory.begin() as db:
        row, executable = begin_operation(db, queued["run_id"], "model-node")
        assert executable
        op_id = row.operation_id
    with factory.begin() as db:
        assert finish_operation(db, op_id, "completed", result={"answer": "ok"}) is True
    with factory.begin() as db:
        _, executable = begin_operation(db, queued["run_id"], "model-node")
        assert executable is False
    with factory() as db:
        row = db.get(OperationRecord, op_id)
        assert row.status == "completed" and row.result == {"answer": "ok"}
        assert may_retry(row) is False


def test_uncertain_operation_never_replays_and_cannot_be_overwritten(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    with factory.begin() as db:
        row, _ = begin_operation(db, queued["run_id"], "model-node")
        op_id = row.operation_id
    with factory.begin() as db:
        assert mark_uncertain(db, op_id, "结果未知：远端流中断") is True
    with factory.begin() as db:
        _, executable = begin_operation(db, queued["run_id"], "model-node")
        assert executable is False
        assert finish_operation(db, op_id, "completed", result={"late": True}) is False
    with factory() as db:
        row = db.get(OperationRecord, op_id)
        assert row.status == "uncertain" and row.result is None
        assert "结果未知" in row.error_code
        assert may_retry(row) is False


def test_explicit_failure_is_the_only_retryable_state():
    assert may_retry(SimpleNamespace(status="failed")) is True
    assert may_retry(SimpleNamespace(status="uncertain")) is False
    assert may_retry(SimpleNamespace(status="running")) is False
    assert may_retry(SimpleNamespace(status="completed")) is False
    assert may_retry(SimpleNamespace(status="pending")) is False


def test_finish_operation_is_single_transition(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    with factory.begin() as db:
        row, _ = begin_operation(db, queued["run_id"], "model-node")
        op_id = row.operation_id
    with factory.begin() as db:
        assert finish_operation(db, op_id, "failed", error_code="PROVIDER_ERROR") is True
    with factory.begin() as db:
        assert finish_operation(db, op_id, "completed", result={"late": True}) is False
    with factory() as db:
        row = db.get(OperationRecord, op_id)
        assert row.status == "failed" and row.error_code == "PROVIDER_ERROR"
        assert may_retry(row) is True


def test_finish_operation_rejects_unsupported_status(environment):
    _, _, factory, _ = environment
    with pytest.raises(ValueError):
        with factory.begin() as db:
            finish_operation(db, "missing", "uncertain")


def test_operation_output_is_a_read_only_projection(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    with factory.begin() as db:
        row, _ = begin_operation(db, queued["run_id"], "model-node", workflow_version="v1")
        op_id = row.operation_id
    with factory() as db:
        output = operation_output(db.get(OperationRecord, op_id))
    assert output["operation_id"] == op_id
    assert output["workflow_version"] == "v1"
    assert output["status"] == "running"
    assert set(output) == {"operation_id", "run_id", "node_id", "parameters_hash", "status",
                           "attempts", "result", "error_code", "workflow_version",
                           "created_at", "started_at", "finished_at"}
