import pytest
from conftest import create_chat
from sqlalchemy import select

from omniharness.clarification_service import create_clarification
from omniharness.models import Approval, Clarification, Message, Run, RunEvent, ToolOperation
from omniharness.services import ServiceError


def _pending(environment, task="t1", key="request-1"):
    _, _, factory, client = environment
    _, _, data = create_chat(client)
    with factory.begin() as db:
        run = db.get(Run, data["run_id"])
        run.workflow_version = "stage-agent-interactive-v1"
        row = create_clarification(db, run, key, task, "请选择范围", options=["A", "B"], stage="execute")
        question_id = row.id
        run.status = "awaiting_user"
    return data["run_id"], question_id


def test_clarification_answer_is_same_run_and_idempotent(environment):
    _, _, factory, client = environment
    run_id, question_id = _pending(environment)
    base = f"/api/v1/runs/{run_id}/clarifications"
    listing = client.get(base).json()
    assert len(listing) == 1 and listing[0]["question"] == "请选择范围"
    response = client.post(f"{base}/{question_id}/answer", json={"client_request_id": "answer-1", "answer": "A"})
    assert response.status_code == 200 and response.json()["status"] == "answered"
    assert client.post(f"{base}/{question_id}/answer", json={"client_request_id": "answer-1", "answer": "A"}).status_code == 200
    assert client.post(f"{base}/{question_id}/answer", json={"client_request_id": "answer-1", "answer": "B"}).status_code == 409
    with factory() as db:
        assert db.get(Run, run_id).status == "queued"
        assert len(list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id,
                       RunEvent.event_type == "clarification.answered")))) == 1


def test_questions_bound_to_run_and_cancelled_with_run(environment):
    _, _, factory, client = environment
    run_id, question_id = _pending(environment)
    _, _, other = create_chat(client)
    path = f"/api/v1/runs/{other['run_id']}/clarifications/{question_id}/answer"
    assert client.post(path, json={"client_request_id": "a", "answer": "x"}).status_code == 404
    assert client.post(f"/api/v1/runs/{run_id}/cancel").json()["status"] == "cancelled"
    with factory() as db:
        assert db.get(Clarification, question_id).status == "cancelled"
    assert client.post(f"/api/v1/runs/{run_id}/clarifications/{question_id}/answer",
                       json={"client_request_id": "b", "answer": "x"}).status_code == 409


def test_multiple_pending_questions_remain_waiting_until_all_answered(environment):
    _, _, factory, client = environment
    run_id, first_id = _pending(environment)
    with factory.begin() as db:
        run = db.get(Run, run_id)
        second = create_clarification(db, run, "second", "t2", "何时开始？")
        second_id = second.id
    base = f"/api/v1/runs/{run_id}/clarifications"
    assert client.post(f"{base}/{first_id}/answer", json={"client_request_id": "a", "answer": "A"}).status_code == 200
    with factory() as db:
        assert db.get(Run, run_id).status == "awaiting_user"
    assert client.post(f"{base}/{second_id}/answer", json={"client_request_id": "b", "answer": "现在"}).status_code == 200
    with factory() as db:
        assert db.get(Run, run_id).status == "queued"


def test_request_key_reentry_requires_identical_question(environment):
    _, _, factory, _ = environment
    run_id, first_id = _pending(environment)
    with factory.begin() as db:
        run = db.get(Run, run_id)
        same = create_clarification(db, run, "request-1", "t1", "请选择范围", options=["A", "B"])
        assert same.id == first_id
        with pytest.raises(ServiceError, match="相同请求标识"):
            create_clarification(db, run, "request-1", "t1", "另一个问题", options=["A", "B"])


def test_v8_clarification_can_be_created_and_answered(environment):
    _, _, factory, client = environment
    _, _, data = create_chat(client)
    run_id = data["run_id"]
    with factory.begin() as db:
        run = db.get(Run, run_id)
        run.workflow_version = "stage-agent-v8"
        question = create_clarification(db, run, "v8-request", "t1", "请选择范围")
        question_id = question.id
        run.status = "awaiting_user"
    response = client.post(f"/api/v1/runs/{run_id}/clarifications/{question_id}/answer",
                           json={"client_request_id": "v8-answer", "answer": "A"})
    assert response.status_code == 200
    with factory() as db:
        assert db.get(Run, run_id).status == "queued"



def test_old_workflow_cannot_create_clarification(environment):
    _, _, factory, client = environment
    _, _, data = create_chat(client)
    with factory.begin() as db:
        run = db.get(Run, data["run_id"])
        run.workflow_version = "stage-agent-v6"
        with pytest.raises(ServiceError, match="当前工作流不支持"):
            create_clarification(db, run, "request-1", "t1", "请选择范围")


def test_answer_does_not_bypass_pending_approval(environment):
    _, _, factory, client = environment
    run_id, question_id = _pending(environment)
    with factory.begin() as db:
        db.add(ToolOperation(id="operation", run_id=run_id, task_id="t1", tool_call_id="tool-call",
                             tool_name="run_command", tool_version="v1", parameters={},
                             binding_fingerprint="f" * 64, status="awaiting_approval"))
        db.flush()
        db.add(Approval(id="approval", operation_id="operation", fingerprint="f" * 64, status="pending"))
    response = client.post(f"/api/v1/runs/{run_id}/clarifications/{question_id}/answer",
                           json={"client_request_id": "a", "answer": "A"})
    assert response.status_code == 200
    with factory() as db:
        assert db.get(Run, run_id).status == "awaiting_approval"
        assert db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant")).status == "awaiting_approval"
        assert db.get(Approval, "approval").status == "pending"


def test_question_can_be_answered_while_status_waits_on_approval(environment):
    _, _, factory, client = environment
    run_id, question_id = _pending(environment)
    with factory.begin() as db:
        db.get(Run, run_id).status = "awaiting_approval"
    response = client.post(f"/api/v1/runs/{run_id}/clarifications/{question_id}/answer",
                           json={"client_request_id": "answer", "answer": "A"})
    assert response.status_code == 200
    with factory() as db:
        assert db.get(Run, run_id).status == "queued"
