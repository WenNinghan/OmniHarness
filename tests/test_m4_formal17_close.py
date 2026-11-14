import httpx
import pytest

from omniharness.evaluation_budget import BudgetLedger
from scripts.m4_formal17_close import assert_no_resources, guarded_cancel


def ledger_for(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3")
    ledger.reserve("run-17", 50, "formal slot")
    ledger.start("run-17")
    return ledger


def test_one_cancel_and_resume_only_reads_existing_intent(tmp_path):
    calls = []
    state = {"status": "pending_review"}

    def handle(request):
        calls.append(request.method)
        if request.method == "POST":
            state["status"] = "cancelled"
        return httpx.Response(200, json={"id": "run", "status": state["status"]})

    ledger = ledger_for(tmp_path)
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        assert guarded_cancel(client, "run", tmp_path / "intent.json", ledger, "run-17")["status"] == "cancelled"
        assert guarded_cancel(client, "run", tmp_path / "intent.json", ledger, "run-17")["status"] == "cancelled"
    assert calls == ["POST", "GET"]
    snapshot = ledger.snapshot()
    assert snapshot["committed"] == 50 and snapshot["reservations"][0]["state"] == "unknown"


def test_lost_cancel_response_never_reposts_and_preserves_budget(tmp_path):
    calls = []

    def handle(request):
        calls.append(request.method)
        if request.method == "POST":
            raise httpx.ReadTimeout("sensitive exception text")
        return httpx.Response(200, json={"id": "run", "status": "pending_review"})

    ledger = ledger_for(tmp_path)
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(Exception, match="not confirmed"):
            guarded_cancel(client, "run", tmp_path / "intent.json", ledger, "run-17")
        with pytest.raises(Exception, match="do not repost"):
            guarded_cancel(client, "run", tmp_path / "intent.json", ledger, "run-17")
    assert calls == ["POST", "GET", "GET"]
    assert ledger.snapshot()["committed"] == 50
    assert ledger.snapshot()["reservations"][0]["state"] == "unknown"


def test_intent_exists_before_unknown_transition_failure_never_posts(tmp_path):
    class BrokenLedger:
        def unknown(self, _):
            raise RuntimeError("storage failure")

    def handle(_):
        pytest.fail("No network action allowed after ledger failure")

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RuntimeError, match="storage failure"):
            guarded_cancel(client, "run", tmp_path / "intent.json", BrokenLedger(), "run-17")
    assert (tmp_path / "intent.json").exists()


@pytest.mark.parametrize("controller,docker", [
    ({"items": [1], "pagination": {"totalItems": 1, "hasNextPage": False}}, lambda *_: (0, False)),
    ({"items": [], "pagination": {"totalItems": 0, "hasNextPage": True}}, lambda *_: (0, False)),
    ({"items": [], "pagination": {"totalItems": 0, "hasNextPage": False}}, lambda *_: (1, False)),
    ({"items": [], "pagination": {"totalItems": 0, "hasNextPage": False}}, lambda _, all_: (0, all_)),
])
def test_absence_fails_closed_if_controller_or_docker_uncertain(controller, docker):
    with pytest.raises(Exception):
        assert_no_resources(lambda: controller, docker, "run")


def test_absence_requires_both_running_and_all_container_checks():
    calls = []
    def docker(_, all_):
        calls.append(all_)
        return 0, False
    assert_no_resources(lambda: {"items": [], "pagination": {"totalItems": 0, "hasNextPage": False}}, docker, "run")
    assert calls == [False, True]
