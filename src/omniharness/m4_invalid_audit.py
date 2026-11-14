"""Version-bound, fenced audit of schema failures before any tool dispatch."""
from __future__ import annotations

import hashlib
import hmac
import json
from copy import deepcopy

from cryptography.fernet import Fernet
from sqlalchemy import func, select

from .m3_agents import AgentBoundaryError, AgentBudgetExceeded, invocation_identity
from .m3_budget import ReservationResult, reserve_invocation
from .m3_host import AgentOperationNeedsReview
from .m4_services import finish_tool_request
from .models import Clarification, OperationRecord, RunBudgetReservation, RunCommand, ToolOperation, now
from .services import operation_id

POLICY = "invalid-tool-audit-v1"
V9_POLICY = "invalid-tool-audit-v9-v1"
V10_POLICY = "invalid-tool-audit-v10-v1"


def _domain(version):
    if version == "stage-agent-v8":
        return POLICY, "v8-tool-preparation", "invalid-tool-audit-key-v1"
    if version == "stage-agent-v9":
        return V9_POLICY, "v9-tool-preparation", "invalid-tool-audit-key-v9-v1"
    if version == "stage-agent-v10":
        return V10_POLICY, "v10-tool-preparation", "invalid-tool-audit-key-v10-v1"
    raise AgentOperationNeedsReview("Unsupported invalid tool audit version")
TASK_TOOL_LIMIT = 3
RUN_LIMIT = 6


class InvalidToolLimitReached(AgentBoundaryError):
    """A committed invalid-input limit must stop the graph, including on replay."""


def _key(settings, version="stage-agent-v8"):
    _domain(version)
    try:
        raw = (settings.secret_dir / "master.key").read_bytes().strip()
        Fernet(raw)  # Reject absent, invalid or corrupt key material.
        label = {"stage-agent-v8": b"omniharness/invalid-tool-audit/v1/key",
                 "stage-agent-v9": b"omniharness/invalid-tool-audit/v9/v1/key",
                 "stage-agent-v10": b"omniharness/invalid-tool-audit/v10/v1/key"}[version]
        return hmac.new(raw, label, hashlib.sha256).digest()
    except (OSError, ValueError, TypeError):
        raise AgentOperationNeedsReview("Invalid tool audit key is unavailable") from None


def _mac(key, domain, value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise AgentBoundaryError("Invocation input cannot be bound safely") from None
    return hmac.new(key, domain.encode("ascii") + b"\0" + encoded, hashlib.sha256).hexdigest()


def _safe_errors(exc, policy=POLICY):
    fields, categories = set(), set()
    allowed = {"expected_sha256", "path", "content", "query", "limit", "offset", "command",
               "cwd", "timeout_seconds", "server_id", "tool_name", "arguments", "artifact_id",
               "question", "reason", "options"}
    for item in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = item.get("loc", ())
        field = loc[0] if loc and isinstance(loc[0], str) and loc[0] in allowed else "other"
        kind = item.get("type", "")
        category = ("MISSING" if kind == "missing" else "EXTRA_FIELD" if kind == "extra_forbidden"
                    else "WRONG_TYPE" if kind.endswith("_type") else "INVALID_FORMAT"
                    if kind in {"string_pattern_mismatch", "string_too_short", "string_too_long",
                                "too_short", "too_long", "greater_than_equal", "less_than_equal"}
                    else "OTHER")
        fields.add(field)
        categories.add(category)
    return {"validation_stage": "schema", "fields": sorted(fields), "categories": sorted(categories),
            "policy": policy}


def _identity(request, host, scope, name, key):
    # The existing helper verifies the graph identity and JSON serializability;
    # its unkeyed argument digest is deliberately never persisted here.
    legacy = invocation_identity(request, kind="tool_calls", role="execute", tool_name=name)
    host._verify_identity(legacy)
    call = request.tool_call
    identity_data = [host.run_id, legacy.checkpoint_ns, legacy.task_id, call["id"]]
    invocation = _mac(key, "invocation", identity_data)
    task = _mac(key, "task", [host.run_id, scope["id"]])
    call_id = _mac(key, "call", identity_data)
    binding = _mac(key, "binding", [host.run_id, name, call.get("args")])
    return legacy, invocation, task, call_id, binding


def _verify_key(db, run_id, key, *, create, version="stage-agent-v8"):
    policy, preparation, marker_node = _domain(version)
    marker_id = operation_id(run_id, marker_node, {})
    if version == "stage-agent-v9":
        # A v9 Run must never adopt a v8 audit ledger or key marker.
        if (db.get(OperationRecord, operation_id(run_id, "invalid-tool-audit-key-v1", {})) is not None
                or db.scalar(select(OperationRecord.operation_id).where(
                    OperationRecord.run_id == run_id,
                    OperationRecord.node_id == "v8-tool-preparation").limit(1))
                or db.scalar(select(ToolOperation.id).where(
                    ToolOperation.run_id == run_id, ToolOperation.tool_version == POLICY).limit(1))):
            raise AgentOperationNeedsReview("Invalid tool audit version differs from saved Run")
    if version == "stage-agent-v10":
        for old_version in ("stage-agent-v8", "stage-agent-v9"):
            old_policy, old_preparation, old_marker = _domain(old_version)
            if (db.get(OperationRecord, operation_id(run_id, old_marker, {})) is not None
                    or db.scalar(select(OperationRecord.operation_id).where(
                        OperationRecord.run_id == run_id,
                        OperationRecord.node_id == old_preparation).limit(1))
                    or db.scalar(select(ToolOperation.id).where(
                        ToolOperation.run_id == run_id, ToolOperation.tool_version == old_policy).limit(1))):
                raise AgentOperationNeedsReview("Invalid tool audit version differs from saved Run")
    marker = db.get(OperationRecord, marker_id)
    verifier = _mac(key, "key-verifier", run_id)
    if marker is None:
        if (db.scalar(select(ToolOperation.id).where(ToolOperation.run_id == run_id,
                      ToolOperation.tool_version == policy).limit(1)) or
            db.scalar(select(OperationRecord.operation_id).where(OperationRecord.run_id == run_id,
                      OperationRecord.node_id == preparation).limit(1)) or
            db.scalar(select(Clarification.id).where(Clarification.run_id == run_id,
                      Clarification.request_key.startswith("tool:")).limit(1))):
            raise AgentOperationNeedsReview("Invalid tool audit key verification is missing")
        if create:
            db.add(OperationRecord(operation_id=marker_id, run_id=run_id,
                node_id=marker_node, parameters_hash=verifier, status="completed",
                attempts=1, workflow_version=version, finished_at=now()))
            db.flush()
    elif ((version in {"stage-agent-v9", "stage-agent-v10"} and marker.run_id != run_id)
          or marker.node_id != marker_node or marker.parameters_hash != verifier
          or marker.workflow_version != version or marker.status != "completed"):
        raise AgentOperationNeedsReview("Invalid tool audit key does not match saved Run")


def _scope():
    from .m4_collaboration import current_task_scope
    try:
        scope = current_task_scope()
        if not scope["id"]:
            raise ValueError
        return scope
    except (ValueError, RuntimeError):
        raise AgentBoundaryError("Durable task scope is unavailable") from None


def _counts(db, run_id, task, name, policy=POLICY):
    base = select(func.count()).select_from(ToolOperation).where(
        ToolOperation.run_id == run_id, ToolOperation.error_code == "INVALID_ARGUMENTS",
        ToolOperation.tool_version == policy)
    return (db.scalar(base),
            db.scalar(base.where(ToolOperation.task_id == task, ToolOperation.tool_name == name)))


def _valid_claim(host, request, name):
    key = _key(host.worker.settings, host.workflow_version)
    legacy, invocation, task, _, input_binding = _identity(request, host, _scope(), name, key)
    binding = _mac(key, "valid-preparation-binding", [input_binding, task])
    claim_id = operation_id(host.run_id, _domain(host.workflow_version)[1], {"invocation_id": legacy.invocation_id})
    return key, legacy, invocation, binding, claim_id


def _check_invalid_reservation(db, run_id, invocation):
    if db.get(RunBudgetReservation, {"run_id": run_id, "invocation_id": invocation}):
        raise AgentOperationNeedsReview("Previously invalid invocation cannot be dispatched")


def claim_valid_preparation(db, host, request, name):
    """Claim a valid identity under the fenced Run lock, before a snapshot.

    This marker is not a dispatch or a budget reservation. It survives a crash
    so a retry with the same binding can continue, but an invalid reuse cannot
    race past a valid request that is preparing a snapshot or approval.
    """
    key, legacy, invocation, binding, claim_id = _valid_claim(host, request, name)
    _verify_key(db, host.run_id, key, create=True, version=host.workflow_version)
    _check_invalid_reservation(db, host.run_id, invocation)
    claim = db.get(OperationRecord, claim_id)
    if claim is None:
        assert_not_limited(db, host.run_id, host.workflow_version)
        if db.get(RunBudgetReservation, {"run_id": host.run_id, "invocation_id": legacy.invocation_id}):
            raise AgentOperationNeedsReview("Tool identity is already reserved")
        db.add(OperationRecord(operation_id=claim_id, run_id=host.run_id, node_id=_domain(host.workflow_version)[1],
            parameters_hash=binding, status="completed", attempts=1, workflow_version=host.workflow_version,
            result={"kind": "valid"}, finished_at=now()))
        db.flush()
    elif (claim.run_id != host.run_id or claim.node_id != _domain(host.workflow_version)[1] or
          claim.workflow_version != host.workflow_version or claim.status != "completed" or
          claim.parameters_hash != binding or claim.result != {"kind": "valid"}):
        raise AgentOperationNeedsReview("Tool preparation binding differs")


def check_valid_reservation(db, host, request, name, *, require_claim):
    """Recheck the invalid ledger in the very transaction that reserves."""
    key, _, invocation, binding, claim_id = _valid_claim(host, request, name)
    _verify_key(db, host.run_id, key, create=True, version=host.workflow_version)
    _check_invalid_reservation(db, host.run_id, invocation)
    if require_claim:
        claim = db.get(OperationRecord, claim_id)
        if (claim is None or claim.run_id != host.run_id or claim.node_id != _domain(host.workflow_version)[1] or
            claim.workflow_version != host.workflow_version or claim.status != "completed" or
            claim.parameters_hash != binding or claim.result != {"kind": "valid"}):
            raise AgentOperationNeedsReview("Tool preparation identity is unavailable")


def assert_delegation_unclaimed(db, run_id, identity, version="stage-agent-v8"):
    """Prevent a task delegation from taking a workspace/ask invocation ID."""
    claim_id = operation_id(run_id, _domain(version)[1], {"invocation_id": identity.invocation_id})
    if db.get(OperationRecord, claim_id) is not None:
        raise AgentOperationNeedsReview("Tool identity is already being prepared")


def assert_not_limited(db, run_id, version="stage-agent-v8"):
    """Call under the fenced Run row lock immediately before a new dispatch.

    Read the persisted decision, rather than recomputing from mutable runtime
    state. The Run lock held by the caller serializes this with audit commits.
    """
    policy, _, _ = _domain(version)
    records = db.scalars(select(OperationRecord.result).where(
        OperationRecord.run_id == run_id, OperationRecord.node_id == "agent:execute:tool_calls",
        OperationRecord.workflow_version == version, OperationRecord.status == "completed"))
    if any(isinstance(result, dict) and result.get("policy") == policy and result.get("limit_reached") is True
           for result in records):
        raise InvalidToolLimitReached("工具参数不符合要求，该次操作未派发。")


async def guard_valid_invocation(host, request, name):
    """Prevent an invalid invocation ID from being reused as a valid dispatch."""
    key = _key(host.worker.settings, host.workflow_version)
    legacy, invocation, _, _, _ = _identity(request, host, _scope(), name, key)
    async with host.serial:
        with host.worker.transaction() as db:
            host._run(db, "execute", allow_supplements=True)
            _verify_key(db, host.run_id, key, create=False, version=host.workflow_version)
            if db.get(RunBudgetReservation, {"run_id": host.run_id, "invocation_id": invocation}):
                raise AgentOperationNeedsReview("Previously invalid invocation cannot be dispatched")
            previous = db.get(RunBudgetReservation, {"run_id": host.run_id,
                                                      "invocation_id": legacy.invocation_id})
            ledger_id = operation_id(host.run_id, "agent:execute:tool_calls",
                                     {"invocation_id": legacy.invocation_id})
            ledger = db.get(OperationRecord, ledger_id)
            # A completed ordinary result remains readable; the normal replay
            # path still verifies its complete frozen binding before returning.
            if not (previous is not None and ledger is not None and ledger.status == "completed"
                    and isinstance(ledger.result, dict) and isinstance(ledger.result.get("response"), dict)):
                assert_not_limited(db, host.run_id, host.workflow_version)


async def audit_invalid(host, request, name, validation_error):
    await host.check_control("tool_calls", "execute", name)
    key = _key(host.worker.settings, host.workflow_version)
    legacy, invocation, task, call_id, input_binding = _identity(request, host, _scope(), name, key)
    tool_id = _mac(key, "tool-operation", [host.run_id, invocation])
    record_id = operation_id(host.run_id, "agent:execute:tool_calls", {"invocation_id": invocation})
    response = {"error": "INVALID_ARGUMENTS", "message": (
        "工具参数不符合格式。新建文件可省略 expected_sha256 或使用 JSON null；修改已有文件须先读取并提供准确 SHA-256。"
        if name == "workspace_patch" else "工具参数不符合已声明格式，该次操作未派发。")}
    async with host.serial:
        if host.worker.lock is None:
            raise AgentBoundaryError("A live Worker execution lock is required")
        with host.worker.transaction() as db:
            run, role = host._run(db, "execute", allow_supplements=True)
            if db.scalar(select(RunCommand.id).where(RunCommand.run_id == host.run_id,
                    RunCommand.status == "pending", RunCommand.command_type == "append_requirement").limit(1)):
                from .m3_host import AgentControlPending
                raise AgentControlPending("Requirements have not yet been checkpointed")
            _verify_key(db, host.run_id, key, create=True, version=host.workflow_version)
            # A formerly valid operation sharing the same graph identity may not
            # be reclassified as an invalid input on re-entry.
            if db.get(RunBudgetReservation, {"run_id": run.id, "invocation_id": legacy.invocation_id}):
                raise AgentOperationNeedsReview("Saved invocation binding differs")
            claim_id = operation_id(run.id, _domain(host.workflow_version)[1], {"invocation_id": legacy.invocation_id})
            claim = db.get(OperationRecord, claim_id)
            claim_binding = _mac(key, "valid-preparation-binding", [input_binding, task])
            if claim is not None and (claim.node_id != _domain(host.workflow_version)[1] or
                    claim.workflow_version != host.workflow_version or claim.status != "completed" or
                    claim.result != {"kind": "invalid"} or claim.parameters_hash != claim_binding):
                raise AgentOperationNeedsReview("Tool invocation is already being prepared")
            binding = _mac(key, "frozen-binding", [input_binding, role, host.plugin_versions, name])
            existing = db.get(ToolOperation, tool_id)
            if existing is not None:
                reserved = db.get(RunBudgetReservation, {"run_id": run.id, "invocation_id": invocation})
                ledger = db.get(OperationRecord, record_id)
                if (claim is None or existing.binding_fingerprint != binding or existing.task_id != task or
                    existing.tool_call_id != call_id or existing.tool_name != name or
                    existing.status != "failed" or existing.error_code != "INVALID_ARGUMENTS" or
                    existing.slot_claimed or reserved is None or reserved.kind != "tool_calls" or
                    reserved.binding_fingerprint != binding or ledger is None or ledger.status != "completed" or
                    not isinstance(ledger.result, dict) or ledger.result.get("binding_fingerprint") != binding or
                    ledger.result.get("response") != existing.result):
                    raise AgentOperationNeedsReview("Saved invalid invocation binding differs")
                response = deepcopy(existing.result)
                limit_reached = bool(ledger.result.get("limit_reached"))
                run_count, task_count = _counts(db, run.id, task, name, _domain(host.workflow_version)[0])
                limit_reached = limit_reached or run_count >= RUN_LIMIT or task_count >= TASK_TOOL_LIMIT
                if not limit_reached:
                    assert_not_limited(db, run.id, host.workflow_version)
            else:
                assert_not_limited(db, run.id, host.workflow_version)
                if claim is not None:
                    raise AgentOperationNeedsReview("Invalid invocation has incomplete durable evidence")
                db.add(OperationRecord(operation_id=claim_id, run_id=run.id, node_id=_domain(host.workflow_version)[1],
                    parameters_hash=claim_binding, status="completed", attempts=1,
                    workflow_version=host.workflow_version, result={"kind": "invalid"}, finished_at=now()))
                db.flush()
                # Lock the frozen budget before counting and inserting. A no-op
                # UPDATE inside reserve_invocation serializes SQLite and PG.
                reservation = reserve_invocation(db, run_id=run.id, kind="tool_calls",
                    invocation_id=invocation, binding_fingerprint=binding)
                if reservation == ReservationResult.EXHAUSTED:
                    raise AgentBudgetExceeded("Run tool budget exhausted")
                if reservation != ReservationResult.RESERVED or db.get(OperationRecord, record_id):
                    raise AgentOperationNeedsReview("Invalid invocation has incomplete durable evidence")
                run_count, task_count = _counts(db, run.id, task, name, _domain(host.workflow_version)[0])
                if run_count >= RUN_LIMIT or task_count >= TASK_TOOL_LIMIT:
                    # Raising rolls the speculative reservation back.
                    raise InvalidToolLimitReached("工具参数不符合要求，该次操作未派发。")
                limit_reached = run_count + 1 >= RUN_LIMIT or task_count + 1 >= TASK_TOOL_LIMIT
                ledger = OperationRecord(operation_id=record_id, run_id=run.id,
                    node_id="agent:execute:tool_calls", parameters_hash=invocation,
                    status="completed", attempts=1, workflow_version=host.workflow_version, finished_at=now(),
                    result={"binding_fingerprint": binding, "response": deepcopy(response),
                            "limit_reached": limit_reached, "policy": _domain(host.workflow_version)[0]})
                db.add(ledger)
                db.flush()
                row = ToolOperation(id=tool_id, run_id=run.id, operation_record_id=record_id,
                    task_id=task, tool_call_id=call_id, tool_name=name, tool_version=_domain(host.workflow_version)[0],
                    parameters=_safe_errors(validation_error, _domain(host.workflow_version)[0]), binding_fingerprint=binding,
                    status="authorized", slot_claimed=False)
                db.add(row)
                db.flush()
                finish_tool_request(db, tool_id, "failed", result=response, error_code="INVALID_ARGUMENTS")
    if limit_reached:
        raise InvalidToolLimitReached("工具参数不符合要求，该次操作未派发。")
    return response
