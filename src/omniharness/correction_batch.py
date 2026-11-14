"""Bounded v10 correction batch validation and serial host-gateway dispatch.

This module cannot establish closure or authorize a call. The caller must first
hold the correct Run fence, prove no uncertain side effect, reserve the unique
correction and model budget durably, and provide the existing fenced gateway
with the proper task scope. A previously reserved unknown call must never be
passed here again. The injected adapter must not call the model and must only
return RECORDED after the gateway's ledger commits and dependency readiness is
verified. Approval interrupts propagate to the host; this component does not
resume or replay them.
"""

from __future__ import annotations

import asyncio
import re
from copy import deepcopy

from langgraph.errors import GraphBubbleUp
from pydantic import ValidationError

from .action_contract import ActionRequirement
from .m4_tools import CommandArguments, PatchArgumentsV8

_HEX = re.compile(r"^[0-9a-f]{64}$")


class InvalidCorrectionBatch(ValueError):
    """No call from this model response may be dispatched."""


def validate_correction_batch(*, missing_actions, tool_calls, dependency_graph=None, completed_action_ids=()):
    """Return a detached, ordered mapping of native calls to missing actions.

    ``missing_actions`` is the trusted host's source-validated, task-bound set;
    model-provided action IDs are never accepted. Every native call must match
    exactly one action by tool kind, exact path or exact argv and cwd. A
    response must cover every missing action exactly once. The
    original native call shape is retained for a gateway adapter, without
    adding any fields to its strict tool argument schema. The trusted host must
    supply a complete dependency mapping, including explicit empty lists.
    Unknown or unsatisfied dependencies invalidate the entire batch.
    """
    if (
        not isinstance(missing_actions, list)
        or not 1 <= len(missing_actions) <= 2
        or not isinstance(tool_calls, list)
        or len(tool_calls) != len(missing_actions)
        or not isinstance(completed_action_ids, (list, tuple, set, frozenset))
    ):
        raise InvalidCorrectionBatch("Invalid batch size")
    if any(
        not isinstance(identity, str) or not _HEX.fullmatch(identity) for identity in completed_action_ids
    ):
        raise InvalidCorrectionBatch("Invalid completed action identity")
    completed = set(completed_action_ids)
    ids, scopes, actions, keys = set(), set(), [], set()
    for action in missing_actions:
        if not isinstance(action, dict):
            raise InvalidCorrectionBatch("Invalid action")
        identity, run, workspace, task = (action.get(k) for k in ("id", "run_id", "workspace_id", "task_id"))
        if (
            not isinstance(identity, str)
            or not _HEX.fullmatch(identity)
            or identity in ids
            or identity in completed
            or not all(isinstance(x, str) and x for x in (run, workspace, task))
        ):
            raise InvalidCorrectionBatch("Action identity or scope is invalid")
        try:
            value = ActionRequirement.model_validate(
                {k: action[k] for k in ("kind", "path", "argv", "cwd", "step_id") if k in action}
            )
        except (ValidationError, ValueError, TypeError):
            raise InvalidCorrectionBatch("Invalid action parameters") from None
        key = (
            ("workspace_patch", value.path)
            if value.kind == "workspace_file_write"
            else ("run_command", tuple(value.argv), value.cwd)
        )
        if key in keys:
            raise InvalidCorrectionBatch("Ambiguous action parameters")
        ids.add(identity)
        scopes.add((run, workspace))
        keys.add(key)
        actions.append((key, deepcopy(action)))
    if len(scopes) != 1:
        raise InvalidCorrectionBatch("Actions cross Run or workspace")
    if not isinstance(dependency_graph, dict) or set(dependency_graph) != ids:
        raise InvalidCorrectionBatch("Complete host dependency graph is required")
    dependencies = {}
    for identity, deps in dependency_graph.items():
        if (
            not isinstance(deps, list)
            or any(not isinstance(dep, str) or not _HEX.fullmatch(dep) for dep in deps)
            or len(set(deps)) != len(deps)
            or identity in deps
            or not set(deps).issubset(ids | completed)
        ):
            raise InvalidCorrectionBatch("Invalid or unknown dependency")
        dependencies[identity] = set(deps)
    matched, call_ids, output = set(), set(), []
    for call in tool_calls:
        if (
            not isinstance(call, dict)
            or set(call) - {"id", "name", "args", "type"}
            or call.get("type", "tool_call") != "tool_call"
            or not isinstance(call.get("id"), str)
            or not call["id"]
            or len(call["id"]) > 200
            or call["id"] in call_ids
            or not isinstance(call.get("args"), dict)
        ):
            raise InvalidCorrectionBatch("Invalid native tool call")
        call_ids.add(call["id"])
        try:
            if call.get("name") == "workspace_patch":
                parameters = PatchArgumentsV8.model_validate(call["args"], strict=True)
                key = ("workspace_patch", parameters.path)
            elif call.get("name") == "run_command":
                parameters = CommandArguments.model_validate(call["args"], strict=True)
                if not isinstance(parameters.command, list):
                    raise InvalidCorrectionBatch("Command must be exact argv")
                key = ("run_command", tuple(parameters.command), parameters.cwd)
            else:
                raise InvalidCorrectionBatch("Tool is outside correction scope")
        except (ValidationError, ValueError, TypeError):
            raise InvalidCorrectionBatch("Invalid tool arguments") from None
        candidates = [action for expected, action in actions if key == expected]
        if len(candidates) != 1 or candidates[0]["id"] in matched:
            raise InvalidCorrectionBatch("Call does not uniquely match a missing action")
        identity = candidates[0]["id"]
        if not dependencies[identity].issubset(completed | matched):
            raise InvalidCorrectionBatch("Tool order does not satisfy dependencies")
        matched.add(identity)
        output.append(
            {
                "action": candidates[0],
                "tool_call": deepcopy(call),
                "dependencies": sorted(dependencies[identity]),
            }
        )
    return output


async def execute_correction_batch(
    *, missing_actions, tool_calls, invoke, dependency_graph=None, completed_action_ids=()
):
    """Validate the whole batch, then invoke the trusted gateway adapter serially.

    The adapter receives detached ``(action, native_tool_call)`` values and
    returns ``status`` in RECORDED/FAILED/UNKNOWN/WAITING_APPROVAL. RECORDED
    additionally requires a nonempty same-Run host operation_id and a boolean
    dependency_ready. The host, not this function, establishes those facts. No
    adapter call is made for any invalid batch. Any error, unknown outcome,
    refusal or unready dependency stops further calls; prior records remain.
    The outer host owns durable recovery and must never call this again for an
    already-reserved unknown correction. Exceptions after dispatch are unknown.
    """
    batch = validate_correction_batch(
        missing_actions=missing_actions,
        tool_calls=tool_calls,
        dependency_graph=dependency_graph,
        completed_action_ids=completed_action_ids,
    )
    records, ready = [], set(completed_action_ids)
    for index, item in enumerate(batch):
        if not set(item["dependencies"]).issubset(ready):
            return {"status": "UNVERIFIED", "reason": "dependency_not_ready", "records": records}
        try:
            result = await invoke(deepcopy(item["action"]), deepcopy(item["tool_call"]))
        except (GraphBubbleUp, asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # LangGraph interrupts inherit Exception and must reach its saver.
            raise
        except Exception:
            return {
                "status": "UNKNOWN",
                "reason": "gateway_exception",
                "records": records,
                "unknown_action_id": item["action"]["id"],
            }
        status = result.get("status") if isinstance(result, dict) else None
        if status not in {"RECORDED", "FAILED", "UNKNOWN", "WAITING_APPROVAL"}:
            status = "UNKNOWN"
        op_id = result.get("operation_id") if isinstance(result, dict) else None
        if status == "RECORDED" and (
            not isinstance(op_id, str) or not op_id or type(result.get("dependency_ready")) is not bool
        ):
            status = "UNKNOWN"
        record = {"action_id": item["action"]["id"], "status": status}
        if isinstance(op_id, str) and op_id:
            record["operation_id"] = op_id
        records.append(record)
        if status != "RECORDED":
            return {"status": status, "reason": "gateway_stopped", "records": records}
        if result["dependency_ready"]:
            ready.add(item["action"]["id"])
        if index + 1 < len(batch) and result["dependency_ready"] is not True:
            return {"status": "UNVERIFIED", "reason": "dependency_not_ready", "records": records}
    return {"status": "RECORDED", "reason": "batch_recorded", "records": records}
