"""M3 data-layer schema tests: tables, command idempotency, budgets and lineage defaults."""
import pytest
from conftest import create_chat
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from omniharness.cli import migrate
from omniharness.models import AgentRun, Run, RunBudget, RunCommand

LINEAGE_COLUMNS = {
    "workflow_version", "thread_id", "parent_id", "attempt_index",
    "strategy_id", "outcome", "verdict",
}


def test_m3_tables_and_lineage_columns_exist(environment):
    _, engine, _, _ = environment
    names = set(inspect(engine).get_table_names())
    assert {"run_commands", "agent_runs", "run_budgets"} <= names
    columns = {item["name"] for item in inspect(engine).get_columns("runs")}
    assert LINEAGE_COLUMNS <= columns


def test_m3_migration_is_repeatable(environment):
    settings, engine, _, _ = environment
    migrate(settings)
    assert "run_commands" in inspect(engine).get_table_names()


def test_run_command_is_idempotent_per_client_request(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    run_id = queued["run_id"]
    with factory.begin() as db:
        db.add(RunCommand(run_id=run_id, sequence_number=1,
                          command_type="pause", client_request_id="cmd-1"))
    with pytest.raises(IntegrityError):
        with factory.begin() as db:
            db.add(RunCommand(run_id=run_id, sequence_number=2,
                              command_type="pause", client_request_id="cmd-1"))


def test_agent_run_parent_link_and_budget_defaults(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    run_id = queued["run_id"]
    with factory.begin() as db:
        db.add(AgentRun(id="agent-parent", run_id=run_id, role="execute",
                        status="completed", depth=0))
        db.add(AgentRun(id="agent-child", run_id=run_id, role="verifier",
                        depth=1, parent_agent_run_id="agent-parent"))
        db.add(RunBudget(run_id=run_id))
    with factory() as db:
        child = db.get(AgentRun, "agent-child")
        assert child.parent_agent_run_id == "agent-parent"
        assert child.status == "queued"
        budget = db.get(RunBudget, run_id)
        assert (budget.max_model_calls, budget.max_tool_calls, budget.max_revisions) == (50, 100, 2)
        assert (budget.model_calls, budget.tool_calls, budget.revisions) == (0, 0, 0)
        assert budget.max_active_seconds == 1800


def test_lineage_fields_default_to_manual_and_unverified(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    with factory() as db:
        run = db.get(Run, queued["run_id"])
        assert run.strategy_id == "manual"
        assert run.parent_id is None
        assert run.attempt_index is None
        assert run.outcome is None and run.verdict is None
