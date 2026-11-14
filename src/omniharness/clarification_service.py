"""Durable clarification records, scoped to a single authenticated Run."""
from __future__ import annotations

from sqlalchemy import select

from .models import Approval, Clarification, Message, Run, ToolOperation, now
from .services import TERMINAL, ServiceError, add_event, api_value


def clarification_output(row: Clarification) -> dict:
    return {key: api_value(getattr(row, key)) for key in (
        "id", "run_id", "request_key", "task_id", "stage", "question", "reason",
        "options", "status", "answer", "created_at", "answered_at",
    )}


def _locked_run(db, run_id: str) -> Run:
    run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None:
        raise ServiceError("NOT_FOUND", "任务不存在。", 404)
    return run


def create_clarification(db, run, request_key: str, task_id: str | None, question: str,
                         reason: str | None = None, options: list[str] | None = None,
                         stage: str = "execute") -> Clarification:
    """Create once, refusing divergent payloads for an existing request key.

    This does not change the Run state: the worker owns checkpoint/interrupt
    transitions. The caller owns the transaction and must not assume commit.
    """
    run = _locked_run(db, run.id)
    if run.status in TERMINAL:
        raise ServiceError("RUN_TERMINAL", "任务已结束。", 409)
    if run.workflow_version not in {"stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10", "stage-agent-interactive-v1"}:
        raise ServiceError("CLARIFICATION_VERSION_INVALID", "当前工作流不支持澄清请求。", 409)
    choices = options if options is not None else []
    if (not isinstance(request_key, str) or not 1 <= len(request_key) <= 120
            or not isinstance(stage, str) or not 1 <= len(stage) <= 32
            or (task_id is not None and (not isinstance(task_id, str) or len(task_id) > 120))
            or not isinstance(question, str) or not question.strip() or len(question) > 4000
            or (reason is not None and (not isinstance(reason, str) or len(reason) > 1000))
            or not isinstance(choices, list) or len(choices) > 6
            or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in choices)
            or len(set(choices)) != len(choices)):
        raise ServiceError("CLARIFICATION_INVALID", "澄清问题格式不正确。", 422)
    existing = db.scalar(select(Clarification).where(
        Clarification.run_id == run.id, Clarification.request_key == request_key).with_for_update())
    if existing:
        if (existing.task_id, existing.stage, existing.question, existing.reason, existing.options) != (
                task_id, stage, question, reason, choices):
            raise ServiceError("CLARIFICATION_CONFLICT", "相同请求标识对应不同问题。", 409)
        return existing
    row = Clarification(run_id=run.id, request_key=request_key, task_id=task_id, stage=stage,
                        question=question, reason=reason, options=choices, status="pending")
    db.add(row)
    db.flush()
    add_event(db, run, "clarification.requested", {"clarification_id": row.id})
    return row


def list_clarifications(db, run_id: str) -> list[dict]:
    if db.get(Run, run_id) is None:
        raise ServiceError("NOT_FOUND", "任务不存在。", 404)
    return [clarification_output(row) for row in db.scalars(
        select(Clarification).where(Clarification.run_id == run_id)
        .order_by(Clarification.created_at, Clarification.id))]


def answer_clarification(db, run_id: str, clarification_id: str,
                         client_request_id: str, answer: str) -> dict:
    run = _locked_run(db, run_id)
    row = db.scalar(select(Clarification).where(
        Clarification.id == clarification_id, Clarification.run_id == run_id).with_for_update())
    if row is None:
        raise ServiceError("NOT_FOUND", "问题不存在或不属于该任务。", 404)
    previous = db.scalar(select(Clarification).where(
        Clarification.run_id == run_id, Clarification.client_request_id == client_request_id).with_for_update())
    if previous is not None:
        if previous.id != row.id or previous.answer != answer:
            raise ServiceError("REQUEST_ID_CONFLICT", "回复标识已用于其他内容。", 409)
        return clarification_output(previous)
    if row.status != "pending" or run.status not in {"awaiting_user", "awaiting_approval"}:
        raise ServiceError("CLARIFICATION_CLOSED", "问题已关闭或任务当前不等待回复。", 409)
    row.status, row.answer, row.client_request_id, row.answered_at = "answered", answer, client_request_id, now()
    db.flush()
    add_event(db, run, "clarification.answered", {"clarification_id": row.id})
    remaining = db.scalar(select(Clarification.id).where(
        Clarification.run_id == run_id, Clarification.status == "pending").limit(1))
    pending_approval = db.scalar(select(Approval.id).join(ToolOperation).where(
        ToolOperation.run_id == run_id, Approval.status == "pending").limit(1))
    if remaining is not None:
        run.status = "awaiting_user"
    elif pending_approval is not None:
        run.status = "awaiting_approval"
    else:
        run.status = "queued"
    assistant = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
    if assistant:
        assistant.status = run.status
    db.flush()
    return clarification_output(row)


def cancel_clarifications(db, run: Run) -> None:
    for row in db.scalars(select(Clarification).where(
            Clarification.run_id == run.id, Clarification.status == "pending").with_for_update()):
        row.status = "cancelled"
