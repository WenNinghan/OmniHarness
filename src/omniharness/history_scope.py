"""Versioned access to same-conversation history; never grants file tools."""
from sqlalchemy import select

from .models import Clarification, Message, Run, Workspace


def normalize_history_scope(body):
    """Freeze explicit selections deterministically; derive defaults for new Runs."""
    if body.history_scope is None:
        scope = {"schema_version": 1, "kind": "workspace" if body.workspace_id else "unscoped",
                 "workspace_id": body.workspace_id, "additional_run_ids": []}
    else:
        scope = body.history_scope.model_dump()
        scope["additional_run_ids"] = sorted(scope["additional_run_ids"])
    return scope


def source_scope(run):
    """Old workspace-linked Runs have known scope; old null Runs remain unassigned."""
    snapshot = run.config_snapshot if isinstance(run.config_snapshot, dict) else {}
    scope = snapshot.get("history_scope")
    if isinstance(scope, dict) and scope.get("schema_version") == 1:
        return scope.get("kind"), scope.get("workspace_id")
    if run.workspace_id:
        return "workspace", run.workspace_id
    return "legacy_unassigned", None


def require_current_scope_authorized(db, run):
    """Recheck a frozen scope at a dispatch boundary; old Run contracts are unchanged."""
    from .services import TERMINAL, ServiceError

    snapshot = run.config_snapshot if isinstance(run.config_snapshot, dict) else {}
    scope = snapshot.get("history_scope")
    if scope is None:
        return
    if (not isinstance(scope, dict) or type(scope.get("schema_version")) is not int
            or scope.get("schema_version") != 1 or scope.get("kind") not in {"workspace", "unscoped"}
            or not isinstance(scope.get("additional_run_ids"), list)
            or len(scope["additional_run_ids"]) > 100
            or any(not isinstance(identity, str) or not 1 <= len(identity) <= 36 for identity in scope["additional_run_ids"])
            or len(scope["additional_run_ids"]) != len(set(scope["additional_run_ids"]))
            or (scope["kind"] == "workspace") != bool(scope.get("workspace_id"))):
        raise ServiceError("HISTORY_SCOPE_INVALID", "保存的历史范围无效。", 409)
    if scope["kind"] == "workspace":
        workspace = db.get(Workspace, scope["workspace_id"])
        if workspace is None or not workspace.enabled:
            raise ServiceError("HISTORY_SCOPE_INVALID", "历史范围中的项目不可用。", 409)
    ids = scope["additional_run_ids"]
    if ids:
        sources = db.scalars(select(Run).where(Run.id.in_(ids), Run.conversation_id == run.conversation_id,
                                               Run.ordinal < run.ordinal, Run.status.in_(TERMINAL))).all()
        if {source.id for source in sources} != set(ids):
            raise ServiceError("HISTORY_SOURCE_INVALID", "所选历史来源已不可用。", 409)


def select_history(db, run):
    """Return eligible public messages and minimal decisions; old Runs retain old semantics."""
    from .services import message_is_context

    require_current_scope_authorized(db, run)
    rows = db.execute(select(Message, Run).join(Run, Message.run_id == Run.id).where(
        Run.conversation_id == run.conversation_id, Run.ordinal <= run.ordinal
    ).order_by(Run.ordinal, Message.role.desc())).all()
    snapshot = run.config_snapshot if isinstance(run.config_snapshot, dict) else {}
    scope = snapshot.get("history_scope")
    legacy = scope is None
    if not legacy and (not isinstance(scope, dict) or scope.get("schema_version") != 1
                       or scope.get("kind") not in {"workspace", "unscoped"}
                       or not isinstance(scope.get("additional_run_ids"), list)):
        raise ValueError("Unsupported frozen history scope")
    selected, decisions = [], []
    # Only earlier terminal Runs whose user message has passed the same scope
    # checks below can contribute answered clarifications. The current Run's
    # own answers are restored through its checkpoint, not injected here.
    from .services import TERMINAL
    previous_ids = {origin.id for row, origin in rows
                    if not legacy and row.role == "user" and row.content
                    and origin.id != run.id and origin.status in TERMINAL
                    and (origin.id in scope["additional_run_ids"]
                         or source_scope(origin) == (scope["kind"], scope.get("workspace_id")))}
    answered_by_run = {}
    if previous_ids:
        for item in db.scalars(select(Clarification).where(
                Clarification.run_id.in_(previous_ids), Clarification.status == "answered",
                Clarification.answer.is_not(None)).order_by(
                Clarification.answered_at, Clarification.created_at, Clarification.id)):
            if isinstance(item.answer, str) and item.answer.strip():
                answered_by_run.setdefault(item.run_id, []).append(item)
    for row, origin in rows:
        if not row.content:
            continue
        allowed = message_is_context(row)
        reason = "incomplete_answer" if not allowed else "same_scope"
        if allowed and not legacy:
            if origin.id == run.id:
                allowed, reason = row.role == "user", "current_input" if row.role == "user" else "incomplete_answer"
            elif origin.id in scope["additional_run_ids"]:
                reason = "explicitly_selected"
            else:
                kind, workspace_id = source_scope(origin)
                allowed = kind == scope["kind"] and workspace_id == scope.get("workspace_id")
                if not allowed:
                    reason = "legacy_unassigned" if kind == "legacy_unassigned" else "scope_mismatch"
        if allowed:
            selected.append({"id": row.id, "role": row.role, "content": row.content})
            if row.role == "user":
                for item in answered_by_run.get(origin.id, ()):
                    selected.extend([
                        {"id": "clarify-q-" + item.id, "role": "assistant",
                         "content": "此前的澄清问题：\n" + item.question},
                        {"id": "clarify-a-" + item.id, "role": "user",
                         "content": "用户当时的回答：\n" + item.answer},
                    ])
        if not legacy:
            decisions.append({"message_id": row.id, "source_run_id": origin.id,
                              "disposition": "included" if allowed else "excluded", "reason": reason})
    return selected, decisions
