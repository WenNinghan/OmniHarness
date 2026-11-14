"""Bounded, immutable Run evidence for reviewers; never executes a tool."""
from __future__ import annotations

import codecs
import hashlib
import hmac
import json
import os
import stat

from sqlalchemy import select

from .artifacts import ArtifactStore
from .models import (
    ArtifactVersion,
    Message,
    OperationRecord,
    Run,
    RunBudgetReservation,
    RunCommand,
    RunEvent,
    ToolOperation,
    WorkspaceSnapshot,
)
from .services import ServiceError, add_event

EVIDENCE_VERSION = "host-evidence-v1"


def _clip(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {"text": text[:limit], "truncated": len(text) > limit}


def build_review_evidence(db, run, store, *, operation_ids=None, max_characters=16000):
    """Only DB-selected rows from this Run may contribute to the packet.

    Caller owns the fenced transaction. A model supplied ID is a filter, never
    authority. No path from a model is opened here; ArtifactStore verifies the
    immutable object's size, digest and scope before returning text.
    """
    operations = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run.id)
                                 .order_by(ToolOperation.created_at, ToolOperation.id)))
    selected = set(operation_ids) if operation_ids is not None else None
    observed, changed = {}, set()
    for op in operations:
        result = op.result or {}
        if op.status == "completed" and op.tool_name in ("workspace_read", "workspace_patch"):
            path, digest = op.parameters.get("path"), result.get("sha256")
            if path and isinstance(digest, str):
                observed[path] = digest
                if op.tool_name == "workspace_patch":
                    changed.add(path)
    all_targets = {path: observed[path] for path in sorted(changed or observed.keys())}
    targets = {}
    for path, digest in all_targets.items():
        if len(json.dumps({**targets, path: digest}, ensure_ascii=False)) <= max_characters // 4:
            targets[path] = digest
    workspace = run.config_snapshot.get("workspace", {})
    packet = {"schema_version": EVIDENCE_VERSION, "run_id": run.id, "workspace_id": run.workspace_id,
              "target_versions": targets, "operations": [], "omitted_operations": 0,
              "omitted_target_versions": len(all_targets) - len(targets),
              "assessment": "UNVERIFIED", "note": "Recorded facts are not an overall acceptance verdict."}
    # Presentation order must not change chronological version resolution.
    priority = {"workspace_patch": 0, "run_command": 1}
    ordered = sorted(reversed(operations), key=lambda op: priority.get(op.tool_name, 2))
    for op in ordered:
        if selected is not None and op.id not in selected:
            continue
        if len(json.dumps(packet, ensure_ascii=False)) > max_characters - 1000:
            packet["omitted_operations"] += 1
            continue
        result = op.result or {}
        ledger = db.get(OperationRecord, op.operation_record_id) if op.operation_record_id else None
        recorded = bool(ledger and ledger.run_id == run.id and ledger.status == "completed")
        item = {"operation_id": op.id, "tool": op.tool_name, "status": op.status,
                "ledger_completed": recorded, "evidence_status": "RECORDED" if recorded else "UNVERIFIED",
                "artifacts": []}
        if op.tool_name in ("workspace_read", "workspace_patch"):
            item.update(path=op.parameters.get("path"), sha256=result.get("sha256"))
        if op.tool_name == "workspace_patch":
            content = op.parameters.get("content")
            matches = (isinstance(content, str)
                       and hashlib.sha256(content.encode("utf-8")).hexdigest() == result.get("sha256"))
            item.update(before_sha256=result.get("before_sha256"), payload_digest_matches=matches)
            if matches:
                item["resulting_source"] = _clip(content, 2500)
            if not matches or op.status != "completed" or item.get("resulting_source", {}).get("truncated"):
                item["evidence_status"] = "UNVERIFIED"
        if op.tool_name == "run_command":
            snapshot = db.get(WorkspaceSnapshot, op.snapshot_id) if op.snapshot_id else None
            manifest = snapshot.manifest if snapshot is not None else {}
            scoped = bool(snapshot and snapshot.run_id == run.id and snapshot.workspace_id == run.workspace_id
                          and snapshot.policy_version == workspace.get("policy_version")
                          and manifest.get("run_id") == run.id and manifest.get("workspace_id") == run.workspace_id
                          and manifest.get("snapshot_id") == snapshot.id)
            files = {x.get("path"): x.get("sha256") for x in manifest.get("files", [])} if scoped else {}
            matched = (bool(targets) and len(targets) == len(all_targets) and scoped
                       and all(files.get(path) == digest for path, digest in targets.items()))
            item.update(command=_clip(op.parameters.get("command"), 2500), cwd=op.parameters.get("cwd"),
                        exit_code=result.get("exit_code"), snapshot_id=op.snapshot_id,
                        snapshot_scope_valid=scoped, target_versions_match=matched,
                        snapshot_version_scope="initial_copy_only", runtime_mutations_verified=False,
                        tested_versions={path: files.get(path) for path in targets})
            # Shell can edit its private copy before testing. Initial manifest
            # equality does not prove the executed code stayed at that version.
            item["evidence_status"] = "UNVERIFIED"
        artifacts = list(db.scalars(select(ArtifactVersion).where(
            ArtifactVersion.run_id == run.id, ArtifactVersion.operation_id == op.id)))
        for artifact in artifacts:
            if artifact.workspace_id != run.workspace_id:
                continue
            ref = {"artifact_id": artifact.id, "kind": artifact.kind, "name": artifact.name,
                   "sha256": artifact.sha256, "size_bytes": artifact.size_bytes, "verified": False}
            if artifact.kind not in ("command_log", "tool_result"):
                item["artifacts"].append(ref)
                continue
            try:
                text, end = store.read_text_page(artifact, run_id=run.id, limit=3500)
                ref.update(verified=True, excerpt=_clip(text, 1500), truncated=end < artifact.size_bytes)
                if artifact.kind == "command_log":
                    ref["complete"] = (artifact.metadata_json.get("complete") is True
                                       and artifact.metadata_json.get("snapshot_id") == op.snapshot_id)
                    if not ref["complete"] or ref["truncated"] or ref["excerpt"]["truncated"]:
                        item["evidence_status"] = "UNVERIFIED"
            except (ServiceError, OSError, ValueError):
                ref["error"] = "EVIDENCE_UNAVAILABLE"
                item["evidence_status"] = "UNVERIFIED"
            item["artifacts"].append(ref)
        if op.tool_name == "run_command" and not any(a["kind"] == "command_log" for a in item["artifacts"]):
            item["evidence_status"] = "UNVERIFIED"
        if len(json.dumps({**packet, "operations": [*packet["operations"], item]}, ensure_ascii=False)) > max_characters:
            packet["omitted_operations"] += 1
        else:
            packet["operations"].append(item)
    return packet


def v9_request_mac(settings, run, op):
    """Bind the tool request independently of the snapshot's mutable record."""
    from .m4_invalid_audit import _key, _mac
    version = "v10" if run.workflow_version == "stage-agent-v10" else "v9"
    return _mac(_key(settings, f"stage-agent-{version}"), f"{version}-recorded-tool-request", {
        "run_id": run.id, "workspace_id": run.workspace_id, "operation_id": op.id,
        "tool": op.tool_name, "tool_version": op.tool_version, "parameters": op.parameters,
        "binding_fingerprint": op.binding_fingerprint, "snapshot_id": op.snapshot_id})


def v9_snapshot_mac(settings, run, op, snapshot):
    """Bind initial snapshot scope/content as a separate evidence dimension."""
    from .m4_invalid_audit import _key, _mac
    value = None if snapshot is None else {
        "id": snapshot.id, "run_id": snapshot.run_id, "workspace_id": snapshot.workspace_id,
        "task_id": snapshot.task_id, "policy_version": snapshot.policy_version,
        "manifest": snapshot.manifest, "workdir": snapshot.workdir}
    version = "v10" if run.workflow_version == "stage-agent-v10" else "v9"
    return _mac(_key(settings, f"stage-agent-{version}"), f"{version}-recorded-initial-snapshot", {
        "run_id": run.id, "workspace_id": run.workspace_id, "operation_id": op.id,
        "snapshot_id": op.snapshot_id, "snapshot": value})


def v9_response_mac(settings, run, operation_id, request_mac, response):
    from .m4_invalid_audit import _key, _mac
    version = "v10" if run.workflow_version == "stage-agent-v10" else "v9"
    return _mac(_key(settings, f"stage-agent-{version}"), f"{version}-recorded-tool-response", {
        "run_id": run.id, "workspace_id": run.workspace_id, "operation_id": operation_id,
        "request_mac": request_mac, "response": response})


def _v9_tool_binding(run, op, purpose):
    from .m4_contract import M4_V9_PLUGIN_VERSIONS, M4_V10_PLUGIN_VERSIONS
    from .stage_contract import STAGE_ROLE_BINDINGS
    if not isinstance(purpose, str) or not purpose:
        return None
    try:
        role_snapshot = run.config_snapshot["agent"]["roles"][STAGE_ROLE_BINDINGS["execute"]]
        plugins = M4_V9_PLUGIN_VERSIONS
        if run.workflow_version == "stage-agent-v10":
            from .v10_sealed_saver import SEALED_PLUGIN_VERSIONS
            frozen = run.config_snapshot["agent"]["plugin_versions"]
            if frozen not in (M4_V10_PLUGIN_VERSIONS, SEALED_PLUGIN_VERSIONS):
                return None
            plugins = frozen
        data = {"request": op.binding_fingerprint, "kind": "tool_calls", "role": "execute",
                "tool": op.tool_name, "role_snapshot": role_snapshot, "plugins": plugins}
        if purpose != "task":
            data["purpose"] = purpose
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    except (KeyError, TypeError, ValueError):
        return None


class _BoundedEvidenceStore:
    def __init__(self, delegate):
        self.root, self.remaining = delegate.root, 8 * 1024 * 1024

    def read_text_page(self, row, *, run_id=None, offset=0, limit=65536):
        # Open once; check actual handle identity/size before hashing and
        # cap every read even if the file grows concurrently.
        if (row.run_id != run_id or row.storage_path != row.id or row.size_bytes < 0
                or row.size_bytes > 1024 * 1024 or row.size_bytes > self.remaining
                or not 0 <= offset <= row.size_bytes or not 1 <= limit <= 1024 * 1024):
            raise ValueError("Evidence integrity budget or scope exceeded")
        path = self.root / row.id
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or getattr(before, "st_file_attributes", 0) & 0x400):
            raise ValueError("Invalid evidence identity")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or not os.path.samestat(before, opened) or opened.st_size != row.size_bytes):
                raise ValueError("Evidence identity or size changed")
            pieces, digest, left = [], hashlib.sha256(), row.size_bytes
            while left:
                block = os.read(fd, min(left, self.remaining, 65536))
                if not block:
                    raise ValueError("Evidence truncated")
                self.remaining -= len(block)
                left -= len(block)
                digest.update(block)
                pieces.append(block)
            after = os.fstat(fd)
            if (after.st_size != row.size_bytes or not os.path.samestat(opened, after)
                    or not os.path.samestat(after, path.lstat()) or digest.hexdigest() != row.sha256):
                raise ValueError("Evidence changed during verification")
        finally:
            os.close(fd)
        data = b"".join(pieces)[offset:offset + limit + 3]
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        selected = data[:limit]
        content = decoder.decode(selected, final=offset + len(selected) == row.size_bytes)
        consumed = len(selected) - len(decoder.getstate()[0])
        for byte in data[limit:]:
            if consumed or not data:
                break
            selected += bytes([byte])
            content += decoder.decode(bytes([byte]), final=offset + len(selected) == row.size_bytes)
            consumed = len(selected) - len(decoder.getstate()[0])
        return content, offset + consumed


def build_review_evidence_v9(db, run, store, *, settings=None, operation_ids=None, max_characters=16000):
    """Separate record provenance from source identity and semantic support.

    The v1 packet remains untouched for frozen earlier Runs. A completed
    ledger must contain the same result as the tool row before any command
    result is attributed to that ledger.
    """
    if run.workflow_version not in {"stage-agent-v9", "stage-agent-v10"}:
        raise ValueError("Invalid evidence workflow")
    packet = build_review_evidence(db, run, _BoundedEvidenceStore(store), operation_ids=operation_ids,
                                   max_characters=max_characters)
    packet["schema_version"] = "host-evidence-v2"
    packet["workflow_version"] = run.workflow_version
    packet["note"] = ("Host records only; an exit code is not code quality verification. "
                      "Initial snapshot matching does not verify runtime source or claim support.")
    from .services import operation_id
    reservations = list(db.scalars(select(RunBudgetReservation).where(
        RunBudgetReservation.run_id == run.id, RunBudgetReservation.kind == "tool_calls")))
    identities = {}
    for reservation in reservations:
        data = {"run_id": run.id, "invocation": reservation.invocation_id}
        key = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        identities[key] = reservation

    def verified_record(op):
        if not op or op.run_id != run.id or op.status != "completed":
            return False
        reservation = identities.get(op.id)
        ledger = db.get(OperationRecord, op.operation_record_id) if op.operation_record_id else None
        result = ledger.result if ledger and isinstance(ledger.result, dict) else {}
        try:
            valid_mac = bool(settings and isinstance(result.get("v9_request_mac"), str) and
                             hmac.compare_digest(result["v9_request_mac"], v9_request_mac(settings, run, op)))
        except Exception:
            valid_mac = False
        return bool(valid_mac and reservation and ledger and ledger.run_id == run.id and ledger.status == "completed"
                    and ledger.node_id == "agent:execute:tool_calls"
                    and ledger.workflow_version == run.workflow_version
                    and ledger.operation_id == operation_id(run.id, ledger.node_id,
                                                            {"invocation_id": reservation.invocation_id})
                    and result.get("binding_fingerprint") == reservation.binding_fingerprint
                    and reservation.binding_fingerprint == _v9_tool_binding(run, op, result.get("purpose")))

    def verified_response(op):
        if not verified_record(op):
            return False
        ledger = db.get(OperationRecord, op.operation_record_id)
        result = ledger.result
        response = result.get("response")
        try:
            return bool(isinstance(response, dict) and response == op.result
                and isinstance(result.get("v9_response_mac"), str)
                and hmac.compare_digest(result["v9_response_mac"],
                    v9_response_mac(settings, run, op.id, result["v9_request_mac"], response)))
        except Exception:
            return False

    # Every target used by the legacy chronological calculation must itself be
    # backed by a verified same-Run ledger and, for a patch, its payload digest.
    targets_verified = True
    for target in db.scalars(select(ToolOperation).where(ToolOperation.run_id == run.id,
            ToolOperation.status == "completed", ToolOperation.tool_name.in_(["workspace_read", "workspace_patch"]))):
        target_result = target.result if isinstance(target.result, dict) else {}
        path, digest = target.parameters.get("path"), target_result.get("sha256")
        if path and isinstance(digest, str):
            if not verified_response(target):
                targets_verified = False
            if target.tool_name == "workspace_patch":
                content = target.parameters.get("content")
                if not isinstance(content, str) or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
                    targets_verified = False
    packet["target_versions_status"] = "RECORDED" if targets_verified and packet["target_versions"] else "UNVERIFIED"
    if not targets_verified:
        packet["target_versions"] = {}
    for item in packet["operations"]:
        op = db.get(ToolOperation, item["operation_id"])
        recorded = verified_record(op)
        response_verified = verified_response(op)
        if not response_verified:
            for key in ("sha256", "before_sha256", "payload_digest_matches", "resulting_source"):
                item.pop(key, None)
        if not recorded:
            item["ledger_completed"] = None
            item["evidence_status"] = "UNVERIFIED"
            for key in ("path", "sha256", "before_sha256", "payload_digest_matches", "resulting_source", "command", "cwd"):
                item.pop(key, None)
        dimensions = {"execution": "RECORDED" if recorded else "UNVERIFIED",
                      "claim_support": "UNVERIFIED"}
        ledger = db.get(OperationRecord, op.operation_record_id) if op and op.operation_record_id else None
        response = (ledger.result or {}).get("response", {}) if ledger and isinstance(ledger.result, dict) else {}
        committed = response.get("artifacts", []) if isinstance(response, dict) else []
        if not isinstance(committed, list):
            committed = []
        # Only the exact artifact references committed in this ledger response
        # may provide model-visible content, even if another scoped artifact is intact.
        safe_artifacts = []
        for artifact in item["artifacts"]:
            if artifact.get("kind") == "v9_tool_request_binding":
                continue
            row = db.get(ArtifactVersion, artifact.get("artifact_id"))
            refs = [ref for ref in committed if isinstance(ref, dict) and ref.get("id") == artifact.get("artifact_id")]
            fields = ("id", "run_id", "operation_id", "workspace_id", "kind", "name", "sha256", "size_bytes", "metadata_json")
            bound = bool(response_verified and row and len(refs) == 1 and all(refs[0].get(key) == getattr(row, key) for key in fields))
            artifact["ledger_bound"] = bound
            if not bound:
                artifact.pop("excerpt", None)
                artifact["verified"] = False
                artifact["error"] = "ARTIFACT_NOT_BOUND_TO_LEDGER"
            safe_artifacts.append(artifact)
        item["artifacts"] = safe_artifacts
        if op and op.tool_name == "run_command":
            snapshot = db.get(WorkspaceSnapshot, op.snapshot_id) if op.snapshot_id else None
            try:
                snapshot_bound = bool(settings and ledger and isinstance(ledger.result, dict)
                    and isinstance(ledger.result.get("v9_snapshot_mac"), str)
                    and hmac.compare_digest(ledger.result["v9_snapshot_mac"],
                                            v9_snapshot_mac(settings, run, op, snapshot)))
            except Exception:
                snapshot_bound = False
            dimensions.update(
                exit_code="RECORDED" if response_verified and type(item.get("exit_code")) is int else "UNVERIFIED",
                initial_snapshot="MATCHED" if (response_verified and snapshot_bound and targets_verified and item.get("snapshot_scope_valid")
                    and item.get("target_versions_match") and (op.result or {}).get("snapshot_id") == op.snapshot_id)
                    else "UNVERIFIED",
                runtime_source="UNVERIFIED")
            if dimensions["exit_code"] != "RECORDED":
                item["exit_code"] = None
            if dimensions["initial_snapshot"] != "MATCHED":
                item["snapshot_scope_valid"] = None
                item["target_versions_match"] = None
                item["tested_versions"] = {}
            if not recorded:
                item["snapshot_id"] = None
            for artifact in item["artifacts"]:
                artifact["log_integrity"] = (
                    "VERIFIED" if recorded and artifact.get("kind") == "command_log"
                    and artifact.get("ledger_bound") is True and artifact.get("verified") is True and artifact.get("complete") is True
                    and not artifact.get("truncated") and not (artifact.get("excerpt") or {}).get("truncated")
                    else "UNVERIFIED")
        item["dimensions"] = dimensions
    while packet["operations"] and len(json.dumps(packet, ensure_ascii=False)) > max_characters:
        packet["operations"].pop()
        packet["omitted_operations"] += 1
    return packet


def _v9_reference_bindings(item):
    """Bind the exact host facts presented at reservation, without user text in events."""
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    execution = {key: item.get(key) for key in ("operation_id", "tool", "status", "command", "cwd", "path")}
    snapshot = {key: item.get(key) for key in ("snapshot_id", "snapshot_scope_valid",
                                                "target_versions_match", "tested_versions")}
    return {"execution": digest(execution), "exit_code": digest(item.get("exit_code")),
            "initial_snapshot": digest(snapshot)}


async def load_review_evidence(host, *, operation_ids=None):
    async with host.serial:
        with host.worker.transaction() as db:
            run, _ = host._run(db, "review", allow_supplements=True)
            builder = build_review_evidence_v9 if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else build_review_evidence
            options = {"settings": host.worker.settings} if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else {}
            return builder(db, run, ArtifactStore(host.worker.settings.data_dir / "artifacts"),
                           operation_ids=operation_ids, **options)


async def task_evidence_references(host, task_id):
    """Task attribution is host-written into results, never supplied by the model."""
    async with host.serial:
        with host.worker.transaction() as db:
            host._run(db, "execute", allow_supplements=True)
            return [op.id for op in db.scalars(select(ToolOperation).where(ToolOperation.run_id == host.run_id))
                    if (op.result or {}).get("host_task_id") == task_id]


async def persist_task_result(host, task_id, content):
    """Keep complete public claims available without copying them into every handoff."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    name = f"task-{task_id}.md"
    async with host.serial:
        with host.worker.transaction() as db:
            run, _ = host._run(db, "execute", allow_supplements=True)
            existing = db.scalar(select(ArtifactVersion).where(
                ArtifactVersion.run_id == run.id, ArtifactVersion.kind == "task_result",
                ArtifactVersion.name == name, ArtifactVersion.sha256 == digest).limit(1))
            if existing is not None:
                return {"artifact_id": existing.id, "sha256": existing.sha256, "size_bytes": existing.size_bytes}
            artifact = ArtifactStore(host.worker.settings.data_dir / "artifacts").put(
                db, run_id=run.id, workspace_id=run.workspace_id, kind="task_result", name=name,
                content=content, metadata={"authority": "model_claim", "logical_task_id": task_id,
                                          "workflow_version": run.workflow_version})
            return {"artifact_id": artifact["id"], "sha256": artifact["sha256"], "size_bytes": artifact["size_bytes"]}


async def prepare_dependency_handoffs(host, *, task, tasks, task_results, task_goal, constraints, messages):
    """Prepare v5 handoffs from checkpointed dependencies and host-owned records.

    The payload never promotes a model report to a verified fact. The Run row
    stays locked while existing preparation events are checked and appended;
    event ordering alone does not provide business-idempotency.
    """
    from langchain_core.messages import HumanMessage

    from .handoff_contract import (
        build_handoff,
        build_handoff_v6,
        build_handoff_v7,
        build_handoff_v8,
        build_handoff_v9,
        build_handoff_v10,
        handoff_event_payload,
    )
    from .m3_agents import AgentBoundaryError

    def invalid():
        raise AgentBoundaryError("Dependency handoff cannot be verified")

    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 8 or not isinstance(task, dict):
        invalid()
    if not isinstance(task_results, list) or len(task_results) > 8:
        invalid()
    if (not isinstance(task_goal, str) or len(task_goal) > 4000 or not isinstance(constraints, list)
            or len(constraints) > 30 or any(not isinstance(value, str) for value in constraints)
            or not isinstance(messages, list)):
        invalid()
    task_by_id = {}
    for item in tasks:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            invalid()
        if item["id"] in task_by_id:
            invalid()
        task_by_id[item["id"]] = item
    target_id = task.get("id")
    if not isinstance(target_id, str) or task_by_id.get(target_id) != task:
        invalid()
    dependencies = task.get("depends_on")
    if (not isinstance(dependencies, list) or len(dependencies) > 8
            or any(not isinstance(item, str) or not item for item in dependencies)
            or len(set(dependencies)) != len(dependencies)
            or any(item == target_id or item not in task_by_id for item in dependencies)):
        invalid()
    results_by_id = {}
    for result in task_results:
        if not isinstance(result, dict) or not isinstance(result.get("task_id"), str):
            invalid()
        source_id = result["task_id"]
        if source_id in results_by_id or source_id not in task_by_id:
            invalid()
        results_by_id[source_id] = result
    if any(source_id not in results_by_id for source_id in dependencies):
        invalid()

    async with host.serial:
        if host.worker.lock is None:
            invalid()
        with host.worker.transaction() as db:
            # _run obtains the Run row FOR UPDATE and verifies ownership,
            # configuration, active execution and any frozen history scope.
            run, _ = host._run(db, "execute", allow_supplements=True)
            if run.workflow_version not in {"stage-agent-v5", "stage-agent-v6", "stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10"} or not run.workspace_id:
                invalid()
            goal = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "user"))
            if goal is None or not goal.id or not isinstance(goal.content, str) or not goal.content.strip():
                invalid()
            requirements = {}
            for message in messages:
                if not isinstance(message, HumanMessage) or not isinstance(message.id, str):
                    continue
                if not message.id.startswith("omni-command:"):
                    continue
                command_id = message.id.removeprefix("omni-command:")
                command = db.get(RunCommand, command_id)
                if (not command_id or command is None or command.run_id != run.id
                        or command.command_type != "append_requirement"
                        or command.status not in {"pending", "applied"}
                        or not isinstance(command.payload, dict) or not isinstance(message.content, str)
                        or command.payload.get("content") != message.content):
                    invalid()
                value = hashlib.sha256(message.content.encode("utf-8")).hexdigest()
                if command_id in requirements and requirements[command_id] != value:
                    invalid()
                requirements[command_id] = value
            if not dependencies:
                return []
            store = ArtifactStore(host.worker.settings.data_dir / "artifacts")
            recorded_operations = list(db.scalars(select(ToolOperation).where(
                ToolOperation.run_id == run.id).order_by(ToolOperation.created_at, ToolOperation.id)))
            existing_events = list(db.scalars(select(RunEvent).where(
                RunEvent.run_id == run.id, RunEvent.event_type == "handoff.prepared")))
            by_handoff = {}
            for event in existing_events:
                payload = event.payload if isinstance(event.payload, dict) else {}
                identity = payload.get("handoff_id")
                if not isinstance(identity, str) or identity in by_handoff:
                    invalid()
                by_handoff[identity] = payload

            prepared = []
            handoff_reader = _BoundedEvidenceStore(store) if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else None
            for source_id in dependencies:
                result = results_by_id[source_id]
                report, ref = result.get("content"), result.get("result_artifact")
                if (result.get("step_id") != task_by_id[source_id].get("step_id")
                        or not isinstance(report, str) or not report.strip() or not isinstance(ref, dict)
                        or not isinstance(ref.get("artifact_id"), str)):
                    invalid()
                artifact = db.get(ArtifactVersion, ref["artifact_id"])
                metadata = artifact.metadata_json if artifact is not None else None
                digest = hashlib.sha256(report.encode("utf-8")).hexdigest()
                if (artifact is None or artifact.run_id != run.id or artifact.workspace_id != run.workspace_id
                        or artifact.kind != "task_result" or not isinstance(metadata, dict)
                        or metadata.get("authority") != "model_claim" or metadata.get("logical_task_id") != source_id
                        or metadata.get("workflow_version") != run.workflow_version
                        or artifact.sha256 != digest or ref.get("sha256") != digest
                        or artifact.size_bytes != len(report.encode("utf-8"))
                        or ref.get("size_bytes") != artifact.size_bytes):
                    invalid()
                # read() verifies the *entire* file's size and digest before
                # returning even one byte. The checked report has the same
                # digest and is already held in the checkpoint, avoiding a
                # second full copy or repeated full-file reads.
                try:
                    if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"}:
                        handoff_reader.read_text_page(artifact, run_id=run.id, offset=0, limit=1)
                    else:
                        store.read(artifact, run_id=run.id, offset=0, limit=1)
                except (ServiceError, OSError, ValueError):
                    invalid()

                operations = []
                for operation in recorded_operations:
                    data = operation.result if isinstance(operation.result, dict) else {}
                    if data.get("host_task_id") != source_id:
                        continue
                    ledger = db.get(OperationRecord, operation.operation_record_id) if operation.operation_record_id else None
                    recorded = bool(operation.status == "completed" and ledger is not None
                                    and ledger.run_id == run.id and ledger.status == "completed")
                    operations.append({"operation_id": operation.id,
                                       "status": "RECORDED" if recorded else "UNVERIFIED"})
                try:
                    # Bind all frozen task definitions and the actual original
                    # goal/requirement contents by digest; the event exposes
                    # only the resulting opaque hash, never these inputs.
                    fingerprint_input = {"schema_version": 1, "run_id": run.id,
                        "tasks": tasks, "source_task_id": source_id, "target_task_id": target_id,
                        "goal_message_id": goal.id,
                        "goal_sha256": hashlib.sha256(goal.content.encode("utf-8")).hexdigest(),
                        "task_goal": task_goal, "constraints": constraints,
                        "requirements": list(requirements.items())}
                    fingerprint = hashlib.sha256(json.dumps(fingerprint_input, ensure_ascii=False,
                        sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
                    builder = {"stage-agent-v6": build_handoff_v6, "stage-agent-v7": build_handoff_v7, "stage-agent-v8": build_handoff_v8, "stage-agent-v9": build_handoff_v9, "stage-agent-v10": build_handoff_v10}.get(run.workflow_version, build_handoff)
                    envelope = builder(run_id=run.id, workspace_id=run.workspace_id,
                        workflow_version=run.workflow_version, source_task_id=source_id,
                        target_task_id=target_id, goal_message_id=goal.id,
                        task_fingerprint=fingerprint,
                        artifact_id=artifact.id, artifact_sha256=artifact.sha256,
                        report_text=report, operations=operations)
                    payload = handoff_event_payload(envelope)
                except (TypeError, ValueError):
                    invalid()
                if not isinstance(payload, dict) or payload.get("handoff_id") != envelope.handoff_id:
                    invalid()
                event_payload = {"mode": run.mode, **payload}
                if max(len(json.dumps(event_payload, ensure_ascii=ascii_mode).encode("utf-8"))
                       for ascii_mode in (False, True)) > 16 * 1024:
                    invalid()
                prior = by_handoff.get(envelope.handoff_id)
                if prior is not None:
                    if prior != event_payload:
                        invalid()
                else:
                    add_event(db, run, "handoff.prepared", payload)
                    by_handoff[envelope.handoff_id] = event_payload
                prepared.append(envelope.model_dump(mode="json"))
            return prepared


def review_input_on_reserved(host, identity, request_messages):
    """Freeze only host-authenticated references from the final prepared request.

    Called under the same Run lock/transaction as invocation reservation. The
    input manifest is an artifact because completion replaces the operation's
    temporary result. A missing, rewritten or summarized evidence message has
    no provable reference set. This says nothing about provider receipt.
    """
    from langchain_core.messages import HumanMessage

    def on_reserved(db, _row):
        run = db.get(Run, host.run_id)
        if run is None or run.workflow_version not in {"stage-agent-v6", "stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10"} or not run.workspace_id:
            raise ValueError("Review input scope unavailable")
        store = ArtifactStore(host.worker.settings.data_dir / "artifacts")
        matching = [m for m in request_messages if isinstance(m, HumanMessage)
                    and m.id == "omni-evidence:review" and isinstance(m.content, str)]
        allowed, capture_status = [], "UNVERIFIED"
        if len(matching) == 1:
            try:
                source = json.loads(matching[0].content)
                packet = source.get("host_evidence") if isinstance(source, dict) else None
                # Reconstruct under the same Run transaction; an arbitrary user
                # message with a matching ID is never trusted as host evidence.
                # ArtifactStore validates a complete file even for a one-byte
                # read. Bound this extra reconstruction to avoid unbounded IO
                # under the Run lock; larger sets remain explicitly unknown.
                candidates = list(db.scalars(select(ArtifactVersion).where(
                    ArtifactVersion.run_id == run.id,
                    ArtifactVersion.kind.in_(["command_log", "tool_result"]))))
                bounded = (all(0 <= item.size_bytes <= 1024 * 1024 for item in candidates)
                           and sum(item.size_bytes for item in candidates) <= 8 * 1024 * 1024)
                builder = build_review_evidence_v9 if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else build_review_evidence
                expected = builder(db, run, store, **({"settings": host.worker.settings}
                    if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else {})) if (bounded or run.workflow_version in {"stage-agent-v9", "stage-agent-v10"}) else None
                if isinstance(packet, dict) and packet == expected:
                    capture_status = "MATCHED_HOST_PACKET"
                    for op in packet.get("operations", []):
                        if not isinstance(op, dict) or not isinstance(op.get("operation_id"), str):
                            continue
                        artifacts = []
                        for artifact in op.get("artifacts", []):
                            if not isinstance(artifact, dict) or not isinstance(artifact.get("artifact_id"), str):
                                continue
                            excerpt = artifact.get("excerpt") or {}
                            artifacts.append({"artifact_id": artifact["artifact_id"],
                                "sha256": artifact.get("sha256"),
                                "verified": artifact.get("verified") is True,
                                **({"log_integrity": artifact.get("log_integrity")} if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else {}),
                                "truncated": bool(artifact.get("truncated") or
                                                  (isinstance(excerpt, dict) and excerpt.get("truncated")) or
                                                  (artifact.get("kind") == "command_log" and not artifact.get("complete")))})
                        allowed.append({"operation_id": op["operation_id"],
                                        "evidence_status": op.get("evidence_status"),
                                         **({"dimensions": op.get("dimensions"), "bindings": _v9_reference_bindings(op)}
                                            if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else {}),
                                        "artifacts": artifacts})
            except (TypeError, ValueError, ServiceError, OSError):
                allowed, capture_status = [], "UNVERIFIED"
        manifest = {"schema_version": 2 if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else 1, "run_id": run.id, "workspace_id": run.workspace_id,
                    **({"workflow_version": run.workflow_version} if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else {}),
                    "review_invocation_id": identity.invocation_id,
                    "binding_fingerprint": identity.binding_fingerprint,
                    "capture_status": capture_status, "allowed": allowed,
                    "meaning": "prepared_not_provider_receipt"}
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Review input manifest exceeds the limit")
        existing = [item for item in db.scalars(select(ArtifactVersion).where(
            ArtifactVersion.run_id == run.id, ArtifactVersion.kind == ("review_input_manifest_v2" if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else "review_input_manifest_v1")))
            if isinstance(item.metadata_json, dict)
            and item.metadata_json.get("review_invocation_id") == identity.invocation_id]
        if existing:
            raise ValueError("Review input manifest already exists before reservation")
        store.put(db, run_id=run.id, workspace_id=run.workspace_id, kind="review_input_manifest_v2" if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else "review_input_manifest_v1",
                  name="review-input-manifest.json", content=encoded,
                  metadata={"review_invocation_id": identity.invocation_id,
                            "binding_fingerprint": identity.binding_fingerprint})
    return on_reserved


def _bound_review_input(db, run, store, identity):
    rows = [item for item in db.scalars(select(ArtifactVersion).where(
        ArtifactVersion.run_id == run.id, ArtifactVersion.kind == ("review_input_manifest_v2" if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else "review_input_manifest_v1")))
        if isinstance(item.metadata_json, dict)
        and item.metadata_json.get("review_invocation_id") == identity.invocation_id]
    if len(rows) != 1 or rows[0].workspace_id != run.workspace_id:
        raise ValueError("Review input binding unavailable")
    artifact = rows[0]
    if artifact.metadata_json.get("binding_fingerprint") != identity.binding_fingerprint:
        raise ValueError("Review input binding differs")
    reader = _BoundedEvidenceStore(store) if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else store
    text, end = reader.read_text_page(artifact, run_id=run.id, limit=1024 * 1024)
    if end != artifact.size_bytes:
        raise ValueError("Review input manifest truncated")
    value = json.loads(text)
    if (not isinstance(value, dict) or value.get("schema_version") != (2 if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else 1)
            or (run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} and value.get("workflow_version") != run.workflow_version)
            or value.get("run_id") != run.id or value.get("workspace_id") != run.workspace_id
            or value.get("review_invocation_id") != identity.invocation_id
            or value.get("binding_fingerprint") != identity.binding_fingerprint
            or not isinstance(value.get("allowed"), list)):
        raise ValueError("Review input manifest invalid")
    return value


def _validate_review_reference(db, run, store, manifest, candidate, io_budget, *, settings=None):
    """Verify provenance and integrity only; never decide semantic support."""
    malformed_execution_digest = (run.workflow_version in {"stage-agent-v9", "stage-agent-v10"}
                                  and candidate.is_unbound_execution_digest())
    raw = candidate.model_dump(mode="json", exclude_unset=malformed_execution_digest)
    result = {"candidate": raw, "reference_status": "UNVERIFIED",
              "reason": "NOT_IN_BOUND_INPUT_OR_UNAVAILABLE", "claim_support": "UNVERIFIED"}
    if malformed_execution_digest:
        result["reason"] = "EXECUTION_DIGEST_WITHOUT_ARTIFACT_REFERENCE"
        return result
    if manifest.get("capture_status") != "MATCHED_HOST_PACKET":
        return result
    if candidate.start is not None:
        # The current host packet contains excerpts without a byte-to-artifact
        # mapping; even a valid byte range cannot be claimed to have been sent.
        result["reason"] = "RANGE_NOT_MAPPABLE_TO_BOUND_INPUT"
        return result
    allowed = manifest["allowed"]
    matches = [item for item in allowed if isinstance(item, dict)
               and (not candidate.operation_id or item.get("operation_id") == candidate.operation_id)
               and (not candidate.artifact_id or any(
                   isinstance(ref, dict) and ref.get("artifact_id") == candidate.artifact_id
                   for ref in item.get("artifacts", [])))]
    if len(matches) != 1:
        return result
    entry = matches[0]
    operation = db.get(ToolOperation, entry.get("operation_id"))
    if operation is None or operation.run_id != run.id or operation.status != "completed":
        return result
    ledger = db.get(OperationRecord, operation.operation_record_id) if operation.operation_record_id else None
    if (ledger is None or ledger.run_id != run.id or ledger.status != "completed"
            or (run.workflow_version not in {"stage-agent-v9", "stage-agent-v10"} and entry.get("evidence_status") != "RECORDED")):
        return result
    if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"}:
        dimension = getattr(candidate, "dimension", None)
        dimensions = entry.get("dimensions") or {}
        # Recheck the live ledger, snapshot and artifact after reservation;
        # an old matched manifest is not authority for later changes.
        cache = io_budget.setdefault("v9_operations", {})
        if operation.id not in cache:
            artifacts = list(db.scalars(select(ArtifactVersion).where(
                ArtifactVersion.run_id == run.id, ArtifactVersion.operation_id == operation.id,
                ArtifactVersion.kind.in_(["command_log", "tool_result"]))))
            size = sum(a.size_bytes for a in artifacts)
            if (any(a.size_bytes < 0 or a.size_bytes > 1024 * 1024 for a in artifacts)
                    or size > io_budget["remaining"]):
                result["reason"] = "INTEGRITY_CHECK_BUDGET_EXCEEDED"
                return result
            io_budget["remaining"] -= size
            current = build_review_evidence_v9(db, run, store, settings=settings, operation_ids=[operation.id])
            current_ops = current.get("operations", [])
            cache[operation.id] = current_ops[0] if len(current_ops) == 1 else None
        current_op = cache[operation.id]
        current_dimensions = current_op.get("dimensions", {}) if current_op else {}
        if (current_dimensions.get("execution") != "RECORDED"
                or (dimension in {"exit_code", "initial_snapshot"}
                    and current_dimensions.get(dimension) != dimensions.get(dimension))):
            return result
        bindings = entry.get("bindings") or {}
        current_bindings = _v9_reference_bindings(current_op)
        if bindings.get("execution") != current_bindings["execution"]:
            return result
        if dimension in {"exit_code", "initial_snapshot"} and bindings.get(dimension) != current_bindings[dimension]:
            return result
        if candidate.artifact_id:
            current_refs = [a for a in current_op.get("artifacts", [])
                            if a.get("artifact_id") == candidate.artifact_id]
            if len(current_refs) != 1 or current_refs[0].get("log_integrity") != "VERIFIED":
                return result
        if dimensions.get("execution") != "RECORDED":
            return result
        if dimension == "exit_code" and dimensions.get("exit_code") != "RECORDED":
            return result
        if dimension == "initial_snapshot" and dimensions.get("initial_snapshot") != "MATCHED":
            return result
        if dimension not in {"execution", "exit_code", "log", "initial_snapshot"}:
            return result
        if dimension == "log" and not candidate.artifact_id:
            return result
        if dimension != "log" and candidate.artifact_id:
            return result
    if candidate.artifact_id:
        refs = [ref for ref in entry.get("artifacts", []) if ref.get("artifact_id") == candidate.artifact_id]
        artifact = db.get(ArtifactVersion, candidate.artifact_id)
        if (len(refs) != 1 or artifact is None or artifact.run_id != run.id
                or artifact.workspace_id != run.workspace_id or artifact.operation_id != operation.id
                or not candidate.expected_sha256 or candidate.expected_sha256 != artifact.sha256
                or refs[0].get("sha256") != artifact.sha256 or not refs[0].get("verified")
                or refs[0].get("truncated")
                or (run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} and refs[0].get("log_integrity") != "VERIFIED")):
            return result
        if artifact.id not in io_budget["checked"] and run.workflow_version not in {"stage-agent-v9", "stage-agent-v10"}:
            if artifact.size_bytes > 1024 * 1024 or artifact.size_bytes > io_budget["remaining"]:
                result["reason"] = "INTEGRITY_CHECK_BUDGET_EXCEEDED"
                return result
            io_budget["remaining"] -= artifact.size_bytes
            try:
                store.read(artifact, run_id=run.id, limit=1)
            except (ServiceError, OSError, ValueError):
                return result
            io_budget["checked"].add(artifact.id)
    result.update(reference_status="VALIDATED", reason="HOST_RECORD_AND_INPUT_MATCH")
    return result


def _review_artifact(db, run, store, identity, *, kind, content):
    """Reuse an identical committed artifact; conflicts are never overwritten."""
    data = content.encode("utf-8")
    if not data or len(data) > 1024 * 1024:
        raise ValueError("Review artifact exceeds its limit")
    digest = hashlib.sha256(data).hexdigest()
    rows = [item for item in db.scalars(select(ArtifactVersion).where(
        ArtifactVersion.run_id == run.id, ArtifactVersion.kind == kind))
        if isinstance(item.metadata_json, dict)
        and item.metadata_json.get("review_invocation_id") == identity.invocation_id]
    if rows:
        if (len(rows) != 1 or rows[0].workspace_id != run.workspace_id or rows[0].sha256 != digest
                or rows[0].size_bytes != len(data)
                or rows[0].metadata_json.get("binding_fingerprint") != identity.binding_fingerprint):
            raise ValueError("Existing review artifact binding differs")
        if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"}:
            _BoundedEvidenceStore(store).read_text_page(rows[0], run_id=run.id, limit=1)
        else:
            store.read(rows[0], run_id=run.id, limit=1)
        return {"id": rows[0].id, "sha256": rows[0].sha256}
    item = store.put(db, run_id=run.id, workspace_id=run.workspace_id, kind=kind,
        name="review-model-result.txt" if kind in {"review_model_result_v1", "review_model_result_v2"} else "review-reference-report.json",
        content=content, metadata={"review_invocation_id": identity.invocation_id,
                                   "binding_fingerprint": identity.binding_fingerprint})
    return {"id": item["id"], "sha256": item["sha256"]}


async def persist_review_evidence(host, identity, raw_content, review=None, *, allow_incomplete=False):
    """Persist raw public output and, if valid, a host-checked review report.

    The operation ledger and request manifest must both match this invocation.
    A replay reuses identical artifacts and events, never a model request.
    """
    from langchain_core.messages import AIMessage

    from .m3_agents import AgentBoundaryError
    from .m3_host import AgentOperationNeedsReview
    from .model_output import extract_output, require_complete_output
    from .services import operation_id

    async with host.serial:
        if host.worker.lock is None:
            raise AgentBoundaryError("A live Worker execution lock is required")
        with host.worker.transaction() as db:
            run, role_snapshot = host._run(db, "review", allow_supplements=True)
            if run.workflow_version not in {"stage-agent-v6", "stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10"} or not run.workspace_id:
                raise AgentBoundaryError("Review workflow scope invalid")
            binding = host._binding(identity, role_snapshot, host.plugin_versions, "model_calls", "review", None)
            op_id = operation_id(run.id, host._node("model_calls", "review"),
                                 {"invocation_id": identity.invocation_id})
            operation = db.get(OperationRecord, op_id)
            evidence = operation.result if operation is not None and isinstance(operation.result, dict) else {}
            saved = evidence.get("response") if isinstance(evidence, dict) else None
            if (operation is None or operation.run_id != run.id or operation.status != "completed"
                    or evidence.get("binding_fingerprint") != binding
                    or not isinstance(saved, dict) or not isinstance(saved.get("message"), dict)):
                raise AgentOperationNeedsReview("Saved review result is unavailable")
            try:
                message = AIMessage.model_validate(saved["message"])
                saved_content = (extract_output(message).text if allow_incomplete
                                 else require_complete_output(message)).strip()
            except Exception:
                raise AgentOperationNeedsReview("Saved review result is incomplete") from None
            if not isinstance(raw_content, str) or saved_content != raw_content:
                raise AgentOperationNeedsReview("Saved review result binding differs")
            store = ArtifactStore(host.worker.settings.data_dir / "artifacts")
            try:
                manifest = _bound_review_input(db, run, store, identity)
                raw = _review_artifact(db, run, store, identity, kind="review_model_result_v2" if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else "review_model_result_v1", content=raw_content)
                if review is None:
                    return None
                findings, counts = [], {"validated": 0, "unverified": 0}
                io_budget = {"remaining": 8 * 1024 * 1024, "checked": set()}
                for finding in review.findings:
                    references = [_validate_review_reference(db, run, store, manifest, candidate, io_budget,
                                                             settings=host.worker.settings)
                                  for candidate in finding.candidate_refs]
                    for ref in references:
                        counts["validated" if ref["reference_status"] == "VALIDATED" else "unverified"] += 1
                    findings.append({**finding.model_dump(mode="json", exclude={"candidate_refs"}),
                                     "references": references, "claim_support": "UNVERIFIED"})
                report = {"schema_version": 2 if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else 1, "run_id": run.id,
                          **({"workflow_version": run.workflow_version} if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else {}),
                          "review_invocation_id": identity.invocation_id,
                          "binding_fingerprint": identity.binding_fingerprint,
                          "review_result_artifact_id": raw["id"], "review_result_sha256": raw["sha256"],
                          "summary": review.summary, "findings": findings, "unverified": review.unverified,
                          "claim_support": "UNVERIFIED", "truncated": False}
                serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                saved_report = _review_artifact(db, run, store, identity, kind="review_reference_report_v2" if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else "review_reference_report_v1",
                                                content=serialized)
                payload = {"schema_version": 2 if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else 1,
                    **({"workflow_version": run.workflow_version} if run.workflow_version in {"stage-agent-v9", "stage-agent-v10"} else {}),
                    "review_invocation_id": identity.invocation_id,
                    "binding_fingerprint": identity.binding_fingerprint,
                    "report_artifact_id": saved_report["id"], "report_sha256": saved_report["sha256"],
                    "review_result_artifact_id": raw["id"], "review_result_sha256": raw["sha256"],
                    "finding_count": len(findings), "references_count": counts["validated"] + counts["unverified"],
                    "validated_count": counts["validated"], "unverified_count": counts["unverified"],
                    "claim_support": "UNVERIFIED", "truncated": False, "meaning": "recorded_not_truth"}
                encoded = json.dumps({"mode": run.mode, **payload}, ensure_ascii=True).encode("utf-8")
                if len(encoded) > 16 * 1024:
                    raise ValueError("Review event metadata exceeds the limit")
                events = [item for item in db.scalars(select(RunEvent).where(
                    RunEvent.run_id == run.id, RunEvent.event_type == "review.evidence_recorded"))
                    if isinstance(item.payload, dict)
                    and item.payload.get("review_invocation_id") == identity.invocation_id]
                if events:
                    if len(events) != 1 or events[0].payload != {"mode": run.mode, **payload}:
                        raise ValueError("Existing review event binding differs")
                else:
                    add_event(db, run, "review.evidence_recorded", payload)
                return report
            except (ServiceError, OSError, TypeError, ValueError):
                raise AgentOperationNeedsReview("Review evidence requires verification") from None


def render_review_evidence(report):
    """Render the user-visible review as a model opinion, never certified truth."""
    lines = ["模型审查摘要（结论尚未验证）：" + report["summary"]]
    for finding in report["findings"]:
        refs = finding["references"]
        validated = sum(ref["reference_status"] == "VALIDATED" for ref in refs)
        references = f"引用关系已核对 {validated}/{len(refs)}" if refs else "未提供引用"
        lines.append(f"[{finding['severity']}/模型判断 {finding['model_status']}] {finding['location']}："
                     f"{finding['description']}；建议：{finding['recommendation']}；"
                     f"{references}，结论支持未验证。")
    if report["unverified"]:
        lines.append("未验证：" + "；".join(report["unverified"]))
    return "\n".join(lines)
