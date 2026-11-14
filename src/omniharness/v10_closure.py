"""Conservative pre-screen for a future v10 closed execution inventory.

Passing this screen is not an absence proof and never authorizes a call. A
caller must also pin and authenticate the full graph/checkpoint, gateway,
permission, operation and budget watermarks under the current owner fence.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import nullcontext

from langchain_core.messages import AIMessage
from sqlalchemy import select, text

from .checkpoint_fencing import WORKER_LOCK_KEY, FenceTicket, _schema
from .model_output import require_complete_output
from .services import operation_id


class CheckpointPinUnavailable(RuntimeError):
    """A checkpoint cannot be pinned to the active fenced transaction."""


async def screen_zero_tool_correction_scope(db, worker, ticket, graph, expected_root_id):
    """Check the unconsumed owner qualification; never dispatch."""
    return await _screen_zero_tool_correction_scope(db, worker, ticket, graph, expected_root_id)


async def _screen_zero_tool_correction_scope(db, worker, ticket, graph, expected_root_id, *, phase_receipt=None):
    """Check explicit action, live owner and policy bindings; never dispatch.

    Call only after the graph inventory and seal have been checked under this
    same pinned transaction. This deliberately supports one explicit-user task.
    """
    from .action_contract import validate_explicit_actions
    from .m4_contract import M4_TOOL_NAMES
    from .models import Message, Run, Workspace, WorkspaceLease, WorkspacePolicy
    from .v10_owner_auth import verify_run_origin
    from .v10_owner_runtime import V10OwnerListener
    from .workspace_io import DEFAULT_EXCLUDES, WorkspaceError, WorkspaceGrant, _allowed, _denied, _relative

    listener = getattr(worker, "_v10_owner_listener", None)
    if not isinstance(listener, V10OwnerListener) or listener.worker is not worker or not listener.alive:
        return False, "owner_listener_unavailable"
    run = db.scalar(select(Run).where(Run.id == ticket.thread_id).with_for_update())
    if phase_receipt is None:
        origin = listener.accepted.get(ticket.thread_id)
    else:
        try:
            origin = json.loads(phase_receipt.origin_json)
        except (TypeError, ValueError, AttributeError):
            return False, "owner_origin_unavailable"
        if listener._correction_receipts.get(ticket.thread_id) is not phase_receipt:
            return False, "owner_origin_unavailable"
    user = db.scalar(select(Message).where(Message.run_id == ticket.thread_id, Message.role == "user"))
    if (not run or not user or run.status != "running" or origin is None
            or ticket.owner_pid != listener.identity.get("pid")
            or str(ticket.owner_started.isoformat()) != listener.identity.get("backend_started")
            or (run.config_snapshot or {}).get("v10_owner_origin") != origin
            or not verify_run_origin(worker.settings, origin, owner=listener.identity, run=run, message=user)):
        return False, "owner_origin_unavailable"
    snapshot = run.config_snapshot
    if not worker.settings.enable_tools:
        return False, "tool_runtime_disabled"
    workspace = snapshot.get("workspace") or {}
    # Match the existing claim order: fence, Run, workspace, then lease.
    # Populate existing values because earlier checks may have put stale ORM
    # objects into this transaction's identity map before these locks.
    row = db.scalar(select(Workspace).where(Workspace.id == run.workspace_id)
                    .with_for_update().execution_options(populate_existing=True))
    lease = db.scalar(select(WorkspaceLease).where(WorkspaceLease.workspace_id == run.workspace_id)
                      .with_for_update().execution_options(populate_existing=True))
    policy = db.scalar(select(WorkspacePolicy).where(WorkspacePolicy.workspace_id == run.workspace_id,
        WorkspacePolicy.version == workspace.get("policy_version")).with_for_update()
        .execution_options(populate_existing=True))
    if (not row or not row.enabled or row.policy_version != workspace.get("policy_version") or not policy
            or not lease or lease.run_id != run.id
            or policy.grant != workspace.get("grant") or policy.tool_policy != workspace.get("tool_policy")
            or not isinstance(row.probe, dict) or row.probe.get("identity") != workspace.get("probe_identity")):
        return False, "workspace_policy_changed"
    expected_sandbox = {"endpoint": worker.settings.sandbox_endpoint, "image": worker.settings.sandbox_image,
        "instance_id": worker.settings.instance_id, "snapshot_root": worker.settings.sandbox_snapshot_root,
        "max_command_seconds": worker.settings.sandbox_max_command_seconds}
    if workspace.get("sandbox") != expected_sandbox or workspace.get("broker_endpoint") != worker.settings.workspace_broker_url:
        return False, "sandbox_binding_changed"
    contract = snapshot.get("action_contract") or {}
    request = contract.get("request") or {}
    if contract.get("version") != 1 or contract.get("auto_correction_enabled") is not True:
        return False, "action_contract_unavailable"
    validated = validate_explicit_actions(request.get("requirements"), run_id=run.id, workspace_id=run.workspace_id,
                                          require_dependencies=True)
    if (not validated.get("assessed") or validated != contract.get("explicit")
            or not 1 <= len(validated["actions"]) <= 2
            or (len(validated["actions"]) == 2 and validated.get("version") != 2)):
        return False, "explicit_actions_unavailable"
    saved = (run.outcome or {}).get("v10_actions")
    if not isinstance(saved, dict) or saved.get("assessed") is not True:
        return False, "action_outcome_unavailable"
    projected = (run.outcome or {}).get("v10_action_outcome")
    if isinstance(projected, dict):
        entries = projected.get("actions", [])
        if not isinstance(entries, list):
            return False, "action_outcome_unavailable"
        if projected.get("status") == "ACTIONS_RECORDED" or any(
                isinstance(item, dict) and item.get("status") == "ACTIONS_RECORDED" for item in entries):
            return False, "action_already_recorded"
    try:
        state = await asyncio.wait_for(graph.aget_state({"configurable": {"thread_id": run.id}}), timeout=2)
    except asyncio.TimeoutError:
        return False, "graph_read_timed_out"
    if state.config.get("configurable", {}).get("checkpoint_id") != expected_root_id or state.next or state.interrupts:
        return False, "root_state_changed"
    values = state.values if isinstance(state.values, dict) else {}
    tasks = (values.get("dispatch") or {}).get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        return False, "task_scope_unavailable"
    task = tasks[0]
    task_id, step_id, paths = task.get("id"), task.get("step_id"), task.get("write_paths")
    if not isinstance(task_id, str) or not task_id or not isinstance(step_id, str) or not step_id:
        return False, "task_scope_unavailable"
    if paths is not None and (not isinstance(paths, list) or any(not isinstance(path, str) for path in paths)):
        return False, "task_scope_unavailable"
    expected = {**validated, "actions": [{**action, "task_id": task_id} for action in validated["actions"]]}
    if saved != expected or any(action.get("step_id") not in (None, step_id) for action in validated["actions"]):
        return False, "action_task_binding_changed"
    tools = (workspace.get("tool_policy") or {}).get("tools", M4_TOOL_NAMES)
    if not isinstance(tools, (list, tuple, set, frozenset)):
        return False, "tool_policy_unavailable"
    grant = workspace.get("grant")
    if not isinstance(grant, dict):
        return False, "workspace_grant_unavailable"
    try:
        if (grant.get("workspace_id") != run.workspace_id or grant.get("instance_id") != worker.settings.instance_id
                or grant.get("root") != row.host_path or grant.get("version") != row.policy_version
                or grant.get("enabled") is not True
                or not isinstance(grant.get("expected_identity"), dict)
                or grant.get("expected_identity") != workspace.get("probe_identity")
                or not isinstance(grant.get("write_allow"), list) or not isinstance(grant.get("excludes"), list)
                or any(not isinstance(value, str) for value in (*grant["write_allow"], *grant["excludes"]))):
            return False, "workspace_grant_unavailable"
        checked_grant = WorkspaceGrant(**grant)
    except (TypeError, ValueError):
        return False, "workspace_grant_unavailable"
    for action in validated["actions"]:
        name = "workspace_patch" if action["kind"] == "workspace_file_write" else "run_command"
        if name not in tools or (name == "workspace_patch" and paths is not None and action["path"] not in paths):
            return False, "action_not_granted"
        if name == "workspace_patch":
            try:
                path = _relative(action["path"])
                if _denied(path, checked_grant) or not _allowed(path, tuple(checked_grant.write_allow)):
                    return False, "action_not_granted"
            except (WorkspaceError, TypeError, ValueError):
                return False, "action_not_granted"
    # This read-only probe invokes the existing broker's root identity checks.
    # It is a point-in-time observation; the actual gateway must recheck it.
    try:
        from .workspace_client import BrokerClient
        broker = getattr(worker, "workspace_broker", None) or BrokerClient(worker.settings)
        probe = await asyncio.wait_for(broker.request(run.workspace_id, row.policy_version, "probe"), timeout=2)
        if (not isinstance(probe, dict) or probe.get("workspace_id") != run.workspace_id
                or probe.get("policy_version") != row.policy_version or probe.get("root") != row.host_path
                or probe.get("identity") != workspace.get("probe_identity")
                or list(probe.get("read_allow", ())) != list(checked_grant.read_allow)
                or list(probe.get("write_allow", ())) != list(checked_grant.write_allow)
                or list(probe.get("excludes", ())) != [*DEFAULT_EXCLUDES, *checked_grant.excludes]):
            return False, "workspace_probe_changed"
    except Exception:
        return False, "workspace_probe_unavailable"
    if (not listener.alive or (listener.accepted.get(run.id) != origin if phase_receipt is None
            else listener._correction_receipts.get(run.id) is not phase_receipt)):
        return False, "owner_listener_unavailable"
    payload = {"run_id": run.id, "workspace_id": run.workspace_id, "root": expected_root_id,
               "epoch": ticket.epoch, "actions": saved, "task": task,
               "policy_version": row.policy_version, "origin_event": origin["event_id"]}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return True, digest


def model_scope_mac(settings, run, operation_id, scope, response):
    """Authenticate one native execute-model response and its graph identity."""
    if run.workflow_version != "stage-agent-v10":
        raise ValueError("Native response scope applies only to v10")
    from .m4_invalid_audit import _key, _mac
    return _mac(_key(settings, run.workflow_version), "v10-native-execution-response", {
        "run_id": run.id, "workspace_id": run.workspace_id, "operation_id": operation_id,
        "scope": scope, "response": response,
    })


def child_terminal_mac(settings, run, manifest):
    from .m4_invalid_audit import _key, _mac
    return _mac(_key(settings, "stage-agent-v10"), "v10-child-terminal", {
        "run_id": run.id, "workspace_id": run.workspace_id, "manifest": manifest})


async def record_zero_tool_child_terminal(host, executor, checkpointer, task_id):
    """Record a narrow child terminal observation; never a closure proof.

    Only a single authenticated, tool-free native response is recorded. The
    parent checkpoint, global ledger inventory and durable correction claim
    still require independent fenced verification before any dispatch.
    """
    from .models import OperationRecord, RunBudgetReservation, ToolOperation

    async with host.serial:
        with host.worker.transaction() as db:
            run, _ = host._run(db, "execute", allow_supplements=True)
            if db.scalar(select(ToolOperation.id).where(ToolOperation.run_id == run.id).limit(1)):
                return False
            records = [r for r in db.scalars(select(OperationRecord).where(
                OperationRecord.run_id == run.id, OperationRecord.node_id == "agent:execute:model_calls"))
                if isinstance(r.result, dict) and isinstance(r.result.get("v10_model_scope"), dict)
                and r.result["v10_model_scope"].get("task_id") == task_id]
            if len(records) != 1:
                return False
            record = records[0]
            scope = record.result["v10_model_scope"]
            namespace = scope.get("checkpoint_ns", "").rsplit("|", 1)[0]
            if not namespace or scope.get("checkpoint_ns", "") == namespace:
                return False
            reservation = db.get(RunBudgetReservation, {"run_id": run.id, "invocation_id": scope["invocation_id"]})
            if reservation is None or not screen_zero_tool_execution_inventory(
                    run=run, settings=host.worker.settings, task_id=task_id,
                    records=records, reservations=[reservation])[0]:
                return False
            expected_response = record.result["response"]["messages"][0]
            response_mac = record.result["v10_model_scope_mac"]
            op_id = record.operation_id
        # Ask the compiled child graph to interpret its own saved task state.
        try:
            child = await executor.aget_state({"configurable": {"thread_id": host.run_id,
                "checkpoint_ns": namespace, "__pregel_checkpointer": checkpointer}})
            values = child.values if isinstance(child.values, dict) else {}
            messages = values.get("messages")
            checkpoint_id = child.config.get("configurable", {}).get("checkpoint_id")
            if (child.next or child.interrupts or not isinstance(checkpoint_id, str) or not checkpoint_id
                    or not isinstance(messages, list) or not messages
                    or not isinstance(messages[-1], AIMessage)
                    or messages[-1].model_dump(mode="json") != expected_response):
                return False
        except Exception:
            return False
        with host.worker.transaction() as db:
            run, _ = host._run(db, "execute", allow_supplements=True)
            record = db.get(OperationRecord, op_id)
            if (record is None or not isinstance(record.result, dict)
                    or record.result.get("v10_model_scope_mac") != response_mac
                    or db.scalar(select(ToolOperation.id).where(ToolOperation.run_id == run.id).limit(1))):
                return False
            manifest = {"task_id": task_id, "checkpoint_ns": namespace, "checkpoint_id": checkpoint_id,
                        "model_operation_id": op_id, "model_scope_mac": response_mac}
            attestation = {"manifest": manifest, "mac": child_terminal_mac(host.worker.settings, run, manifest)}
            outcome = dict(run.outcome or {})
            observations = dict(outcome.get("v10_child_executions") or {})
            if task_id in observations and observations[task_id] != attestation:
                from .m3_host import AgentOperationNeedsReview
                raise AgentOperationNeedsReview("Child terminal observation differs from saved Run")
            observations[task_id] = attestation
            outcome["v10_child_executions"] = observations
            run.outcome = outcome
        return True


async def _inspect_pinned_zero_tool_inventory(worker, ticket, graph, executor, checkpointer, expected_root_id,
                                              *, owner_db=None, require_seal=False, phase_correction_id=None):
    """Inspect one narrow terminated graph under a pinned owner transaction.

    This returns diagnostic preconditions only. It does not reserve a budget,
    prove all side-effect routes or authorize correction. Caller must supply
    the actual worker-built graphs and bound saver, not client-provided state.
    """
    from .m4_contract import M4_V10_PLUGIN_VERSIONS
    from .m4_services import validate_workspace_lease
    from .models import (
        AgentRun,
        Approval,
        Clarification,
        OperationRecord,
        Run,
        RunBudget,
        RunBudgetReservation,
        RunCommand,
        ToolOperation,
        WorkspaceSnapshot,
    )

    root_config = {"configurable": {"thread_id": ticket.thread_id}}
    with (nullcontext(owner_db) if owner_db is not None else worker.transaction()) as db:
        expected_plugins = M4_V10_PLUGIN_VERSIONS
        if require_seal:
            from .v10_sealed_saver import SEALED_PLUGIN_VERSIONS, V10SealedSaver, verify_pinned_seal_inventory
            if (not isinstance(checkpointer, V10SealedSaver) or checkpointer.ticket != ticket
                    or worker.lock is None or db.connection() is not worker.lock.connection):
                return False, "sealed_owner_scope_unavailable"
            watermarks, _ = verify_pinned_seal_inventory(db, ticket, expected_root_id, worker.settings)
            expected_plugins = SEALED_PLUGIN_VERSIONS
        else:
            watermarks = pin_checkpoint_watermark(db, ticket, expected_root_id)
        run = db.scalar(select(Run).where(Run.id == ticket.thread_id).with_for_update())
        if (run is None or run.workflow_version != "stage-agent-v10" or run.status != "running"
                or not isinstance(run.config_snapshot, dict)):
            return False, "run_scope_unavailable"
        snapshot = run.config_snapshot
        agent, workspace = snapshot.get("agent") or {}, snapshot.get("workspace") or {}
        if (agent.get("plugin_versions") != expected_plugins or workspace.get("id") != run.workspace_id
                or agent.get("strategy") not in {"single", "compact"}
                or not validate_workspace_lease(db, run.workspace_id, run.id)):
            return False, "frozen_configuration_unavailable"
        servers = (workspace.get("mcp") or {}).get("servers", [])
        if not isinstance(servers, list) or any(not isinstance(s, dict) or s.get("enabled") for s in servers):
            return False, "mcp_not_excluded"
        if (db.scalar(select(ToolOperation.id).where(ToolOperation.run_id == run.id).limit(1))
                or db.scalar(select(Approval.id).join(ToolOperation).where(ToolOperation.run_id == run.id).limit(1))
                or db.scalar(select(Clarification.id).where(Clarification.run_id == run.id).limit(1))
                or db.scalar(select(WorkspaceSnapshot.id).where(WorkspaceSnapshot.run_id == run.id).limit(1))
                or db.scalar(select(RunCommand.id).where(RunCommand.run_id == run.id).limit(1))):
            return False, "tool_or_interaction_present"
        observations = (run.outcome or {}).get("v10_child_executions") or {}
        if not isinstance(observations, dict) or len(observations) != 1:
            return False, "child_observation_unavailable"
        task_id, attestation = next(iter(observations.items()))
        manifest = attestation.get("manifest") if isinstance(attestation, dict) else None
        signature = attestation.get("mac") if isinstance(attestation, dict) else None
        if (not isinstance(manifest, dict) or manifest.get("task_id") != task_id
                or not isinstance(signature, str)
                or not hmac.compare_digest(signature, child_terminal_mac(worker.settings, run, manifest))):
            return False, "child_observation_invalid"
        child_ns, child_id = manifest.get("checkpoint_ns"), manifest.get("checkpoint_id")
        if (not isinstance(child_ns, str) or not child_ns or set(watermarks) != {"", child_ns}
                or watermarks.get(child_ns) != child_id):
            return False, "checkpoint_inventory_mismatch"
        if not _pinned_checkpoint_references_present(db, ticket, watermarks):
            return False, "checkpoint_storage_incomplete"
        reservations = list(db.scalars(select(RunBudgetReservation).where(RunBudgetReservation.run_id == run.id)))
        records = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run.id)))
        if require_seal:
            records = [record for record in records if record.node_id != "v10-checkpoint-seal-v1"]
        budget = db.get(RunBudget, run.id)
        if budget is None or budget.tool_calls != 0 or budget.model_calls != len(reservations):
            return False, "budget_or_ledger_inventory_mismatch"
        if phase_correction_id is not None:
            from .correction_reservation import correction_invocation_id
            invocation = correction_invocation_id(run.id)
            correction_records = [record for record in records if record.operation_id == phase_correction_id]
            correction_reservations = [item for item in reservations if item.invocation_id == invocation]
            if len(correction_records) != 1 or len(correction_reservations) != 1:
                return False, "correction_ledger_unavailable"
            records = [record for record in records if record.operation_id != phase_correction_id]
            reservations = [item for item in reservations if item.invocation_id != invocation]
        if (len(records) != len(reservations) or not records
                or any(r.kind != "model_calls" for r in reservations)):
            return False, "budget_or_ledger_inventory_mismatch"
        expected = {}
        for reservation in reservations:
            for role in ("think", "plan", "dispatch", "execute", "review"):
                expected[operation_id(run.id, f"agent:{role}:model_calls", {
                    "invocation_id": reservation.invocation_id})] = reservation
        if any(r.operation_id not in expected or r.workflow_version != run.workflow_version
               or r.status != "completed" or not isinstance(r.result, dict)
               or r.result.get("purpose") != "task"
               or r.result.get("binding_fingerprint") != expected[r.operation_id].binding_fingerprint
               for r in records):
            return False, "model_ledger_unavailable"
        if len({expected[r.operation_id].invocation_id for r in records}) != len(reservations):
            return False, "model_reservation_not_one_to_one"
        agents = list(db.scalars(select(AgentRun).where(AgentRun.run_id == run.id)))
        if len(agents) != len(reservations) or any(item.status != "completed" for item in agents):
            return False, "agent_inventory_unavailable"
        execution_records = [r for r in records if r.node_id == "agent:execute:model_calls"]
        if len(execution_records) != 1 or execution_records[0].operation_id != manifest.get("model_operation_id"):
            return False, "execution_inventory_mismatch"
        record = execution_records[0]
        reservation = expected[record.operation_id]
        if (record.result.get("v10_model_scope_mac") != manifest.get("model_scope_mac")
                or not screen_zero_tool_execution_inventory(run=run, settings=worker.settings, task_id=task_id,
                    records=[record], reservations=[reservation])[0]):
            return False, "native_inventory_invalid"
        # The fence remains pinned while both compiled graphs read their
        # durable state on the saver connection. No graph writes are issued.
        try:
            root = await asyncio.wait_for(graph.aget_state(root_config), timeout=2)
            child = await asyncio.wait_for(executor.aget_state({"configurable": {"thread_id": run.id,
                "checkpoint_ns": child_ns, "__pregel_checkpointer": checkpointer}}), timeout=2)
        except asyncio.TimeoutError:
            return False, "graph_read_timed_out"
        if (root.next or root.interrupts or child.next or child.interrupts
                or root.config.get("configurable", {}).get("checkpoint_id") != expected_root_id
                or child.config.get("configurable", {}).get("checkpoint_id") != child_id):
            return False, "graph_state_not_terminal"
        root_values = root.values if isinstance(root.values, dict) else {}
        task_results = root_values.get("task_results")
        if (not isinstance(task_results, list) or len(task_results) != 1
                or not isinstance(task_results[0], dict) or task_results[0].get("task_id") != task_id
                or task_results[0].get("status", "completed") != "completed"):
            return False, "parent_task_inventory_mismatch"
        child_messages = child.values.get("messages") if isinstance(child.values, dict) else None
        expected_message = record.result["response"]["messages"][0]
        if (not isinstance(child_messages, list) or not child_messages
                or not isinstance(child_messages[-1], AIMessage)
                or child_messages[-1].model_dump(mode="json") != expected_message):
            return False, "child_response_mismatch"
        return True, "pinned_preconditions_only_not_closed"


async def inspect_pinned_zero_tool_inventory(worker, ticket, graph, executor, checkpointer, expected_root_id,
                                             *, owner_db=None, require_seal=False):
    """Public diagnostic for the pre-reservation inventory."""
    return await _inspect_pinned_zero_tool_inventory(worker, ticket, graph, executor, checkpointer, expected_root_id,
                                                      owner_db=owner_db, require_seal=require_seal)


async def inspect_consumed_correction_phase(db, worker, *, ticket, root_id, graph, executor, checkpointer):
    """Recheck a consumed host receipt and its original closure in one owner transaction.

    This does not create a correction phase or authorize a tool call. The
    caller must independently authenticate the recorded model response and
    check listener liveness before and after the transaction.
    """
    from .correction_phase import require_consumed_phase_identity
    from .correction_reservation import _require_closed_inventory

    try:
        run, receipt = require_consumed_phase_identity(db, worker, ticket=ticket, root_id=root_id)
        passed, reason = await _inspect_pinned_zero_tool_inventory(
            worker, ticket, graph, executor, checkpointer, root_id, owner_db=db, require_seal=True,
            phase_correction_id=receipt.operation_id)
        if not passed:
            return False, reason
        scoped, digest = await _screen_zero_tool_correction_scope(
            db, worker, ticket, graph, root_id, phase_receipt=receipt)
        if not scoped:
            return False, digest
        closure = _require_closed_inventory(db, run, worker, digest, ticket=ticket, root_id=root_id,
                                             graph=graph, executor=executor, checkpointer=checkpointer)
        if not hmac.compare_digest(closure, receipt.closure_fingerprint):
            return False, "consumed_closure_changed"
        return True, digest
    except Exception:
        return False, "consumed_phase_unavailable"


def pin_checkpoint_watermark(db, ticket, expected_checkpoint_id):
    """Pin a checkpoint fence until *db's caller-owned transaction* ends.

    This is only a building block: checkpoint IDs are not a response/ledger
    closure proof, and the returned value gives no permission to dispatch.
    Caller must hold the worker owner's DB session, drain the graph/scheduler,
    validate terminal state and compare all relevant evidence while the same
    transaction remains open. No claim survives the transaction by itself.
    """
    if (not isinstance(ticket, FenceTicket)
            or ticket.workflow_version not in {"stage-agent-v10", "stage-agent-v10-sealed-v2"}
            or not isinstance(expected_checkpoint_id, str) or not expected_checkpoint_id
            or db.get_bind().dialect.name != "postgresql" or not db.in_transaction()):
        raise CheckpointPinUnavailable("A v10 owner transaction and checkpoint are required")
    schema = _schema(ticket.schema)
    # Every saver write takes FOR SHARE on this row. After FOR UPDATE returns,
    # earlier checkpoint writes are complete and subsequent writes wait until
    # the caller commits/rolls back. This function never commits the caller's
    # transaction or accepts a pooled session in place of the lock owner.
    row = db.execute(text(f"""
        SELECT f.epoch, f.workflow_version, f.owner_pid, f.owner_started, f.lock_key,
               pg_backend_pid() AS current_pid,
               EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity a
                   JOIN pg_catalog.pg_locks l ON l.pid=a.pid
                   WHERE a.pid=f.owner_pid AND a.backend_start=f.owner_started
                     AND l.locktype='advisory' AND l.mode='ExclusiveLock' AND l.granted
                     AND l.objsubid=1 AND l.classid::bigint=(f.lock_key >> 32)
                     AND l.objid::bigint=(f.lock_key & 4294967295)
                     AND l.database=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())) AS owner_live
        FROM {schema}.fences f WHERE f.thread_id=:thread FOR UPDATE
    """), {"thread": ticket.thread_id}).mappings().one_or_none()
    if (row is None or row["epoch"] != ticket.epoch or row["workflow_version"] != ticket.workflow_version
            or row["owner_pid"] != ticket.owner_pid or row["current_pid"] != ticket.owner_pid
            or row["owner_started"] != ticket.owner_started or row["lock_key"] != WORKER_LOCK_KEY
            or row["owner_live"] is not True):
        raise CheckpointPinUnavailable("Checkpoint owner fence changed or is not held")
    rows = db.execute(text(f"""
        SELECT DISTINCT ON (checkpoint_ns) checkpoint_ns, checkpoint_id
        FROM {schema}.checkpoints WHERE thread_id=:thread
        ORDER BY checkpoint_ns, checkpoint_id DESC
    """), {"thread": ticket.thread_id}).mappings().all()
    watermarks = {item["checkpoint_ns"]: item["checkpoint_id"] for item in rows}
    if watermarks.get("") != expected_checkpoint_id:
        raise CheckpointPinUnavailable("Root checkpoint changed before pinning")
    return watermarks


def _pinned_checkpoint_references_present(db, ticket, watermarks):
    """Check selected durable channels and existing write references under the pin.

    This cannot detect entirely omitted writes or prove a complete parent chain.
    """
    schema = _schema(ticket.schema)
    for namespace, checkpoint_id in watermarks.items():
        params = {"thread": ticket.thread_id, "namespace": namespace, "checkpoint": checkpoint_id}
        checkpoint = db.execute(text(f"""
            SELECT jsonb_typeof(checkpoint->'channel_versions') AS version_type
            FROM {schema}.checkpoints
            WHERE thread_id=:thread AND checkpoint_ns=:namespace AND checkpoint_id=:checkpoint
        """), params).mappings().one_or_none()
        if checkpoint is None or checkpoint["version_type"] != "object":
            return False
        missing = db.execute(text(f"""
            SELECT count(*) FROM {schema}.checkpoints c
            CROSS JOIN LATERAL jsonb_each_text(c.checkpoint->'channel_versions') v
            LEFT JOIN {schema}.checkpoint_blobs b ON b.thread_id=c.thread_id
                AND b.checkpoint_ns=c.checkpoint_ns AND b.channel=v.key AND b.version=v.value
            WHERE c.thread_id=:thread AND c.checkpoint_ns=:namespace AND c.checkpoint_id=:checkpoint
              AND v.key IN ('messages', 'task_results', 'task', 'execution', 'stage_outputs', 'plan', 'dispatch')
              AND NOT jsonb_exists(COALESCE(c.checkpoint->'channel_values', '{{}}'::jsonb), v.key)
              AND (b.channel IS NULL OR b.type IS NULL OR (b.type <> 'empty' AND b.blob IS NULL))
        """), params).scalar_one()
        if missing:
            return False
    orphaned = db.execute(text(f"""
        SELECT count(*) FROM {schema}.checkpoint_writes w
        LEFT JOIN {schema}.checkpoints c ON c.thread_id=w.thread_id
            AND c.checkpoint_ns=w.checkpoint_ns AND c.checkpoint_id=w.checkpoint_id
        WHERE w.thread_id=:thread AND (c.checkpoint_id IS NULL OR w.type IS NULL
            OR (w.type <> 'empty' AND w.blob IS NULL))
    """), {"thread": ticket.thread_id}).scalar_one()
    return not orphaned


def screen_zero_tool_execution_inventory(*, run, settings, task_id, records, reservations):
    """Check a supplied, complete inventory without inferring its completeness.

    Only task-purpose native execute-model records with a unique matching budget
    reservation and an explicitly completed, tool-free response can pass.
    The caller must obtain both lists at one fenced transaction watermark.
    """
    run_id, workflow_version = run.id, run.workflow_version
    if workflow_version != "stage-agent-v10" or not records or not isinstance(task_id, str) or not task_id:
        return False, "execution_inventory_unavailable"
    by_invocation = {}
    for reservation in reservations:
        if (reservation.run_id != run_id or reservation.kind != "model_calls"
                or reservation.invocation_id in by_invocation):
            return False, "reservation_scope_or_identity_invalid"
        by_invocation[reservation.invocation_id] = reservation
    expected = {operation_id(run_id, "agent:execute:model_calls", {"invocation_id": key}): value
                for key, value in by_invocation.items()}
    if len(expected) != len(records) or {row.operation_id for row in records} != set(expected):
        return False, "model_ledger_reservation_mismatch"
    for row in records:
        reservation = expected[row.operation_id]
        result = row.result
        if (row.run_id != run_id or row.workflow_version != workflow_version
                or row.node_id != "agent:execute:model_calls" or row.status != "completed"
                or not isinstance(result, dict) or result.get("purpose") != "task"
                or result.get("binding_fingerprint") != reservation.binding_fingerprint):
            return False, "model_record_invalid"
        response = result.get("response")
        scope = result.get("v10_model_scope")
        signature = result.get("v10_model_scope_mac")
        if (not isinstance(scope, dict) or scope.get("run_id") != run_id or scope.get("task_id") != task_id
                or scope.get("invocation_id") != reservation.invocation_id
                or not isinstance(scope.get("checkpoint_ns"), str) or not scope["checkpoint_ns"]
                or not isinstance(scope.get("graph_task_id"), str) or not scope["graph_task_id"]
                or not isinstance(signature, str)):
            return False, "model_scope_binding_missing"
        from .m3_agents import _hash
        if _hash({"namespace": scope["checkpoint_ns"], "task_id": scope["graph_task_id"], "model": True}) != reservation.invocation_id:
            return False, "model_scope_identity_mismatch"
        try:
            if not hmac.compare_digest(signature, model_scope_mac(settings, run, row.operation_id, scope, response)):
                return False, "model_scope_authentication_failed"
        except Exception:
            return False, "model_scope_authentication_unavailable"
        messages = response.get("messages") if isinstance(response, dict) else None
        if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict):
            return False, "native_response_unavailable"
        try:
            message = AIMessage.model_validate(messages[0])
            # Only plain text is admitted to this narrow screen. Native
            # provider content blocks such as tool_use/server_tool_use, or
            # unknown extension fields, must never be interpreted as no call.
            content = message.content
            plain = (isinstance(content, str) or (isinstance(content, list) and all(
                isinstance(block, dict) and set(block) == {"type", "text"}
                and block.get("type") in {"text", "output_text"}
                and isinstance(block.get("text"), str) for block in content)))
            metadata = message.response_metadata or {}
            if (not plain or message.tool_calls or message.invalid_tool_calls or message.additional_kwargs
                    or set(metadata) - {"finish_reason", "stop_reason", "status", "omni_require_finish"}):
                return False, "native_response_has_or_may_have_tool_calls"
            require_complete_output(message, require_finish=True)
        except Exception:
            return False, "native_response_incomplete_or_invalid"
    return True, "inventory_screened_not_closed"
