"""G2 budget slice: frozen caps, atomic shared counters, worker wiring, read-only API."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import create_chat
from fastapi.testclient import TestClient

from omniharness.api import create_app
from omniharness.models import RunBudget
from omniharness.providers import ModelDelta
from omniharness.services import (
    BudgetLimits,
    budget_limits,
    budget_output,
    ensure_budget,
    reserve_budget,
)
from omniharness.worker import Worker

SMALL = BudgetLimits(max_model_calls=3, max_tool_calls=2, max_revisions=1, max_active_seconds=60)


def budget_path(run_id):
    return f"/api/v1/runs/{run_id}/budget"


def test_budget_row_is_lazily_created_and_caps_are_frozen(environment):
    settings, _, factory, client = environment
    _, _, queued = create_chat(client)
    run_id = queued["run_id"]
    with factory() as db:
        assert db.get(RunBudget, run_id) is None
        snapshot = budget_output(db, run_id, budget_limits(settings))
    assert snapshot["initialized"] is False
    assert snapshot["budget_remaining"]["model_calls"] == 50
    assert snapshot["updated_at"] is None
    with factory() as db:
        assert db.get(RunBudget, run_id) is None  # reading never initializes a row

    with factory.begin() as db:
        assert reserve_budget(db, run_id, "model_calls", SMALL) is True
    with factory() as db:
        row = db.get(RunBudget, run_id)
        assert row.max_model_calls == 3 and row.model_calls == 1
        assert row.max_tool_calls == 2 and row.active_seconds == 0

    # A later, larger config must not rewrite the caps frozen at creation.
    with factory.begin() as db:
        reserve_budget(db, run_id, "model_calls", budget_limits(settings))
    with factory() as db:
        row = db.get(RunBudget, run_id)
        assert row.max_model_calls == 3 and row.model_calls == 2


def test_reservation_stops_at_cap_without_overshoot(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    run_id = queued["run_id"]
    outcomes = []
    for _ in range(5):
        with factory.begin() as db:
            outcomes.append(reserve_budget(db, run_id, "model_calls", SMALL))
    assert outcomes == [True, True, True, False, False]
    with factory() as db:
        row = db.get(RunBudget, run_id)
        assert row.model_calls == 3
        assert row.tool_calls == 0 and row.revisions == 0

    with factory.begin() as db:
        assert reserve_budget(db, run_id, "tool_calls", SMALL) is True
        assert reserve_budget(db, run_id, "revisions", SMALL) is True
        assert reserve_budget(db, run_id, "revisions", SMALL) is False
    with factory() as db:
        row = db.get(RunBudget, run_id)
        assert (row.model_calls, row.tool_calls, row.revisions) == (3, 1, 1)


def test_unknown_counter_is_rejected(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    with pytest.raises(ValueError, match="unknown budget counter"):
        with factory.begin() as db:
            reserve_budget(db, queued["run_id"], "arbitrary_column", SMALL)


def test_shared_counter_is_atomic_across_concurrent_sessions(environment):
    _, _, factory, client = environment
    _, _, queued = create_chat(client)
    run_id = queued["run_id"]
    limits = BudgetLimits(max_model_calls=6, max_tool_calls=6, max_revisions=6, max_active_seconds=60)
    with factory.begin() as db:
        ensure_budget(db, run_id, limits)

    def attempt(_):
        with factory.begin() as db:
            return reserve_budget(db, run_id, "model_calls", limits)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(24)))
    assert sum(outcomes) == 6
    with factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 6


def test_budgets_are_isolated_per_run(environment):
    _, _, factory, client = environment
    model, conversation, first = create_chat(client)
    second = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={
        "content": "Second", "model_config_id": model["id"], "client_request_id": "request-2",
    }).json()
    with factory.begin() as db:
        reserve_budget(db, first["run_id"], "model_calls", SMALL)
        reserve_budget(db, first["run_id"], "model_calls", SMALL)
    with factory() as db:
        assert db.get(RunBudget, first["run_id"]).model_calls == 2
        assert db.get(RunBudget, second["run_id"]) is None


async def test_worker_reserves_exactly_one_model_call(environment):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    run_id = queued["run_id"]
    assert await Worker(settings, engine).run_once() == run_id
    with factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 1
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "completed"
    snapshot = client.get(budget_path(run_id)).json()
    assert snapshot["initialized"] is True
    assert snapshot["model_calls"] == 1
    assert snapshot["budget_remaining"]["model_calls"] == 49


async def test_budget_exhaustion_stops_before_any_model_call(environment):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    run_id = queued["run_id"]
    exhausted = BudgetLimits(max_model_calls=0, max_tool_calls=2, max_revisions=1, max_active_seconds=60)
    with factory.begin() as db:
        ensure_budget(db, run_id, exhausted)
    calls = []

    async def stream(config, history):
        calls.append(history)
        yield ModelDelta(text="must never run")

    assert await Worker(settings, engine, stream_fn=stream).run_once() == run_id
    assert calls == []
    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == "failed" and run["error_code"] == "BUDGET_EXCEEDED"
    with factory() as db:
        assert db.get(RunBudget, run_id).model_calls == 0


async def test_disabled_provider_does_not_consume_budget(environment):
    settings, engine, factory, client = environment
    model, _, queued = create_chat(client, adapter="openai", api_key="test-only-key")
    assert client.delete(f"/api/v1/model-configs/{model['id']}").status_code == 204
    assert await Worker(settings, engine).run_once() == queued["run_id"]
    with factory() as db:
        assert db.get(RunBudget, queued["run_id"]) is None
    assert client.get(f"/api/v1/runs/{queued['run_id']}").json()["error_code"] == "MODEL_DISABLED"


def test_budget_endpoint_requires_session_and_never_initializes(environment):
    settings, engine, factory, client = environment
    _, _, queued = create_chat(client)
    path = budget_path(queued["run_id"])
    anonymous = TestClient(create_app(settings, engine))
    assert anonymous.get(path).status_code == 401
    assert client.get(path, headers={"Origin": "https://example.invalid"}).status_code == 403
    assert client.get(budget_path("no-such-run")).status_code == 404
    assert client.get(path).json()["initialized"] is False
    with factory() as db:
        assert db.get(RunBudget, queued["run_id"]) is None
