"""Offline correction model safety checks; never invokes a provider."""

import time
from types import SimpleNamespace

import pytest
from conftest import create_chat

from omniharness.correction_model import _request
from omniharness.correction_reservation import (
    CorrectionReservationUnavailable,
    correction_intent_mac,
    remaining_active_budget,
    validate_execute_host,
)
from omniharness.models import Message, RunBudget
from omniharness.services import BudgetLimits, ensure_budget


def test_active_segment_counts_current_elapsed_and_reserved_model(environment):
    _, _, factory, client = environment
    run_id = create_chat(client)[2]["run_id"]
    with factory.begin() as db:
        ensure_budget(
            db,
            run_id,
            BudgetLimits(max_model_calls=1, max_tool_calls=2, max_revisions=1, max_active_seconds=10),
        )
        budget = db.get(RunBudget, run_id)
        budget.active_segment_open, budget.model_calls = True, 1
        proof = SimpleNamespace(segment_started=time.monotonic() - 2)
        assert 6 <= remaining_active_budget(db, run_id, proof, 1, model_already_reserved=True) <= 8
        assert remaining_active_budget(db, run_id, proof, 1) == 0
        budget.tool_calls = 2
        assert remaining_active_budget(db, run_id, proof, 1, model_already_reserved=True) == 0
        budget.tool_calls = 0
        budget.active_segment_open = False
        with pytest.raises(CorrectionReservationUnavailable):
            remaining_active_budget(db, run_id, proof, 1, model_already_reserved=True)


def test_correction_request_contains_only_scoped_tools_and_intent_is_bound(environment):
    settings, _, factory, client = environment
    run_id = create_chat(client)[2]["run_id"]
    from omniharness.models import Run, Workspace

    with factory.begin() as db:
        db.add(Workspace(id="fixture-workspace", name="fixture", host_path="/fixture"))
        db.flush()
        run = db.get(Run, run_id)
        user = db.query(Message).filter_by(run_id=run_id, role="user").one()
        run.workspace_id = "fixture-workspace"
        from omniharness.action_contract import validate_explicit_actions

        requested = [{"kind": "workspace_file_write", "path": "a.py"}]
        contract = validate_explicit_actions(requested, run_id=run_id, workspace_id=run.workspace_id)
        action = {**contract["actions"][0], "task_id": "t1"}
        run.config_snapshot = {
            "workspace": {"tool_policy": {"tools": ["workspace_patch", "run_command"]}},
            "agent": {"roles": {}},
            "action_contract": {"version": 1, "request": {"requirements": requested}, "explicit": contract},
        }
        run.outcome = {"v10_actions": {**contract, "actions": [action]}}
        role = {"model": {"max_output_tokens": 500, "context_policy": {}}}
        actions, tools, messages, _, deps = _request(run, user, role)
        assert actions == [action] and [tool.name for tool in tools] == ["workspace_patch"]
        assert deps == {action["id"]: []} and user.content in messages[1].content
        before = correction_intent_mac(settings, run, user)
        run.outcome = {"v10_actions": {**contract, "actions": [{**action, "path": "b.py"}]}}
        assert correction_intent_mac(settings, run, user) != before
        requested = [
            {"kind": "workspace_file_write", "path": "a.py"},
            {"kind": "sandbox_command", "argv": ["pytest"], "cwd": "."},
        ]
        contract = validate_explicit_actions(requested, run_id=run_id, workspace_id=run.workspace_id)
        run.config_snapshot = {
            **run.config_snapshot,
            "action_contract": {"version": 1, "request": {"requirements": requested}, "explicit": contract},
        }
        run.outcome = {
            "v10_actions": {
                **contract,
                "actions": [{**item, "task_id": "t1"} for item in contract["actions"]],
            }
        }
        with pytest.raises(CorrectionReservationUnavailable):
            _request(run, user, role)
        requested[0].update(client_id="write", depends_on=[])
        requested[1].update(client_id="test", depends_on=["write"])
        contract = validate_explicit_actions(
            requested, run_id=run_id, workspace_id=run.workspace_id, require_dependencies=True
        )
        run.config_snapshot = {
            **run.config_snapshot,
            "action_contract": {"version": 1, "request": {"requirements": requested}, "explicit": contract},
        }
        run.outcome = {
            "v10_actions": {
                **contract,
                "actions": [{**item, "task_id": "t1"} for item in contract["actions"]],
            }
        }
        actions, _, _, _, deps = _request(run, user, role)
        assert deps[actions[1]["id"]] == [actions[0]["id"]]


def test_current_execute_model_and_provider_versions_are_rechecked(environment):
    from omniharness.models import ModelConfig, Provider, Run

    _, _, factory, client = environment
    model_output, _, queued = create_chat(client)
    with factory.begin() as db:
        run = db.get(Run, queued["run_id"])
        model = db.get(ModelConfig, model_output["id"])
        provider = db.get(Provider, model.provider_id)
        snapshot = {"id": model.id, "version": model.version, "provider_version": provider.version}
        role = {"model": snapshot}
        host = SimpleNamespace(run_id=run.id, _run=lambda db, role_name: (run, role))
        proof = SimpleNamespace(host=host)
        assert validate_execute_host(db, run, proof) == role
        provider.enabled = False
        db.flush()
        with pytest.raises(CorrectionReservationUnavailable):
            validate_execute_host(db, run, proof)
        provider.enabled = True
        model.version += 1
        db.flush()
        with pytest.raises(CorrectionReservationUnavailable):
            validate_execute_host(db, run, proof)


def test_recorded_batch_rejects_forged_response_and_changed_intent(environment):
    import hashlib

    from langchain_core.messages import AIMessage
    from sqlalchemy import select

    from omniharness.action_contract import validate_explicit_actions
    from omniharness.correction_model import load_recorded_correction_batch
    from omniharness.correction_reservation import correction_invocation_id
    from omniharness.m4_invalid_audit import _key, _mac
    from omniharness.models import OperationRecord, Run, RunBudgetReservation, Workspace
    from omniharness.services import operation_id
    from omniharness.stage_contract import STAGE_ROLE_BINDINGS

    settings, _, factory, client = environment
    run_id = create_chat(client)[2]["run_id"]
    with factory.begin() as db:
        db.add(Workspace(id="batch-workspace", name="fixture", host_path="/fixture"))
        db.flush()
        run = db.get(Run, run_id)
        run.workspace_id, run.workflow_version = "batch-workspace", "stage-agent-v10"
        user = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
        requirements = [{"kind": "workspace_file_write", "path": "a.py"}]
        contract = validate_explicit_actions(requirements, run_id=run_id, workspace_id=run.workspace_id)
        action = {**contract["actions"][0], "task_id": "t1"}
        role = {"model": {"max_output_tokens": 500, "context_policy": {}}}
        run.config_snapshot = {
            "workspace": {"tool_policy": {"tools": ["workspace_patch"]}},
            "agent": {"roles": {STAGE_ROLE_BINDINGS["execute"]: role}},
            "action_contract": {
                "version": 1,
                "request": {"requirements": requirements},
                "explicit": contract,
            },
        }
        run.outcome = {"v10_actions": {**contract, "actions": [action]}}
        _, _, _, serialized, _ = _request(run, user, role)
        request_digest = hashlib.sha256(serialized.encode()).hexdigest()
        invocation = correction_invocation_id(run_id)
        operation = operation_id(run_id, "v10-correction:model_calls", {"invocation_id": invocation})
        message = AIMessage(
            content="",
            response_metadata={"finish_reason": "tool_calls"},
            tool_calls=[{"id": "call1", "name": "workspace_patch", "args": {"path": "a.py", "content": "x"}}],
        )
        raw = message.model_dump(mode="json")
        signature = _mac(
            _key(settings, "stage-agent-v10"),
            "v10-correction-response",
            {"run_id": run_id, "operation_id": operation, "request_sha256": request_digest, "response": raw},
        )
        binding = "b" * 64
        db.add(
            RunBudgetReservation(
                run_id=run_id, invocation_id=invocation, kind="model_calls", binding_fingerprint=binding
            )
        )
        row = OperationRecord(
            operation_id=operation,
            run_id=run_id,
            node_id="v10-correction:model_calls",
            parameters_hash="x",
            status="completed",
            workflow_version="stage-agent-v10",
            result={
                "binding_fingerprint": binding,
                "dispatch_state": "RECORDED",
                "batch_status": "VALIDATED_NOT_DISPATCHED",
                "intent_mac": correction_intent_mac(settings, run, user),
                "request_sha256": request_digest,
                "response": {"message": raw},
                "response_mac": signature,
            },
        )
        db.add(row)
        db.flush()
        assert load_recorded_correction_batch(db, run, settings)[0]["tool_call"]["id"] == "call1"
        forged = {**raw, "content": "forged"}
        row.result = {**row.result, "response": {"message": forged}}
        with pytest.raises(CorrectionReservationUnavailable):
            load_recorded_correction_batch(db, run, settings)
        row.result = {**row.result, "response": {"message": raw}}
        run.outcome = {"v10_actions": {**contract, "actions": [{**action, "path": "b.py"}]}}
        with pytest.raises(CorrectionReservationUnavailable):
            load_recorded_correction_batch(db, run, settings)
