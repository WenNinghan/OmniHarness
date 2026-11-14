import pytest
from conftest import create_chat
from test_m4_api import workspace  # noqa: F401
from test_m4_reconciliation import unknown  # noqa: F401

from omniharness.models import OperationRecord, Run, RunCommand, ToolOperation, WorkspaceLease, now
from omniharness.services import add_event


def test_cancelled_run_with_unknown_tool_is_not_reported_as_free_or_success(environment, unknown):  # noqa: F811
    _, _, factory, client = environment
    run_id, operation_id, _, _ = unknown
    with factory.begin() as db:
        op = db.get(ToolOperation, operation_id)
        op.result = {"exit_code": 0, "sandbox_id": op.sandbox_id, "command_id": "cmd"}
        op.command_id = "cmd"
    value = client.get(f"/api/v1/runs/{run_id}/observation").json()
    assert value["phase"] == "needs_reconciliation" and value["run_status"] == "cancelled"
    assert value["resources"] == {"tool_slots_held": 1, "workspace_lease_held": True}
    assert value["evidence"]["command_results"][0]["exit_code"] is None
    assert value["evidence"]["completion_claim"] == "unverified"
    with factory() as db:
        assert db.get(ToolOperation, operation_id).slot_claimed
        assert db.get(OperationRecord, "ledger").status == "uncertain"
        assert db.get(WorkspaceLease, db.get(Run, run_id).workspace_id) is not None


def test_resource_reclamation_is_distinct_from_unknown_command_outcome(environment, unknown):  # noqa: F811
    _, _, factory, client = environment
    run_id, operation_id, _, _ = unknown
    with factory.begin() as db:
        op = db.get(ToolOperation, operation_id)
        op.slot_claimed, op.reclaimed_at = False, now()
    value = client.get(f"/api/v1/runs/{run_id}/observation").json()
    assert value["resources"]["tool_slots_held"] == 0 and value["resources"]["workspace_lease_held"]
    assert value["evidence"]["command_results"][0]["resource_reclaimed"]
    assert value["phase"] == "needs_reconciliation"


@pytest.mark.parametrize("saved_status", ["dispatched", "running"])
def test_uncertain_ledger_classifies_a_killed_workers_last_saved_request(environment, unknown, saved_status):  # noqa: F811
    _, _, factory, client = environment
    run_id, operation_id, _, _ = unknown
    with factory.begin() as db:
        db.get(ToolOperation, operation_id).status = saved_status
    value = client.get(f"/api/v1/runs/{run_id}/observation").json()
    assert value["phase"] == "needs_reconciliation"
    assert value["execution"]["uncertain_tools"] == 1
    assert value["execution"]["active_tools"] == 0
    with factory() as db:
        assert db.get(ToolOperation, operation_id).status == saved_status


def test_no_command_evidence_and_missing_cache_are_not_success_or_zero(environment):
    _, _, factory, client = environment
    _, _, result = create_chat(client)
    run_id = result["run_id"]
    with factory.begin() as db:
        run = db.get(Run, run_id)
        run.status = "completed"
        add_event(db, run, "agent.model_completed", {"usage_details": {"cache_read_input_tokens": 0}})
        add_event(db, run, "agent.model_completed", {"usage": {"input_tokens": 10}})
        db.add(RunCommand(run_id=run_id, sequence_number=1, command_type="append_requirement",
            client_request_id="input", payload={"delivery": "prepared_for_model"}, status="applied"))
    value = client.get(f"/api/v1/runs/{run_id}/observation").json()
    assert value["phase"] == "done" and value["evidence"]["command_total"] == 0
    assert value["cache"]["cache_read_input_tokens"] == {"value": None, "observed": 0,
        "reported_calls": 1, "total_finished_calls": 2, "source": "partial"}
    assert value["cache"]["cache_creation_input_tokens"]["source"] == "unknown"
    assert value["cache"]["cache_creation_input_tokens"]["observed"] is None
    assert value["inputs"]["applied_requirements"] == 1 and value["inputs"]["model_read"] == "unknown"


def test_observation_requires_local_authentication(environment):
    client = environment[3]
    _, _, result = create_chat(client)
    client.post("/api/v1/auth/logout")
    assert client.get(f"/api/v1/runs/{result['run_id']}/observation").status_code == 401
