"""Bounded public metadata about scoped history and prepared model inputs.

Events describe host-side selection and dispatch intent, not provider receipt.
No message text, credentials, private reasoning, or provider signatures are kept.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from .models import ArtifactVersion, Message, Run, RunEvent
from .services import add_event

_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_REASONS = {"current_input", "same_scope", "explicitly_selected", "scope_mismatch",
            "legacy_unassigned", "incomplete_answer"}
_PAGE_SIZE = 50
_MAX_ITEMS = 200


def _safe_id(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def record_history_selection(db, run, selections):
    """Persist scope decisions once, using only ID/reason metadata.

    This list is input selection before compaction; it does not claim that
    selected messages were directly sent to a provider.
    """
    if not isinstance(run.config_snapshot, dict) or not run.config_snapshot.get("history_scope"):
        return
    if not selections:
        return
    locked = db.scalar(select(Run).where(Run.id == run.id).with_for_update())
    if locked is None:
        return
    previous = db.scalar(select(RunEvent.id).where(
        RunEvent.run_id == run.id, RunEvent.event_type == "context.history_selection").limit(1))
    if previous is not None:
        return
    items = []
    for row in selections:
        if not isinstance(row, dict):
            continue
        message_id, source_run_id = _safe_id(row.get("message_id")), _safe_id(row.get("source_run_id"))
        disposition, reason = row.get("disposition"), row.get("reason")
        if message_id and source_run_id and disposition in {"included", "excluded"} and reason in _REASONS:
            items.append({"message_id": message_id, "source_run_id": source_run_id,
                          "disposition": disposition, "reason": reason})
    visible = items[:_MAX_ITEMS]
    pages = max(1, (len(visible) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    for page in range(pages):
        add_event(db, locked, "context.history_selection", {
            "schema_version": 1, "page": page + 1, "pages": pages,
            "total": len(items), "truncated": len(items) > _MAX_ITEMS,
            "items": visible[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE],
            "meaning": "history_selection_before_compaction",
        })


def model_input_callback(host, role, identity, messages, *, system_present=False, tool_count=0,
                         system_message=None, tool_schemas=()):
    """Build an on-reserved callback bound to this exact model invocation."""
    message_ids = [_safe_id(getattr(message, "id", None)) for message in messages]
    message_ids = [item for item in message_ids if item]
    summary_ids = []
    for message in messages:
        value = getattr(message, "id", None)
        if isinstance(value, str) and value.startswith("omni-summary:"):
            summary = _safe_id(value[len("omni-summary:"):])
            if summary:
                summary_ids.append(summary)

    def on_reserved(db, _operation):
        run = db.scalar(select(Run).where(Run.id == host.run_id).with_for_update())
        if run is None or not isinstance(run.config_snapshot, dict):
            return
        rows = db.execute(select(Message.id, Message.run_id).join(Run, Message.run_id == Run.id).where(
            Run.conversation_id == run.conversation_id, Run.ordinal <= run.ordinal,
            Message.id.in_(message_ids[:_MAX_ITEMS]))).all() if message_ids else []
        found = {row.id: row.run_id for row in rows}
        direct = [{"message_id": identity_, "source_run_id": found[identity_]} for identity_ in message_ids
                  if identity_ in found][:_MAX_ITEMS]
        summaries = db.scalars(select(ArtifactVersion).where(
            ArtifactVersion.run_id == run.id, ArtifactVersion.kind == "context_summary",
            ArtifactVersion.id.in_(summary_ids[:_MAX_ITEMS]))).all() if summary_ids else []
        summaries_by_id = {item.id: item for item in summaries}
        coverage = []
        for sid in summary_ids[:_MAX_ITEMS]:
            artifact = summaries_by_id.get(sid)
            if artifact is None:
                continue
            sources = artifact.metadata_json.get("sources", []) if isinstance(artifact.metadata_json, dict) else []
            for source in sources if isinstance(sources, list) else []:
                mid = _safe_id(source.get("message_id")) if isinstance(source, dict) else None
                if mid:
                    coverage.append({"message_id": mid, "summary_id": sid})
        # A summary source can be synthetic; only expose IDs from this conversation.
        coverage_truncated = len(coverage) > _MAX_ITEMS
        source_ids = [item["message_id"] for item in coverage][:_MAX_ITEMS]
        permitted = set(db.scalars(select(Message.id).join(Run, Message.run_id == Run.id).where(
            Run.conversation_id == run.conversation_id, Run.ordinal <= run.ordinal,
            Message.id.in_(source_ids))).all()) if source_ids else set()
        coverage = [item for item in coverage if item["message_id"] in permitted][:_MAX_ITEMS]
        role_key = host.role_bindings.get(role)
        role_info = (run.config_snapshot.get("agent") or {}).get("roles", {}).get(role_key, {})
        model = role_info.get("model") or {}
        manifest = {"schema_version": 1, "status": "unavailable", "cache_hit": "unknown"}
        try:
            from .context_manifest import input_manifest
            key = (host.worker.settings.secret_dir / "master.key").read_bytes().strip()
            manifest = input_manifest(messages, system_message=system_message, tools=tool_schemas,
                                      model=model, key=key, run_id=run.id)
        except (ValueError, TypeError, OSError, AttributeError):
            # Observability cannot invent a manifest or alter dispatch when
            # a legacy/test host does not provide the required inputs.
            pass
        add_event(db, run, "context.model_input", {
            "schema_version": 1, "status": "dispatch_intent", "role": role,
            "invocation_id": identity.invocation_id, "agent_run_id": host._agent_run_id(identity.invocation_id),
            "model_config_id": _safe_id(model.get("id")), "model_version": model.get("version"),
            "direct": direct, "summary_coverage": coverage,
            "summary_ids": [sid for sid in summary_ids if sid in summaries_by_id][:_MAX_ITEMS],
            "system_present": bool(system_present), "tool_count": tool_count,
            "provider_receipt": "unknown", "counting": "unknown",
            "manifest": manifest,
            "truncated": len(message_ids) > _MAX_ITEMS or len(summary_ids) > _MAX_ITEMS or coverage_truncated,
        })
    return on_reserved
