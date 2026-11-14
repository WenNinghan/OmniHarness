from concurrent.futures import ThreadPoolExecutor

import pytest

from omniharness.evaluation_budget import BudgetLedger, EvaluationBudgetError


def test_reserve_start_resume_unknown_and_reconcile(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = BudgetLedger(path, limit=100)
    assert ledger.reserve("probe", 2, "summary capability")
    assert not ledger.reserve("probe", 2, "summary capability")
    assert ledger.start("probe") and not ledger.start("probe")
    ledger.unknown("probe")
    resumed = BudgetLedger(path, limit=100)
    assert not resumed.start("probe") and resumed.snapshot()["committed"] == 2
    resumed.finish("probe", 1)
    resumed.finish("probe", 1)
    assert resumed.snapshot()["committed"] == 1
    with pytest.raises(EvaluationBudgetError):
        resumed.finish("probe", 0)
    with pytest.raises(EvaluationBudgetError):
        resumed.reserve("probe", 3, "summary capability")


def test_concurrent_reservations_do_not_overspend(tmp_path):
    ledger = BudgetLedger(tmp_path / "ledger.sqlite3", limit=100)

    def reserve(i):
        try:
            return ledger.reserve(str(i), 50, "Run")
        except EvaluationBudgetError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(8))) == 2
    assert ledger.snapshot()["available"] == 0


def test_authorization_and_unknown_are_not_free(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = BudgetLedger(path, limit=100)
    ledger.reserve("lost", 50, "Run")
    ledger.start("lost")
    ledger.unknown("lost")
    with pytest.raises(EvaluationBudgetError):
        BudgetLedger(path, limit=101)
    with pytest.raises(EvaluationBudgetError):
        BudgetLedger(path, authorization_id="different")
    with pytest.raises(EvaluationBudgetError):
        ledger.finish("lost", 51)
    assert ledger.snapshot()["committed"] == 50


def test_only_one_runner_can_drive_the_campaign_and_lock_releases(tmp_path):
    ledger = BudgetLedger(tmp_path / "ledger.sqlite3")
    with ledger.runner_lock():
        with pytest.raises(EvaluationBudgetError, match="Another"):
            with ledger.runner_lock():
                pytest.fail("Two drivers must not run together")
    with ledger.runner_lock():
        assert ledger.snapshot()["available"] == 1500
