"""Experimental fenced Agent Worker integration; external API remains gated.

The graph, commands and business records live in separate transactions. Where
an external invocation cannot be reconciled unambiguously, this module stops
in pending_review rather than replaying it. It is not a tool sandbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time

from langchain_core.messages import AIMessage
from langgraph.types import Command
from langsmith import tracing_context
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from .checkpoint_fencing import FencingError, open_fenced_saver, register_fence
from .context_policy import ContextPolicyError, public_context_error
from .m3_agents import (
    AgentBoundaryError,
    AgentBudgetExceeded,
    AgentContextLimitExceeded,
    AgentRole,
    build_controlled_agent,
)
from .m3_budget import ReservationConflict
from .m3_delegation import DelegationDrainTimeout, HierarchicalDelegation
from .m3_host import (
    AgentCancellationRequested,
    AgentControlPending,
    AgentOperationNeedsReview,
    FencedAgentHost,
)
from .m3_roles import PLUGIN_VERSIONS, ROLE_BINDINGS, ROLE_SPECS, ROLE_VERSIONS, WORKFLOW_VERSION
from .m3_stages import StageOutputError, build_stage_graph
from .m4_contract import (
    INTERACTIVE_COMPAT_WORKFLOW_VERSION,
    INTERACTIVE_V9_WORKFLOW_VERSION,
    INTERACTIVE_V10_WORKFLOW_VERSION,
    M4_PLUGIN_VERSIONS,
    M4_V8_PLUGIN_VERSIONS,
    M4_V9_PLUGIN_VERSIONS,
    M4_V10_PLUGIN_VERSIONS,
    WORKSPACE_WORKFLOWS,
    workspace_role_versions,
)
from .m4_invalid_audit import InvalidToolLimitReached
from .model_output import extract_output, output_issue, require_complete_output
from .models import AgentRun, Message, OperationRecord, Run, RunBudget, RunEvent, now
from .providers import ProviderError, agent_streaming_enabled, build_chat_model
from .services import (
    TERMINAL,
    ServiceError,
    add_event,
    finish_cancelled,
    reject_pending_commands,
    runtime_config,
    settle_message_output,
    validate_agent_run_versions,
)
from .stage_contract import (
    INTERACTIVE_STAGE_WORKFLOW_VERSION,
    STAGE_KEYS,
    STAGE_ROLE_BINDINGS,
    STAGE_V1_VERSIONS,
    STAGE_V1_WORKFLOW_VERSION,
    STAGE_VERSIONS,
    STAGE_WORKFLOW_VERSION,
)
from .worker import WorkerOwnershipLost

STAGE_WORKFLOWS = (STAGE_V1_WORKFLOW_VERSION, STAGE_WORKFLOW_VERSION, INTERACTIVE_STAGE_WORKFLOW_VERSION, *WORKSPACE_WORKFLOWS)


def _user_resume_map(db, run_id, interrupts):
    """Resume only the exact checkpoint-bound, answered questions for this Run."""
    from .m4_runtime import approval_resume_map
    from .models import Clarification
    user_items, other_items = [], []
    for item in interrupts:
        value = item.value
        if isinstance(value, dict) and value.get("type") in {"user_clarification", "user_barrier"}:
            user_items.append(item)
        else:
            other_items.append(item)
    result = approval_resume_map(db, run_id, other_items) if other_items else {}
    if result is None:
        return None
    for item in user_items:
        value = item.value
        if value.get("type") == "user_barrier":
            if value.get("run_id") != run_id:
                raise AgentBoundaryError("Clarification barrier belongs to another Run")
            pending = db.scalar(select(Clarification.id).where(
                Clarification.run_id == run_id, Clarification.status == "pending").limit(1))
            if pending:
                return None
            result[item.id] = "continue"
            continue
        row = db.get(Clarification, value.get("clarification_id"))
        if value.get("run_id") != run_id or row is None or row.run_id != run_id:
            raise AgentBoundaryError("Clarification checkpoint binding differs")
        if row.status == "pending":
            return None
        if row.status != "answered" or not isinstance(row.answer, str) or not row.answer.strip():
            raise AgentBoundaryError("Clarification is not answerable")
        result[item.id] = {"clarification_id": row.id, "answer": row.answer}
    return result


def _wait_for_user(worker, identity, *, started, checkpoint_state=None):
    """Release the Worker only after the question interrupt was checkpointed."""
    with worker.transaction() as db:
        uncertain = db.scalar(select(OperationRecord.operation_id).where(
            OperationRecord.run_id == identity, OperationRecord.status.in_(["running", "uncertain"])).limit(1))
    if uncertain:
        _review(worker, identity, "OPERATION_UNCERTAIN", "等待回复前有结果不明的调用，需要核对。", started=started)
        return
    with worker.transaction() as db:
        from .models import Approval, Clarification, ToolOperation
        run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
        pending = db.scalar(select(Clarification.id).where(
            Clarification.run_id == identity, Clarification.status == "pending").limit(1))
        pending_approval = db.scalar(select(Approval.id).join(ToolOperation).where(
            ToolOperation.run_id == identity, Approval.status == "pending").limit(1))
        if pending is None and pending_approval is None and run.status not in {"pause_requested", "cancel_requested"}:
            return True
        # Use the persisted interrupt cutoff consistently on clean and crashed
        # waits. If it cannot be proven, count the full live monotonic span.
        elapsed = (_checkpointed_question_seconds(db, run, checkpoint_state, started=started)
                   if started is not None and pending is not None and pending_approval is None else None)
        budget = db.get(RunBudget, identity)
        if elapsed is not None and budget is not None and budget.active_segment_open:
            budget.active_seconds = min(budget.max_active_seconds, budget.active_seconds + elapsed)
            budget.active_segment_open = False
        else:
            _settle_active(db, identity, started)
        if run.status == "cancel_requested":
            finish_cancelled(db, run)
            return
        run.status = ("paused" if run.status == "pause_requested" else
                      "awaiting_user" if pending is not None else "awaiting_approval")
        _v10_project(db, run, worker)
        assistant = db.scalar(select(Message).where(Message.run_id == identity, Message.role == "assistant"))
        if assistant:
            assistant.status = run.status
        add_event(db, run, "run." + run.status, {"reason": "clarification_checkpoint_saved"})
    return False


def _checkpointed_question_seconds(db, run, state, *, started=None):
    """Validate the exact Run, question, segment and side-effect boundaries."""
    from .interrupt_timing import current_boot_id, recovered_seconds
    from .models import Approval, Clarification, RunEvent, ToolOperation

    interrupts = getattr(state, "interrupts", ()) or ()
    if len(interrupts) != 1 or not getattr(state, "next", None):
        return None
    value = interrupts[0].value
    identity = run.id
    if (run.status != "running" or run.workflow_version not in {
            INTERACTIVE_STAGE_WORKFLOW_VERSION, "stage-agent-v7", INTERACTIVE_COMPAT_WORKFLOW_VERSION, INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION} or not isinstance(value, dict)
            or value.get("type") != "user_clarification" or value.get("run_id") != identity):
        return None
    question = db.get(Clarification, value.get("clarification_id"))
    if question is None or question.run_id != identity or question.status != "pending":
        return None
    if db.scalar(select(OperationRecord.operation_id).where(
            OperationRecord.run_id == identity,
            OperationRecord.status.in_(["running", "uncertain"])).limit(1)):
        return None
    if db.scalar(select(ToolOperation.id).where(
            ToolOperation.run_id == identity,
            ToolOperation.status.not_in(["completed", "failed", "rejected", "cancelled"])
            | ToolOperation.slot_claimed).limit(1)):
        return None
    if db.scalar(select(Approval.id).join(ToolOperation).where(
            ToolOperation.run_id == identity, Approval.status == "pending").limit(1)):
        return None
    event = db.scalar(select(RunEvent).where(
        RunEvent.run_id == identity, RunEvent.event_type == "budget.active_segment_started"
    ).order_by(RunEvent.sequence_number.desc()).limit(1))
    segment = event.payload if event else None
    # A live worker must match its own start; the tolerance only covers the
    # conversion of a nanosecond monotonic timestamp to a Python float.
    if started is not None and (not isinstance(segment, dict) or type(segment.get("start_ns")) is not int
                                or abs(segment["start_ns"] - round(started * 1_000_000_000)) > 1000):
        return None
    return recovered_seconds(segment, value.get("timing"), now_ns=time.monotonic_ns(), boot_id=current_boot_id())


def _recover_checkpointed_question(worker, identity, state):
    """Settle a proven same-boot interactive interrupt once after owner loss."""
    with worker.transaction() as db:
        run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
        budget = db.scalar(select(RunBudget).where(RunBudget.run_id == identity).with_for_update())
        if run is None or budget is None or not budget.active_segment_open:
            return False
        elapsed = _checkpointed_question_seconds(db, run, state)
        if elapsed is None:
            return False
        budget.active_seconds = min(budget.max_active_seconds, budget.active_seconds + elapsed)
        budget.active_segment_open = False
        run.status = "awaiting_user"
        assistant = db.scalar(select(Message).where(Message.run_id == identity, Message.role == "assistant"))
        if assistant:
            assistant.status = "awaiting_user"
        add_event(db, run, "run.awaiting_user", {"reason": "clarification_checkpoint_recovered",
                  "active_seconds_settled": elapsed})
    return True


def _checkpoint_url(database_url: str) -> str:
    url = make_url(database_url)
    # Business connections may set search_path; the fenced saver explicitly
    # controls its own separate checkpoint schema and rejects URL options.
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _roles(worker, snapshot, model_factory):
    roles = snapshot["agent"]["roles"]
    built = {}
    for key in ("think", "plan", "execute", "review"):
        spec = ROLE_SPECS[key]
        config = runtime_config(roles[key]["model"], worker.secrets)
        model = model_factory(spec["name"], config)
        built[key] = AgentRole(name=spec["name"], description=spec["description"], model=model,
                               system_prompt=spec["system_prompt"], tools=())
    return built


def _settle_active(db, identity, started):
    budget = db.get(RunBudget, identity)
    if budget is not None and started is not None:
        budget.active_seconds = min(budget.max_active_seconds,
                                    budget.active_seconds + math.ceil(time.monotonic() - started))
        budget.active_segment_open = False


def _finish(worker, identity, status, code=None, message=None, *, started=None):
    with worker.transaction() as db:
        run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
        if run is None or run.status in TERMINAL:
            return
        _settle_active(db, identity, started)
        if run.status == "cancel_requested" or status == "cancelled":
            finish_cancelled(db, run)
            return
        run.status, run.finished_at = status, now()
        run.error_code, run.error_message = code, message
        _v10_project(db, run, worker)
        assistant = db.scalar(select(Message).where(Message.run_id == identity, Message.role == "assistant"))
        if assistant:
            assistant.status = status
            settle_message_output(assistant, status)
        reject_pending_commands(db, run)
        add_event(db, run, f"run.{status}", {"message_id": assistant.id if assistant else None,
                                              "code": code, "message": message, "usage": run.usage,
                                              "output_metadata": assistant.output_metadata if assistant else {}})


def _v10_project(db, run, worker):
    if run.workflow_version == INTERACTIVE_V10_WORKFLOW_VERSION:
        from .action_outcome import compute_action_outcome
        from .artifacts import ArtifactStore
        state = dict(run.outcome or {})
        contract = (run.config_snapshot or {}).get("action_contract") or {}
        explicit = contract.get("explicit")
        if "v10_actions" not in state and isinstance(explicit, dict) and explicit.get("assessed"):
            state["v10_actions"] = explicit
            run.outcome = state
        db.flush()
        projection = compute_action_outcome(db, run,
            ArtifactStore(worker.settings.data_dir / "artifacts"), worker.settings)
        run.outcome = {**(run.outcome or {}), "v10_action_outcome": projection}


def _root_diagnostic(exc):
    """Classify the original failure behind a sanitized boundary error.

    _raise_sanitized uses ``from None``, which hides but keeps ``__context__``.
    Only the fixed categories of _failure_diagnostic are kept; never text.
    """
    from .m3_host import _failure_diagnostic
    try:
        diagnostic = _failure_diagnostic(exc, "unknown")
    except Exception:
        return None
    chain = (diagnostic.get("transport_diagnostic") or {}).get("exception_chain", [])
    diagnostic["exception_type"] = chain[-1]["type"].rsplit(".", 1)[-1] if chain else "other"
    diagnostic["chain_depth"] = max(0, len(chain) - 1)
    return diagnostic


def _interruption(code, started_at, finished_at, diagnostic=None):
    elapsed = None
    if started_at is not None and finished_at is not None:
        if started_at.tzinfo is None:  # SQLite test databases return naive UTC.
            started_at = started_at.replace(tzinfo=finished_at.tzinfo)
        elapsed = round(max(0.0, (finished_at - started_at).total_seconds()), 3)
    result = {"review_code": code if isinstance(code, str) and len(code) <= 80 else "UNKNOWN",
              "elapsed_seconds": elapsed}
    if diagnostic is not None:
        result["diagnostic"] = diagnostic
    return result


def _review(worker, identity, code, message, *, preserve_requirements=False, started=None, diagnostic=None):
    with worker.transaction() as db:
        run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
        if run is None or run.status in TERMINAL:
            return
        _settle_active(db, identity, started)
        if run.status == "cancel_requested":
            finish_cancelled(db, run)
            return
        run.status, run.error_code, run.error_message = "pending_review", code, message
        reviewed_at = now()
        for operation in db.scalars(select(OperationRecord).where(
                OperationRecord.run_id == identity, OperationRecord.status == "running").with_for_update()):
            operation.status, operation.error_code, operation.finished_at = "uncertain", "RESULT_UNKNOWN", reviewed_at
            # Fixed classification only: which review closed the in-flight call
            # and how long it had run. Never exception text or provider data.
            operation.result = {**(operation.result or {}),
                                "interruption": _interruption(code, operation.started_at, reviewed_at,
                                                              diagnostic)}
        for node in db.scalars(select(AgentRun).where(AgentRun.run_id == identity, AgentRun.status == "running")):
            node.status, node.error_code, node.finished_at = "pending_review", code, reviewed_at
            add_event(db, run, "agent.model_uncertain", {"agent_run_id": node.id, "role": node.role,
                                                         "interruption": _interruption(code, node.started_at,
                                                                                       reviewed_at, diagnostic)})
        _v10_project(db, run, worker)
        assistant = db.scalar(select(Message).where(Message.run_id == identity, Message.role == "assistant"))
        if assistant:
            assistant.status = "pending_review"
            settle_message_output(assistant, "pending_review")
        if not preserve_requirements:
            reject_pending_commands(db, run)
        add_event(db, run, "run.pending_review", {"code": code, "message": message,
                                                  "message_id": assistant.id if assistant else None,
                                                  "output_metadata": assistant.output_metadata if assistant else {},
                                                  **({"diagnostic": diagnostic} if diagnostic is not None else {})})


def _budget_exceeded(worker, identity, *, started):
    """Report a budget stop, retaining any uncertain concurrent operations."""
    # All tracked descendants have already been drained. No new operation may
    # start between this check and the state transition. Cancellation still
    # takes precedence in the locked _finish/_review transactions.
    with worker.transaction() as db:
        uncertain = db.scalar(select(OperationRecord.operation_id).where(
            OperationRecord.run_id == identity,
            OperationRecord.status.in_(["running", "uncertain"]),
        ).limit(1))
    if uncertain is not None:
        _review(worker, identity, "BUDGET_EXCEEDED", "模型或工具调用次数已达到预算上限；并发调用结果需核对。", started=started)
    else:
        _finish(worker, identity, "failed", "BUDGET_EXCEEDED", "模型或工具调用次数已达到预算上限。", started=started)



def _invalid_tool_limit(worker, identity, *, started):
    """Fail after the scheduler drains, unless another operation needs review."""
    with worker.transaction() as db:
        uncertain = db.scalar(select(OperationRecord.operation_id).where(
            OperationRecord.run_id == identity,
            OperationRecord.status.in_(["running", "uncertain"]),
        ).limit(1))
    if uncertain is not None:
        _review(worker, identity, "INVALID_TOOL_LIMIT", "无效工具调用次数已达上限；并发调用结果需核对。", started=started)
    else:
        _finish(worker, identity, "failed", "INVALID_TOOL_LIMIT", "无效工具调用次数已达上限，任务已停止。", started=started)

def _context_exceeded(worker, identity, *, started):
    """Stop before the oversized call and preserve uncertain parallel work."""
    with worker.transaction() as db:
        uncertain = db.scalar(select(OperationRecord.operation_id).where(
            OperationRecord.run_id == identity,
            OperationRecord.status.in_(["running", "uncertain"]),
        ).limit(1))
    if uncertain is not None:
        _review(worker, identity, "CONTEXT_LIMIT", "模型上下文已达到本地上限；并发调用结果需核对。", started=started)
    else:
        _finish(worker, identity, "failed", "CONTEXT_LIMIT", "模型上下文已达到本地上限，请缩短输入或创建新对话。", started=started)


def _complete(worker, identity, answer, *, started, source_message=None, review_status=None):
    """Atomically publish one final answer and its terminal event."""
    with worker.transaction() as db:
        run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
        if run is None or run.status in TERMINAL:
            return
        _settle_active(db, identity, started)
        if run.status == "cancel_requested":
            finish_cancelled(db, run)
            return
        if run.workflow_version == INTERACTIVE_V10_WORKFLOW_VERSION:
            # Do not publish normal completion over an unknown dispatched call.
            from .models import ToolOperation
            unsettled = db.scalar(select(OperationRecord.operation_id).where(
                OperationRecord.run_id == identity,
                OperationRecord.status.in_(["running", "uncertain"])).limit(1))
            uncertain_tool = db.scalar(select(ToolOperation.id).where(
                ToolOperation.run_id == identity,
                (ToolOperation.status.in_(["dispatched", "running", "uncertain", "awaiting_approval"])
                 | ToolOperation.slot_claimed.is_(True))).limit(1))
            if unsettled is not None or uncertain_tool is not None:
                raise AgentOperationNeedsReview("A dispatched operation has no confirmed terminal result")
            _v10_project(db, run, worker)
        assistant = db.scalar(select(Message).where(Message.run_id == identity, Message.role == "assistant"))
        if assistant is None or (assistant.content and assistant.content != answer):
            raise AgentOperationNeedsReview("Existing answer differs from the checkpoint")
        if not assistant.content and answer:
            assistant.content = answer
            add_event(db, run, "message.delta", {"message_id": assistant.id, "delta": answer})
        if source_message is not None:
            output = extract_output(source_message)
            if assistant.reasoning_content and assistant.reasoning_content != output.reasoning:
                raise AgentOperationNeedsReview("Existing reasoning differs from the checkpoint")
            if output.reasoning and not assistant.reasoning_content:
                assistant.reasoning_content = output.reasoning
                add_event(db, run, "message.reasoning_delta", {"message_id": assistant.id, "delta": output.reasoning})
            assistant.output_metadata = {"output_status": "complete", "finish_reason": output.finish_reason}
        if review_status in {"COMPLETED", "INVALID_OUTPUT", "INCOMPLETE", "UNKNOWN", "NOT_RUN_BUDGET"}:
            run.outcome = {**(run.outcome or {}), "review_status": review_status}
            if review_status != "COMPLETED":
                assistant.output_metadata = {**(assistant.output_metadata or {}), "review_status": review_status}
        if run.workspace_id and answer:
            from .command_exit_audit import audit_command_exit_codes
            from .command_exit_evidence import collect_command_exit_evidence

            bindings, evidence_truncated = collect_command_exit_evidence(db, identity)
            if bindings or evidence_truncated:
                audit = audit_command_exit_codes(answer, bindings)
                audit["truncated"] = audit["truncated"] or evidence_truncated
                if audit["conflicts"] or audit["truncated"]:
                    if not audit["conflicts"]:
                        audit["status"] = "incomplete"
                    assistant.output_metadata = {**(assistant.output_metadata or {}), "command_result_audit": audit}
        run.status, run.finished_at = "completed", now()
        run.error_code = run.error_message = None
        assistant.status = "completed"
        settle_message_output(assistant, "completed")
        reject_pending_commands(db, run)
        add_event(db, run, "run.completed", {"message_id": assistant.id, "usage": run.usage,
                                              "output_metadata": assistant.output_metadata})


def _pause(worker, identity, reason, *, started):
    """Settle the active interval in the same transaction as the pause event."""
    with worker.transaction() as db:
        run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
        if run is None:
            return None
        if run.status == "cancel_requested":
            _settle_active(db, identity, started)
            finish_cancelled(db, run)
            return "cancelled"
        if run.status != "pause_requested":
            return run.status
        uncertain = db.scalar(select(OperationRecord.operation_id).where(
            OperationRecord.run_id == identity, OperationRecord.status.in_(["running", "uncertain"])).limit(1))
        if uncertain is not None:
            _settle_active(db, identity, started)
            run.status, run.error_code = "pending_review", "OPERATION_UNCERTAIN"
            run.error_message = "暂停时存在结果不明的调用，需要核对；未自动重放。"
            for operation in db.scalars(select(OperationRecord).where(
                    OperationRecord.run_id == identity, OperationRecord.status == "running").with_for_update()):
                operation.status, operation.error_code, operation.finished_at = "uncertain", "RESULT_UNKNOWN", now()
            assistant = db.scalar(select(Message).where(Message.run_id == identity, Message.role == "assistant"))
            if assistant:
                assistant.status = "pending_review"
            for node in db.scalars(select(AgentRun).where(AgentRun.run_id == identity, AgentRun.status == "running")):
                node.status, node.error_code, node.finished_at = "pending_review", "OPERATION_UNCERTAIN", now()
                add_event(db, run, "agent.model_uncertain", {"agent_run_id": node.id, "role": node.role})
            add_event(db, run, "run.pending_review", {"code": run.error_code, "message": run.error_message})
            return "pending_review"
        _settle_active(db, identity, started)
        run.status = "paused"
        assistant = db.scalar(select(Message).where(Message.run_id == identity, Message.role == "assistant"))
        if assistant:
            assistant.status = "paused"
        add_event(db, run, "run.paused", {"reason": reason})
        return "paused"


def _wait_for_approval(worker, identity, *, started):
    """Only a fully drained Run may release the Worker and stop active time."""
    with worker.transaction() as db:
        uncertain = db.scalar(select(OperationRecord.operation_id).where(
            OperationRecord.run_id == identity, OperationRecord.status.in_(["running", "uncertain"])).limit(1))
    if uncertain:
        _review(worker, identity, "OPERATION_UNCERTAIN", "审批边界仍有结果不明的调用，需要核对。", started=started)
        return
    with worker.transaction() as db:
        run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
        _settle_active(db, identity, started)
        if run.status == "cancel_requested":
            finish_cancelled(db, run)
            return
        run.status = "paused" if run.status == "pause_requested" else "awaiting_approval"
        _v10_project(db, run, worker)
        assistant = db.scalar(select(Message).where(Message.run_id == identity, Message.role == "assistant"))
        if assistant:
            assistant.status = run.status
        add_event(db, run, "run." + run.status, {"reason": "approval_checkpoint_saved"})



def _publish_stage_results(worker, identity, values):
    """Publish only outputs already observed in a committed graph checkpoint."""
    if not isinstance(values, dict):
        return
    outputs = values.get("stage_outputs")
    tasks = values.get("task_results")
    items = []
    if isinstance(outputs, dict):
        items.extend((stage, None, content) for stage, content in outputs.items()
                     if stage in STAGE_KEYS and isinstance(content, str))
    if isinstance(tasks, list):
        items.extend(("execute", item.get("task_id"), item.get("content")) for item in tasks
                     if isinstance(item, dict) and item.get("status", "completed") == "completed" and isinstance(item.get("task_id"), str)
                     and isinstance(item.get("content"), str))
    if not items:
        return
    with worker.transaction() as db:
        run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
        if run is None:
            return
        previous = {(event.payload.get("stage"), event.payload.get("task_id")) for event in db.scalars(
            select(RunEvent).where(RunEvent.run_id == identity, RunEvent.event_type == "agent.stage_result"))}
        for stage, task_id, content in items:
            if (stage, task_id) in previous:
                continue
            add_event(db, run, "agent.stage_result", {"stage": stage, "task_id": task_id,
                      "content": content[:16000], "truncated": len(content) > 16000})
            previous.add((stage, task_id))

def _checkpoint_answer(values):
    messages = values.get("messages", []) if isinstance(values, dict) else []
    if not messages or not isinstance(messages[-1], AIMessage) or messages[-1].tool_calls:
        raise AgentOperationNeedsReview("Checkpoint has no completed assistant answer")
    return require_complete_output(messages[-1])


def _retain_incomplete_output(worker, identity, output, metadata):
    """Publish observed partial output once, including on checkpoint recovery."""
    with worker.transaction() as db:
        run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
        if run is None or run.status in TERMINAL:
            return
        message = db.scalar(select(Message).where(Message.run_id == identity, Message.role == "assistant"))
        if message is None:
            raise AgentOperationNeedsReview("Checkpoint output message is missing")
        for field, value, event_type in (
            ("content", output.text, "message.delta"),
            ("reasoning_content", output.reasoning, "message.reasoning_delta"),
        ):
            saved = getattr(message, field) or ""
            if not value.startswith(saved):
                raise AgentOperationNeedsReview("Saved partial output differs from the checkpoint")
            suffix = value[len(saved):]
            if suffix:
                setattr(message, field, value)
                add_event(db, run, event_type, {"message_id": message.id, "delta": suffix})
        if message.output_metadata != metadata:
            message.output_metadata = metadata
            add_event(db, run, "message.output_state", {"message_id": message.id, "output_metadata": metadata})


def _complete_checkpoint(worker, identity, values, *, started):
    """Validate a committed answer and retain incomplete output without replay."""
    messages = values.get("messages", []) if isinstance(values, dict) else []
    source = messages[-1] if messages and isinstance(messages[-1], AIMessage) else None
    try:
        answer = _checkpoint_answer(values)
    except ProviderError:
        if source is not None:
            output = extract_output(source)
            issue = output_issue(text=output.text, reasoning=output.reasoning, finish_reason=output.finish_reason,
                                 refusal=output.refusal,
                                 require_finish=bool(source.response_metadata.get("omni_require_finish")),
                                 has_tools=bool(source.tool_calls))
            # Only save the observed final message. The operation remains
            # non-replayable; a failed response cannot become completed.
            _retain_incomplete_output(worker, identity, output,
                                      {"finish_reason": output.finish_reason,
                                       "output_status": issue.status if issue else "incomplete"})
        raise
    review_status = values.get("review_status") if isinstance(values, dict) else None
    _complete(worker, identity, answer, started=started, source_message=source, review_status=review_status)


async def _verified_child_interrupt(agent, saver, config, identity):
    """Prove every link of a nested paused task before graph re-entry."""
    if not identity.task_id or identity.thread_id != config["configurable"]["thread_id"]:
        return False
    parts = identity.checkpoint_ns.split("|")
    if (len(parts) not in (1, 2) or any(not p.startswith("tools:") or len(p) <= 6 for p in parts)
            or parts[-1] != f"tools:{identity.task_id}"):
        return False
    root = await agent.aget_state(config)
    task = next((task for task in root.tasks if task.id == parts[0][6:] and task.name == "tools"), None)
    if task is None or not task.interrupts:
        return False
    matching = None
    for index in range(len(parts) + 1):
        checkpoint = await saver.aget_tuple({"configurable": {
            "thread_id": identity.thread_id, "checkpoint_ns": "|".join(parts[:index]),
        }})
        if checkpoint is None:
            return False
        expected_task = parts[index][6:] if index < len(parts) else None
        interrupt_ids = set()
        for task_id, channel, values in checkpoint.pending_writes or ():
            if (channel != "__interrupt__" or (expected_task and task_id != expected_task)
                    or not isinstance(values, (tuple, list))):
                continue
            for item in values:
                value = getattr(item, "value", None)
                if (isinstance(value, dict) and value.get("type") == "pause"
                        and value.get("run_id") == identity.thread_id and getattr(item, "id", None)):
                    interrupt_ids.add(item.id)
        matching = interrupt_ids if matching is None else matching & interrupt_ids
        if not matching:
            return False
    return True


async def run_agent(worker, identity, *, model_factory=None):
    if worker.lock is None:
        raise WorkerOwnershipLost("Agent Worker execution lock is required")
    if worker.engine.dialect.name != "postgresql" or not worker.settings.enable_agent:
        _review(worker, identity, "AGENT_RUNTIME_UNAVAILABLE", "Agent 需要已启用的 PostgreSQL 执行环境。")
        return
    custom_factory = model_factory or getattr(worker, "agent_model_factory", None)
    task = None
    correction_task = None
    started = None
    segment_timing = None
    try:
        with worker.transaction() as db:
            run = db.get(Run, identity)
            if run is None:
                return
            version = run.workflow_version
            if version in {INTERACTIVE_STAGE_WORKFLOW_VERSION, "stage-agent-v7", INTERACTIVE_COMPAT_WORKFLOW_VERSION, INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
                run = db.scalar(select(Run).where(Run.id == identity).with_for_update())
                if run is None or run.workflow_version != version:
                    raise AgentBoundaryError("Interactive workflow changed while starting")
            if version == WORKFLOW_VERSION:
                role_versions, role_bindings = ROLE_VERSIONS, ROLE_BINDINGS
            elif version in STAGE_WORKFLOWS:
                role_versions = (workspace_role_versions(version) if version in WORKSPACE_WORKFLOWS else
                                 STAGE_V1_VERSIONS if version == STAGE_V1_WORKFLOW_VERSION else STAGE_VERSIONS)
                role_bindings = STAGE_ROLE_BINDINGS
            else:
                raise AgentBoundaryError("Unsupported Agent workflow version")
            plugin_versions = (M4_V10_PLUGIN_VERSIONS if version == INTERACTIVE_V10_WORKFLOW_VERSION else
                               M4_V9_PLUGIN_VERSIONS if version == INTERACTIVE_V9_WORKFLOW_VERSION else
                               M4_V8_PLUGIN_VERSIONS if version == INTERACTIVE_COMPAT_WORKFLOW_VERSION else
                               M4_PLUGIN_VERSIONS if version in WORKSPACE_WORKFLOWS else PLUGIN_VERSIONS)
            if version == INTERACTIVE_V10_WORKFLOW_VERSION:
                from .v10_sealed_saver import SEALED_PLUGIN_VERSIONS
                if (run.config_snapshot.get("agent") or {}).get("plugin_versions") == SEALED_PLUGIN_VERSIONS:
                    plugin_versions = SEALED_PLUGIN_VERSIONS
            validate_agent_run_versions(run, version, role_versions=role_versions,
                                        plugin_versions=plugin_versions)
            business_schema = db.execute(text("SELECT current_schema()")).scalar_one()
            budget = db.get(RunBudget, identity)
            if budget is None:
                raise AgentBoundaryError("Frozen run budget is missing")
            remaining = budget.max_active_seconds - budget.active_seconds
            unsettled = budget.active_segment_open
            if not unsettled:
                budget.active_segment_open = True
                if version in {INTERACTIVE_STAGE_WORKFLOW_VERSION, "stage-agent-v7", INTERACTIVE_COMPAT_WORKFLOW_VERSION, INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
                    from .interrupt_timing import new_segment
                    start_ns = time.monotonic_ns()
                    started = start_ns / 1_000_000_000
                    segment_timing = new_segment(start_ns)
                    if segment_timing is not None:
                        add_event(db, run, "budget.active_segment_started", segment_timing)
                else:
                    started = time.monotonic()
            snapshot = run.config_snapshot
            from .history_scope import select_history
            history, history_decisions = select_history(db, run)
            from .context_visibility import record_history_selection
            record_history_selection(db, run, history_decisions)

        def factory(role, config):
            if custom_factory:
                return custom_factory(role, config)
            frozen = snapshot.get("agent", {}).get("roles", {}).get(role, {}).get("model", {})
            if agent_streaming_enabled(frozen, version, config):
                return build_chat_model(config, native_streaming=True)
            return build_chat_model(config)

        if version not in WORKSPACE_WORKFLOWS and (len(history) > worker.settings.max_chat_messages
                or sum(len(x["content"]) for x in history) > worker.settings.max_chat_characters):
            _finish(worker, identity, "failed", "CONTEXT_LIMIT", "此对话已达到本地历史上限，请创建新对话。", started=started)
            return
        if remaining <= 0:
            _finish(worker, identity, "failed", "BUDGET_EXCEEDED", "任务已达到活跃时间预算上限。", started=started)
            return
        if version in STAGE_WORKFLOWS:
            roles = {stage: factory(stage, runtime_config(snapshot["agent"]["roles"][stage]["model"], worker.secrets))
                     for stage in STAGE_KEYS}
        else:
            roles = _roles(worker, snapshot, factory)
        ticket = None
        if version == INTERACTIVE_V10_WORKFLOW_VERSION and plugin_versions == SEALED_PLUGIN_VERSIONS:
            # A ticket is reusable only in this process and for this exact
            # continuously held WorkerLock object. Never reconstruct it from
            # a saved fence row after a restart or connection replacement.
            cache = getattr(worker, "_v10_sealed_tickets", None)
            if cache is None:
                cache = worker._v10_sealed_tickets = {}
            cached = cache.get(identity)
            if cached is None and len(cache) >= 10000:
                raise AgentOperationNeedsReview("Sealed owner ticket capacity reached")
            if (cached and cached[0] is worker.lock and cached[1] is worker.lock.connection
                    and not worker.lock.lost):
                from .v10_closure import pin_checkpoint_watermark
                previous = cached[2]
                schema = previous.schema
                from .v10_saver_guard import authorize_same_owner_resume, revoke_resume
                try:
                    with worker.transaction() as db:
                        checkpoint = db.execute(text(f"SELECT checkpoint_id FROM {schema}.checkpoints "
                            "WHERE thread_id=:run AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1"),
                            {"run": identity}).scalar_one_or_none()
                        if checkpoint:
                            pin_checkpoint_watermark(db, previous, checkpoint)
                            authorize_same_owner_resume(db, previous, checkpoint, worker.settings)
                            ticket = previous
                except BaseException:
                    revoke_resume(previous)
                    raise
        if ticket is None:
            ticket = register_fence(worker.lock, identity, version, schema=worker.settings.checkpoint_schema)
            if version == INTERACTIVE_V10_WORKFLOW_VERSION and plugin_versions == SEALED_PLUGIN_VERSIONS:
                cache[identity] = (worker.lock, worker.lock.connection, ticket)
        async with open_fenced_saver(_checkpoint_url(worker.settings.database_url), ticket) as base_saver:
            saver = base_saver
            if version == INTERACTIVE_V10_WORKFLOW_VERSION:
                from .v10_sealed_saver import SEALED_PLUGIN_VERSIONS, V10SealedSaver
                if plugin_versions == SEALED_PLUGIN_VERSIONS:
                    saver = V10SealedSaver(base_saver.conn, ticket, business_schema=business_schema,
                                           settings=worker.settings)
            agent = None
            config = {"configurable": {"thread_id": identity}}

            async def verify_child(invocation):
                return await _verified_child_interrupt(agent, saver, config, invocation)

            from .m4_runtime import M4AgentHost, approval_resume_map, build_execution_agent
            host_class = M4AgentHost if version in WORKSPACE_WORKFLOWS else FencedAgentHost
            host = host_class(worker, identity, version, role_versions=role_versions,
                                   plugin_versions=plugin_versions, role_bindings=role_bindings,
                                   verify_delegation_resume=verify_child)
            host._active_segment_timing = segment_timing
            host._active_segment_started = started

            current_executor = None

            def build_agent(scheduler):
                nonlocal current_executor
                if version in STAGE_WORKFLOWS:
                    execution_agent = None
                    if version in WORKSPACE_WORKFLOWS:
                        from .artifacts import ArtifactStore
                        from .sandbox import SandboxConfig, SandboxExecutor
                        from .workspace_client import BrokerClient, read_service_key
                        if not worker.settings.enable_tools:
                            raise AgentBoundaryError("Workspace runtime is disabled")
                        frozen = snapshot["workspace"]
                        current = {"endpoint": worker.settings.sandbox_endpoint, "image": worker.settings.sandbox_image,
                                   "instance_id": worker.settings.instance_id,
                                   "snapshot_root": worker.settings.sandbox_snapshot_root,
                                   "max_command_seconds": worker.settings.sandbox_max_command_seconds}
                        if (frozen["sandbox"] != current
                                or frozen.get("broker_endpoint") != worker.settings.workspace_broker_url):
                            raise AgentBoundaryError("Frozen workspace service configuration changed")
                        configuration = SandboxConfig(api_key=read_service_key(worker.settings.sandbox_key_file),
                                                      **frozen["sandbox"])
                        creation_client = None
                        if frozen.get("creation_service") is not None:
                            from .sandbox_creation_client import CreationServiceClient
                            creation_client = CreationServiceClient(worker.settings, frozen["creation_service"])
                        execution_agent = build_execution_agent(
                            model=roles["execute"], host=host, workspace=frozen,
                            broker=getattr(worker, "workspace_broker", None) or BrokerClient(worker.settings),
                            sandbox=getattr(worker, "sandbox_executor", None) or SandboxExecutor(
                                configuration, creation_client=creation_client),
                            artifacts=ArtifactStore(worker.settings.data_dir / "artifacts"), scheduler=scheduler,
                            max_context_messages=worker.settings.max_chat_messages,
                            max_context_characters=worker.settings.max_chat_characters,
                        )
                    current_executor = execution_agent
                    return build_stage_graph(models=roles, hooks=host, checkpointer=saver,
                                             max_context_messages=worker.settings.max_chat_messages,
                                             max_context_characters=worker.settings.max_chat_characters,
                                             workflow_version=version, execution_agent=execution_agent,
                                             execution_strategy=snapshot["agent"].get("strategy", "staged"),
                                             review_failure_policy=snapshot["agent"].get("review_failure_policy", "fail"),
                                             review_revision_policy=snapshot["agent"].get("review_revision_policy", "off"))
                return build_controlled_agent(coordinator=roles["think"],
                                              subagents=[roles["plan"], roles["execute"], roles["review"]],
                                              hooks=host, checkpointer=saver, require_durable_ids=True,
                                              scheduler=scheduler, nested_delegation=True,
                                              max_context_messages=worker.settings.max_chat_messages,
                                              max_context_characters=worker.settings.max_chat_characters)

            initial_scheduler = HierarchicalDelegation(max_active=2, max_depth=2)
            agent = build_agent(initial_scheduler)
            state = await agent.aget_state(config)
            if version == INTERACTIVE_V10_WORKFLOW_VERSION and plugin_versions == SEALED_PLUGIN_VERSIONS and state.values:
                from .v10_sealed_saver import verify_pinned_seal_inventory
                saved_id = state.config.get("configurable", {}).get("checkpoint_id")
                with worker.transaction() as db:
                    verify_pinned_seal_inventory(db, ticket, saved_id, worker.settings)
            if version in STAGE_WORKFLOWS:
                _publish_stage_results(worker, identity, state.values)
            if state.values and not state.next:
                if version == INTERACTIVE_V10_WORKFLOW_VERSION:
                    from .correction_reservation import correction_invocation_id
                    from .models import RunBudgetReservation
                    with worker.transaction() as db:
                        prior = db.scalar(select(OperationRecord.operation_id).where(
                            OperationRecord.run_id == identity,
                            OperationRecord.node_id == "v10-correction:model_calls").limit(1))
                        reservation = db.get(RunBudgetReservation, (identity, correction_invocation_id(identity)))
                    if prior is not None or reservation is not None:
                        _review(worker, identity, "CORRECTION_RECOVERY_REVIEW",
                                "已有纠错预约或结果，需要核对；未重复调用。", started=started)
                        return
                if unsettled:
                    _review(worker, identity, "ACTIVE_TIME_UNCERTAIN", "上次执行的活跃时间未结算，需要核对。")
                    return
                _complete_checkpoint(worker, identity, state.values, started=started)
                return
            with worker.transaction() as db:
                current = db.get(Run, identity)
                current_status = current.status if current else None
            interactive = version in {INTERACTIVE_STAGE_WORKFLOW_VERSION, "stage-agent-v7", INTERACTIVE_COMPAT_WORKFLOW_VERSION, INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}
            if state.interrupts and current_status == "pause_requested":
                # Checkpoint commit won the race with the business paused
                # update; recover the pause rather than automatically resume.
                status = _pause(worker, identity, "checkpoint_recovered", started=started)
                if status != "running":
                    return
            if unsettled:
                if interactive and _recover_checkpointed_question(worker, identity, state):
                    return
                _review(worker, identity, "ACTIVE_TIME_UNCERTAIN", "上次执行的活跃时间未结算，需要核对。")
                return
            if state.interrupts:
                if interactive:
                    with worker.transaction() as db:
                        signals = _user_resume_map(db, identity, state.interrupts)
                    if signals is None:
                        if not _wait_for_user(worker, identity, started=started, checkpoint_state=state):
                            return
                        with worker.transaction() as db:
                            signals = _user_resume_map(db, identity, state.interrupts)
                        if signals is None:
                            raise AgentOperationNeedsReview("Clarification readiness changed")
                    payload = Command(resume=signals)
                elif version in WORKSPACE_WORKFLOWS:
                    with worker.transaction() as db:
                        signals = approval_resume_map(db, identity, state.interrupts)
                    if signals is None:
                        _wait_for_approval(worker, identity, started=started)
                        return
                    payload = Command(resume=signals)
                else:
                    payload = Command(resume="continue")
            elif state.values:
                payload = None
            else:
                payload = {"messages": history if version in WORKSPACE_WORKFLOWS else
                           [(item["role"], item["content"]) for item in history]}
            while True:
                scheduler = HierarchicalDelegation(max_active=2, max_depth=2)
                agent = build_agent(scheduler)
                action = None
                last_publish = time.monotonic()
                mock_correction = bool(
                    version == INTERACTIVE_V10_WORKFLOW_VERSION
                    and plugin_versions == SEALED_PLUGIN_VERSIONS
                    and getattr(worker, "_v10_mock_correction_test_only", False) is True
                    and custom_factory is not None
                    and identity not in getattr(worker, "_v10_drain_tainted", set())
                    and (snapshot.get("action_contract") or {}).get("auto_correction_enabled") is True
                    and 1 <= len(((snapshot.get("action_contract") or {}).get("explicit") or {}).get("actions", [])) <= 2
                )
                graph_task = None
                try:
                    # Context propagates to all tracked descendants.
                    with tracing_context(enabled=False):
                        if mock_correction:
                            from .v10_drain import start_host_graph
                            task = start_host_graph(worker, ticket=ticket, graph=agent, executor=current_executor,
                                saver=saver, scheduler=scheduler, payload=payload, config=config)
                        else:
                            task = asyncio.create_task(agent.ainvoke(payload, config, durability="sync"))
                        graph_task = task
                    while not task.done():
                        await asyncio.wait({task}, timeout=worker.settings.poll_seconds)
                        worker.consume_commands()
                        if version in STAGE_WORKFLOWS and time.monotonic() - last_publish >= 0.5:
                            saved_state = await agent.aget_state(config)
                            _publish_stage_results(worker, identity, saved_state.values)
                            last_publish = time.monotonic()
                        if worker.cancelled(identity):
                            action = "cancelled"
                            task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await task
                            break
                        if time.monotonic() - started >= remaining:
                            action = "timeout"
                            task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await task
                            break
                    try:
                        result = task.result() if action is None else None
                    except Exception:
                        if version in STAGE_WORKFLOWS:
                            saved_state = await agent.aget_state(config)
                            _publish_stage_results(worker, identity, saved_state.values)
                        raise
                finally:
                    try:
                        if task and not task.done():
                            task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await task
                    finally:
                        # Drain before any business terminal/paused transition;
                        # the saver and owner are still live during cleanup.
                        await scheduler.aclose()
                    task = None
                if version in STAGE_WORKFLOWS:
                    state = await agent.aget_state(config)
                    _publish_stage_results(worker, identity, state.values)
                if mock_correction and (action is not None or result.get("__interrupt__")):
                    from .v10_drain import abandon_host_graph
                    abandon_host_graph(worker, identity)
                if action == "cancelled":
                    _finish(worker, identity, "cancelled", started=started)
                    return
                if action == "timeout":
                    _review(worker, identity, "ACTIVE_TIME_EXCEEDED", "任务达到活跃时间上限；在途调用结果需核对。", started=started)
                    return
                if result.get("__interrupt__"):
                    state = await agent.aget_state(config)
                    if not state.interrupts or not state.next:
                        raise AgentOperationNeedsReview("Interrupt is missing its persisted checkpoint")
                    if interactive and any(isinstance(item.value, dict) and
                            item.value.get("type") in {"user_clarification", "user_barrier"} for item in state.interrupts):
                        with worker.transaction() as db:
                            signals = _user_resume_map(db, identity, state.interrupts)
                        if signals is None:
                            if not _wait_for_user(worker, identity, started=started, checkpoint_state=state):
                                return
                            with worker.transaction() as db:
                                signals = _user_resume_map(db, identity, state.interrupts)
                            if signals is None:
                                raise AgentOperationNeedsReview("Clarification readiness changed")
                        payload = Command(resume=signals)
                        continue
                    if version in WORKSPACE_WORKFLOWS:
                        with worker.transaction() as db:
                            current = db.get(Run, identity)
                            pause_requested = current.status == "pause_requested"
                            signals = approval_resume_map(db, identity, state.interrupts)
                        if not pause_requested and signals is None:
                            _wait_for_approval(worker, identity, started=started)
                            return
                    status = _pause(worker, identity, "checkpoint_saved", started=started)
                    if status == "running":
                        # Resume won the race after checkpoint commit, before
                        # mark_paused acquired the Run lock; do not abandon it.
                        payload = Command(resume=signals if version in WORKSPACE_WORKFLOWS else "continue")
                        continue
                    return
                if mock_correction:
                    from .correction_model import dispatch_reserved_correction
                    from .correction_reservation import reserve_correction_model
                    from .v10_drain import capture_drained_execution

                    await capture_drained_execution(worker, ticket=ticket, graph=agent, executor=current_executor,
                        saver=saver, scheduler=scheduler, graph_task=graph_task)
                    checkpoint_id = state.config.get("configurable", {}).get("checkpoint_id")
                    reserved = await reserve_correction_model(worker, identity, ticket=ticket,
                        expected_checkpoint_id=checkpoint_id, graph=agent, executor=current_executor, checkpointer=saver)
                    if reserved.get("status") == "RESERVED":
                        correction_task = asyncio.create_task(dispatch_reserved_correction(
                            worker, identity, ticket=ticket, root_id=checkpoint_id,
                            graph=agent, executor=current_executor, checkpointer=saver))
                        while not correction_task.done():
                            await asyncio.wait({correction_task}, timeout=worker.settings.poll_seconds)
                            # The dispatcher can await a bounded read-only
                            # broker probe while holding the owner transaction.
                            # Do not nest another transaction on that connection.
                            if worker.lock.connection.in_transaction():
                                continue
                            worker.consume_commands()
                            from .models import Workspace, WorkspaceLease, WorkspacePolicy
                            with worker.transaction() as db:
                                frozen_workspace = snapshot.get("workspace") or {}
                                current_workspace = db.get(Workspace, frozen_workspace.get("id"))
                                current_lease = db.get(WorkspaceLease, frozen_workspace.get("id"))
                                current_policy = db.get(WorkspacePolicy,
                                    (frozen_workspace.get("id"), frozen_workspace.get("policy_version")))
                                workspace_revoked = bool(
                                    not current_workspace or not current_workspace.enabled
                                    or current_workspace.policy_version != frozen_workspace.get("policy_version")
                                    or not current_lease or current_lease.run_id != identity or not current_policy
                                    or current_policy.grant != frozen_workspace.get("grant")
                                    or current_policy.tool_policy != frozen_workspace.get("tool_policy"))
                            if (worker.cancelled(identity) or worker.pause_requested(identity)
                                    or workspace_revoked or time.monotonic() - started >= remaining):
                                await _cancel_correction_task(correction_task)
                                _review(worker, identity, "CORRECTION_RESULT_UNKNOWN",
                                        "纠错模型调用已中断，结果需要核对；不会自动重发。", started=started)
                                return
                        correction = correction_task.result()
                    else:
                        _review(worker, identity, "CORRECTION_RESERVATION_REVIEW",
                                "纠错预约不可用，需要核对；未调用模型。", started=started)
                        return
                    if correction.get("status") != "RECORDED":
                        _review(worker, identity, "CORRECTION_RESULT_UNKNOWN",
                                "纠错模型调用状态需要核对；不会自动重发。", started=started)
                        return
                    if (getattr(worker, "_v10_mock_tool_execution_test_only", False) is True
                            and correction.get("batch_status") == "VALIDATED_NOT_DISPATCHED"):
                        await _run_test_correction_tools(worker, identity, ticket=ticket, root_id=checkpoint_id,
                            graph=agent, executor=current_executor, execution_saver=saver, snapshot=snapshot,
                            role_versions=role_versions, role_bindings=role_bindings, plugin_versions=plugin_versions,
                            started=started, remaining=remaining)
                        _review(worker, identity, "CORRECTION_TOOL_REVIEW",
                                "纠错工具记录已保存，需要核对执行结果。", started=started)
                        return
                    # This isolated mock path never dispatches a tool; the
                    # required action remains for reconciliation.
                    _review(worker, identity, "CORRECTION_NOT_DISPATCHED",
                            "纠错候选已评估，工具操作尚未执行，需要核对。", started=started)
                    return
                _complete_checkpoint(worker, identity, result, started=started)
                return
    except (WorkerOwnershipLost, DelegationDrainTimeout):
        raise
    except StageOutputError as exc:
        if exc.output is not None:
            output = exc.output
            issue = output_issue(text=output.text, reasoning=output.reasoning, finish_reason=output.finish_reason,
                                 refusal=output.refusal, require_finish=True)
            try:
                _retain_incomplete_output(worker, identity, output,
                                          {"finish_reason": output.finish_reason,
                                           "output_status": issue.status if issue else "incomplete",
                                           "stage": exc.stage})
            except AgentOperationNeedsReview:
                _review(worker, identity, "OUTPUT_CONFLICT", "保存的阶段输出与本次结果不一致，需要核对。", started=started)
                return
        with worker.transaction() as db:
            uncertain = db.scalar(select(OperationRecord.operation_id).where(
                OperationRecord.run_id == identity, OperationRecord.status.in_(["running", "uncertain"])).limit(1))
        if uncertain is not None:
            _review(worker, identity, exc.code, exc.public_message, started=started)
        else:
            _finish(worker, identity, "failed", exc.code, exc.public_message, started=started)
    except ProviderError as exc:
        # Known invalid output is a failure; any unrelated concurrent operation
        # with an unknown outcome still requires review rather than replay.
        with worker.transaction() as db:
            uncertain = db.scalar(select(OperationRecord.operation_id).where(
                OperationRecord.run_id == identity, OperationRecord.status.in_(["running", "uncertain"])).limit(1))
        if uncertain is not None:
            _review(worker, identity, exc.code, exc.public_message, started=started)
        else:
            _finish(worker, identity, "failed", exc.code, exc.public_message, started=started)
    except InvalidToolLimitReached:
        _invalid_tool_limit(worker, identity, started=started)
    except AgentBudgetExceeded:
        _budget_exceeded(worker, identity, started=started)
    except AgentContextLimitExceeded:
        _context_exceeded(worker, identity, started=started)
    except ContextPolicyError as exc:
        from .context_host import record_context_rejection
        record_context_rejection(worker, identity, exc)
        with worker.transaction() as db:
            unknown = db.scalar(select(OperationRecord.operation_id).where(
                OperationRecord.run_id == identity, OperationRecord.status.in_(["running", "uncertain"])).limit(1))
        if unknown:
            _review(worker, identity, exc.code, public_context_error(exc.code), started=started)
        else:
            _finish(worker, identity, "failed", exc.code, public_context_error(exc.code), started=started)
    except (AgentOperationNeedsReview, ReservationConflict):
        _review(worker, identity, "OPERATION_UNCERTAIN", "模型或工具调用结果不确定，需要核对；未自动重放。", started=started)
    except AgentCancellationRequested:
        _finish(worker, identity, "cancelled", started=started)
    except AgentControlPending:
        _review(worker, identity, "AGENT_CONTROL_REVIEW", "控制命令需要核对，补充要求仍保留；未自动重放。",
                preserve_requirements=True, started=started)
    except ServiceError as exc:
        if exc.code in {"HISTORY_SCOPE_INVALID", "HISTORY_SOURCE_INVALID"}:
            _review(worker, identity, exc.code, exc.message, started=started)
        else:
            _review(worker, identity, "AGENT_CHECKPOINT_REVIEW", "控制命令或检查点需要核对；未自动重放。", started=started)
    except FencingError:
        _review(worker, identity, "AGENT_CHECKPOINT_REVIEW", "控制命令或检查点需要核对；未自动重放。", started=started)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _review(worker, identity, "AGENT_EXECUTION_REVIEW", "Agent 执行中断，需要核对；未自动重放。", started=started,
                diagnostic=_root_diagnostic(exc))
    finally:
        if correction_task and not correction_task.done():
            await _cancel_correction_task(correction_task)
        if correction_task and correction_task.done() and not correction_task.cancelled():
            with contextlib.suppress(Exception):
                correction_task.exception()
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if worker.lock is not None and not worker.lock.lost:
            _release_terminal_run(worker, identity)


async def _run_test_correction_tools(worker, identity, *, ticket, root_id, graph, executor, execution_saver,
                                     snapshot, role_versions, role_bindings, plugin_versions, started, remaining):
    """Exercise the real gateway only with explicitly injected test services."""
    from .artifacts import ArtifactStore
    from .correction_gateway_binding import build_bound_correction_graph
    from .correction_phase import prepare_correction_phase
    from .m4_runtime import ExecutionBoundary, M4AgentHost
    from .m4_tools import WorkspaceToolGateway
    from .v10_correction_saver import V10CorrectionSaver
    from .v10_namespace_prototype import CorrectionNamespacePrototype

    broker = getattr(worker, "workspace_broker", None)
    sandbox = getattr(worker, "sandbox_executor", None)
    if broker is None or sandbox is None or getattr(worker, "agent_model_factory", None) is None:
        raise AgentOperationNeedsReview("Isolated correction test services are unavailable")
    phase = await prepare_correction_phase(worker, identity, ticket=ticket, root_id=root_id,
                                           graph=graph, executor=executor, checkpointer=execution_saver)
    if phase.get("status") != "PREPARED" or phase.get("tool_dispatch") is not False:
        raise AgentOperationNeedsReview("Correction phase is unavailable")
    async with open_fenced_saver(_checkpoint_url(worker.settings.database_url), ticket) as base:
        writer = V10CorrectionSaver(base, worker=worker, ticket=ticket, root_id=root_id,
                                    graph=graph, executor=executor, execution_saver=execution_saver)
        scheduler = HierarchicalDelegation(max_active=1, max_depth=2)
        tool_task = None
        try:
            host = M4AgentHost(worker, identity, "stage-agent-v10", role_versions=role_versions,
                               role_bindings=role_bindings, plugin_versions=plugin_versions)
            host.scheduler = scheduler
            host._active_segment_started = started
            gateway = WorkspaceToolGateway(host=host, workspace=snapshot["workspace"], broker=broker,
                                           sandbox=sandbox, artifacts=ArtifactStore(worker.settings.data_dir / "artifacts"),
                                           scheduler=scheduler)
            boundary = ExecutionBoundary(host=host, gateway=gateway, scheduler=scheduler)
            mapped = CorrectionNamespacePrototype(writer, run_id=identity, prefix=writer._namespace)
            correction_graph, payload, config = await build_bound_correction_graph(
                host=host, boundary=boundary, saver=writer, checkpointer=mapped)
            tool_task = asyncio.create_task(correction_graph.ainvoke(payload, config, durability="sync"))
            while not tool_task.done():
                await asyncio.wait({tool_task}, timeout=worker.settings.poll_seconds)
                if writer._active_writes or worker.lock.connection.in_transaction():
                    continue
                worker.consume_commands()
                if worker.cancelled(identity) or worker.pause_requested(identity) or time.monotonic() - started >= remaining:
                    await _cancel_correction_task(tool_task)
                    raise AgentOperationNeedsReview("Correction tool execution was interrupted")
            result = tool_task.result()
            if (not isinstance(result, dict) or result.get("__interrupt__") or result.get("stop_reason")
                    or len(result.get("records", [])) != len(payload.get("missing_actions", []))):
                raise AgentOperationNeedsReview("Correction tool results require reconciliation")
        finally:
            if tool_task and not tool_task.done():
                await _cancel_correction_task(tool_task)
            await scheduler.aclose()
            await writer.close_writes()


async def _cancel_correction_task(task):
    """Bound cleanup even if the caller receives repeated cancellation."""
    if not task.done():
        task.cancel()
    waiter = asyncio.create_task(asyncio.wait({task}, timeout=10))
    cancelled = False
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            cancelled = True
    _, pending = waiter.result()
    if pending:
        # A durable DISPATCHED claim remains unknown. Terminate the worker
        # rather than allow a second request while the old one is in flight.
        task.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
        raise DelegationDrainTimeout("Correction model did not stop before timeout")
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.exception()
    if cancelled:
        raise asyncio.CancelledError


def _release_terminal_run(worker, identity):
    from .m4_services import release_workspace_lease

    terminal = False
    with worker.transaction() as db:
        run = db.get(Run, identity)
        if run and run.status in TERMINAL:
            terminal = release_workspace_lease(db, identity) is True
    # Do not forget an owner ticket before the transaction commits.
    if terminal:
        getattr(worker, "_v10_sealed_tickets", {}).pop(identity, None)


async def recover_agents(worker):
    with worker.transaction() as db:
        active = [(run.id, run.status) for run in db.scalars(select(Run).where(
            Run.status.in_(["running", "pause_requested", "cancel_requested"])))
                  if run.config_snapshot.get("workflow_type") == "agent"]
    for identity, status in active:
        if status == "cancel_requested":
            worker.finish(identity, "cancelled")
        else:
            await run_agent(worker, identity)
