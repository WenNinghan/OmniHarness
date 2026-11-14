"""Read-only, bounded projections of recorded evidence for one Run.

Snapshot completeness describes recorded events up to a fixed sequence only.
It never means an agent, model provider, or verifier received or proved them.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from sqlalchemy import func, select

from .models import Run, RunEvent
from .security import SecretStore
from .services import ServiceError

_EVENTS = ("handoff.prepared", "review.evidence_recorded")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CURSOR_TTL = 15 * 60
_PAGE_BYTES = 256 * 1024
_ITEM_BYTES = _PAGE_BYTES - 8192


def _invalid_cursor():
    raise ServiceError("EVIDENCE_CURSOR_INVALID", "证据游标无效或已过期，请重新加载。", 409)


def _encoded_size(value):
    return max(len(json.dumps(value, ensure_ascii=ascii_mode, allow_nan=False).encode("utf-8"))
               for ascii_mode in (False, True))


def _hex(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _uuid(value):
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _capabilities(run):
    version = run.workflow_version
    if version in {"stage-agent-v6", "stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
        return {"handoff": "supported", "review": "supported"}
    if version == "stage-agent-v5":
        return {"handoff": "supported", "review": "not_recorded"}
    if version in {"stage-agent-v1", "stage-agent-v2", "stage-agent-v3", "stage-agent-v4"}:
        return {"handoff": "not_recorded", "review": "not_recorded"}
    snapshot = run.config_snapshot if isinstance(run.config_snapshot, dict) else {}
    if version is None and snapshot.get("workflow_type", "chat") == "chat":
        return {"handoff": "not_recorded", "review": "not_recorded"}
    return {"handoff": "unknown", "review": "unknown"}


def _project_handoff(payload):
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int:
        return None
    if (payload["schema_version"], payload.get("workflow_version")) not in {
            (1, "stage-agent-v5"), (2, "stage-agent-v6"), (2, "stage-agent-v7"), (2, "stage-agent-v8"), (2, "stage-agent-v9"), (2, "stage-agent-v10")}:
        return None
    for key in ("handoff_id", "binding_fingerprint", "source_task_ref", "target_task_ref", "artifact_sha256"):
        if not _hex(payload.get(key)):
            return None
    if not _uuid(payload.get("artifact_id")):
        return None
    if any(type(payload.get(key)) is not bool for key in ("report_truncated", "operations_truncated")):
        return None
    count, included = payload.get("operation_count"), payload.get("operations_included")
    operations, validation = payload.get("host_operations"), payload.get("validation")
    if (not _integer(count, 0, 1_000_000) or not _integer(included, 0, 20)
            or not isinstance(operations, list) or len(operations) != included or included > count
            or payload["operations_truncated"] != (count > included)
            or not isinstance(validation, dict) or validation.get("scope") != "VALIDATED"
            or validation.get("integrity") != "VALIDATED" or validation.get("claim_support") != "UNVERIFIED"
            or payload.get("operation_coverage") != "UNVERIFIED"
            or payload.get("meaning") != "prepared_not_provider_receipt"):
        return None
    safe_operations = []
    for item in operations:
        if (not isinstance(item, dict) or not _hex(item.get("operation_id"))
                or item.get("status") not in {"RECORDED", "UNVERIFIED"}):
            return None
        safe_operations.append({"operation_id": item["operation_id"], "status": item["status"]})
    return {key: payload[key] for key in (
        "schema_version", "workflow_version", "handoff_id", "binding_fingerprint", "source_task_ref",
        "target_task_ref", "artifact_id", "artifact_sha256", "report_truncated", "operation_count",
        "operations_included", "operations_truncated", "operation_coverage", "meaning",
    )} | {"host_operations": safe_operations, "validation": {
        "scope": validation["scope"], "integrity": validation["integrity"],
        "claim_support": validation["claim_support"]}}


def _project_review(payload, workflow_version):
    if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
            or (workflow_version in {"stage-agent-v9", "stage-agent-v10"} and (payload["schema_version"] != 2 or payload.get("workflow_version") != workflow_version))
            or (workflow_version not in {"stage-agent-v9", "stage-agent-v10"} and payload["schema_version"] != 1)):
        return None
    if any(not _hex(payload.get(key)) for key in (
            "review_invocation_id", "binding_fingerprint", "report_sha256", "review_result_sha256")):
        return None
    if any(not _uuid(payload.get(key)) for key in ("report_artifact_id", "review_result_artifact_id")):
        return None
    if (not _integer(payload.get("finding_count"), 0, 20)
            or any(not _integer(payload.get(key), 0, 160) for key in (
                "references_count", "validated_count", "unverified_count"))
            or payload["validated_count"] + payload["unverified_count"] != payload["references_count"]
            or payload.get("claim_support") != "UNVERIFIED" or type(payload.get("truncated")) is not bool
            or payload.get("meaning") != "recorded_not_truth"):
        return None
    return {**({"workflow_version": workflow_version} if workflow_version in {"stage-agent-v9", "stage-agent-v10"} else {}), **{key: payload[key] for key in (
        "schema_version", "review_invocation_id", "binding_fingerprint", "report_artifact_id", "report_sha256",
        "review_result_artifact_id", "review_result_sha256", "finding_count", "references_count",
        "validated_count", "unverified_count", "claim_support", "truncated", "meaning",
    )}}


def _project(row, workflow_version):
    safe = (_project_handoff(row.payload) if row.event_type == "handoff.prepared"
            else _project_review(row.payload, workflow_version) if row.event_type == "review.evidence_recorded" else None)
    if (row.event_type == "handoff.prepared" and safe is not None
            and safe.get("workflow_version") != workflow_version):
        safe = None
    if row.event_type == "review.evidence_recorded" and workflow_version not in {"stage-agent-v6", "stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
        safe = None
    # RunEvent IDs are generated by the host. If a malformed record is ever
    # encountered, do not copy an arbitrary identifier into a public response.
    event_id = row.id if _uuid(row.id) else hashlib.sha256(
        (row.run_id + ":" + str(row.id)).encode("utf-8")).hexdigest()
    if not _uuid(row.id):
        safe = None
    result = {"sequence_number": row.sequence_number, "event_id": event_id, "event_type": row.event_type}
    if safe is None:
        result["unsupported_schema"] = True
    else:
        result["payload"] = safe
    return result, safe is None


def _decode_cursor(secrets, cursor, run_id, watermark_now):
    try:
        value = json.loads(secrets.decrypt(cursor))
        if not isinstance(value, dict) or set(value) != {
            "purpose", "schema_version", "run_id", "watermark", "last_sequence", "returned",
            "initial_total", "incomplete", "issued", "expiry"}:
            _invalid_cursor()
        now = int(time.time())
        if (value["purpose"] != "evidence-view-v1" or type(value["schema_version"]) is not int
                or value["schema_version"] != 1 or value["run_id"] != run_id
                or not _integer(value["watermark"], 0, watermark_now)
                or not _integer(value["last_sequence"], 0, value["watermark"])
                or not _integer(value["initial_total"], 0, value["watermark"])
                or not _integer(value["returned"], 0, value["initial_total"])
                or type(value["incomplete"]) is not bool
                or not _integer(value["issued"], 0, now) or type(value["expiry"]) is not int
                or value["expiry"] != value["issued"] + _CURSOR_TTL or now >= value["expiry"]):
            _invalid_cursor()
        return value
    except (RuntimeError, ValueError, TypeError, KeyError, UnicodeError):
        _invalid_cursor()


def evidence_page(db, settings, run_id, *, cursor=None, limit=25):
    """Project one authenticated Run's evidence events under a fixed watermark.

    Caller owns the DB transaction and has already authenticated the session.
    The Run row lock synchronizes watermark/count reads with event appenders.
    """
    if not _integer(limit, 1, 100):
        raise ServiceError("EVIDENCE_LIMIT_INVALID", "证据分页大小无效。", 422)
    run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None:
        raise ServiceError("NOT_FOUND", "任务不存在。", 404)
    current_watermark = run.next_event_sequence - 1
    secrets = SecretStore(settings)
    capabilities = _capabilities(run)
    base = (RunEvent.run_id == run.id, RunEvent.event_type.in_(_EVENTS))
    if cursor is None:
        watermark, last, returned = current_watermark, 0, 0
        total = db.scalar(select(func.count()).select_from(RunEvent).where(
            *base, RunEvent.sequence_number <= watermark))
        issued = int(time.time())
        expiry = issued + _CURSOR_TTL
        incomplete = "unknown" in capabilities.values()
    else:
        state = _decode_cursor(secrets, cursor, run.id, current_watermark)
        watermark, last, returned = state["watermark"], state["last_sequence"], state["returned"]
        total, issued, expiry, incomplete = state["initial_total"], state["issued"], state["expiry"], state["incomplete"]
        count_now = db.scalar(select(func.count()).select_from(RunEvent).where(
            *base, RunEvent.sequence_number <= watermark))
        previous_now = db.scalar(select(func.count()).select_from(RunEvent).where(
            *base, RunEvent.sequence_number <= last))
        if count_now != total or previous_now != returned:
            incomplete = True
    rows = list(db.scalars(select(RunEvent).where(*base, RunEvent.sequence_number > last,
                                                   RunEvent.sequence_number <= watermark)
                           .order_by(RunEvent.sequence_number).limit(limit + 1)))
    items = []
    for row in rows[:limit]:
        item, unsupported = _project(row, run.workflow_version)
        if _encoded_size([*items, item]) > _ITEM_BYTES:
            if not items:
                raise ServiceError("EVIDENCE_VIEW_TOO_LARGE", "证据记录超出分页上限。", 409)
            break
        items.append(item)
        incomplete = incomplete or unsupported
    new_last = items[-1]["sequence_number"] if items else last
    new_returned = returned + len(items)
    remaining = len(rows) > len(items)
    if not remaining and new_returned != total:
        incomplete = True
    complete = not remaining and not incomplete and new_returned == total
    if remaining:
        next_state = {"purpose": "evidence-view-v1", "schema_version": 1, "run_id": run.id,
                      "watermark": watermark, "last_sequence": new_last, "returned": new_returned,
                      "initial_total": total, "incomplete": incomplete, "issued": issued, "expiry": expiry}
        next_cursor = secrets.encrypt(json.dumps(next_state, ensure_ascii=True, separators=(",", ":")))
    else:
        next_cursor = None
    legacy = complete and total == 0 and all(value == "not_recorded" for value in capabilities.values())
    status = "legacy_unrecorded" if legacy else "incomplete" if incomplete else "complete" if complete else "loading"
    response = {"schema_version": 1, "run_id": run.id, "watermark": watermark, "snapshot_total": total,
                "items": items, "next_cursor": next_cursor, "snapshot_complete": complete,
                "snapshot_status": status, "recording_capabilities": capabilities}
    if _encoded_size(response) > _PAGE_BYTES:
        raise ServiceError("EVIDENCE_VIEW_TOO_LARGE", "证据分页超出大小限制。", 409)
    return response
