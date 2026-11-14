"""Parallel clarification decisions against the explicitly isolated PG fixture."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from conftest import create_chat
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_postgres import postgres_environment  # noqa: F401

from omniharness.api import create_app
from omniharness.clarification_service import answer_clarification, create_clarification
from omniharness.models import Clarification, Run, RunEvent
from omniharness.security import create_pairing_code


def test_parallel_answers_do_not_cross_wake_or_duplicate(postgres_environment):  # noqa: F811 - shared opt-in fixture
    settings, engine, factory, _ = postgres_environment
    with TestClient(create_app(settings, engine), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        _, _, data = create_chat(client)
    run_id = data["run_id"]
    with factory.begin() as db:
        run = db.get(Run, run_id)
        run.workflow_version = "stage-agent-v7"
        first = create_clarification(db, run, "q1", "t1", "第一个问题")
        second = create_clarification(db, run, "q2", "t2", "第二个问题")
        ids = (first.id, second.id)
        run.status = "awaiting_user"
    start = Barrier(2)
    def answer(index):
        start.wait(timeout=10)
        with factory.begin() as db:
            return answer_clarification(db, run_id, ids[index], f"a{index}", f"答复{index}")
    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(answer, [0, 1]))
    assert {item["id"] for item in outputs} == set(ids)
    with factory() as db:
        assert db.get(Run, run_id).status == "queued"
        rows = {x.id: x for x in db.scalars(select(Clarification).where(Clarification.run_id == run_id))}
        assert rows[ids[0]].answer == "答复0" and rows[ids[1]].answer == "答复1"
        events = list(db.scalars(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.event_type == "clarification.answered")))
        assert len(events) == 2 and len({x.sequence_number for x in events}) == 2
