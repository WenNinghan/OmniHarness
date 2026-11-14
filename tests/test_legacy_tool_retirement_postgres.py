from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from test_legacy_tool_retirement import evidence_for, setup_legacy
from test_postgres import postgres_environment  # noqa: F401

from omniharness.legacy_tool_retirement import retire_legacy_tool
from omniharness.models import OperationRecord, RunEvent, ToolOperation


def test_concurrent_retirement_is_once_and_unknown_on_postgres(postgres_environment):  # noqa: F811
    settings, _, factory, _ = postgres_environment
    scope, snapshot = setup_legacy(settings, factory)
    evidence = evidence_for(scope, snapshot)
    barrier = Barrier(2)

    def retire():
        barrier.wait(timeout=10)
        with factory.begin() as db:
            return retire_legacy_tool(db, settings, scope, snapshot, evidence, request_id="same-maintenance")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: retire(), range(2)))
    assert sum(result["already_retired"] for result in results) == 1
    assert all(result["outcome"] == "unknown" and result["slot_released"] for result in results)
    with factory() as db:
        assert db.get(OperationRecord, scope.ledger_id).status == "reconciled"
        assert db.get(ToolOperation, scope.operation_id).sandbox_id is None
        assert db.query(RunEvent).filter_by(run_id=scope.run_id, event_type="tool.reconciled").count() == 1
