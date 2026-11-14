"""Isolated v10 serial tool graph, intentionally not wired to any Worker.

A trusted caller must establish closed-inventory proof and a durable model
reservation/result before using this graph. This module does not grant either.
Its graph input and task configuration must come from those host-owned records,
not a client or a model. Only same-task batches are supported in this subset.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_config
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from .correction_batch import InvalidCorrectionBatch, validate_correction_batch
from .m4_collaboration import relative_file
from .m4_tools import tool_definitions


class _State(TypedDict, total=False):
    missing_actions: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    completed_action_ids: list[str]
    dependency_graph: dict[str, list[str]]
    batch: list[dict[str, Any]]
    messages: list[Any]
    index: int
    records: list[dict[str, Any]]
    stop_reason: str | None


def build_correction_tool_graph(
    *, workspace, execution_boundary, frozen_task_scope, verify_dependencies, confirm_record, checkpointer,
    bind_request=None, failure_handler=None,
):
    """Build a graph using the real ToolNode and existing gateway middleware.

    ``confirm_record(action, tool_message)`` is trusted host code: after the
    gateway returns it must re-read authenticated same-Run/task ledger evidence
    and return {status: RECORDED, operation_id: ..., dependency_ready: bool} or
    a non-recorded status. It is not a model result. The graph does not supply a
    production implementation for this callback. ``frozen_task_scope`` must
    come from the persisted dispatch, never the model. ``verify_dependencies``
    must freshly re-read host evidence immediately before each tool dispatch;
    this graph does not provide a production implementation. Invoke with
    durability="sync". Do not attach to the Worker until closure proof,
    one-shot reservation, and recovery are implemented.
    """
    if checkpointer is None:
        raise ValueError("A durable graph checkpointer is required")
    if (
        not isinstance(frozen_task_scope, dict)
        or set(frozen_task_scope) != {"id", "write_paths"}
        or not isinstance(frozen_task_scope["id"], str)
        or not frozen_task_scope["id"]
    ):
        raise ValueError("A frozen task scope is required")
    paths = frozen_task_scope["write_paths"]
    if paths is not None:
        if not isinstance(paths, list) or any(relative_file(path) != path for path in paths):
            raise ValueError("Invalid frozen task write scope")
    frozen = deepcopy(frozen_task_scope)

    async def wrapped_tool_call(request, handler):
        try:
            state = request.state
            if not isinstance(state, dict):
                raise InvalidCorrectionBatch("Correction tool state is unavailable")
            item = state["batch"][state["index"]]
            cfg = get_config().get("configurable", {})
            if (
                cfg.get("omni_task_scope") != frozen
                or cfg.get("thread_id") != item["action"].get("run_id")
                or item["action"].get("task_id") != frozen["id"]
                or request.tool_call != item["tool_call"]
            ):
                raise InvalidCorrectionBatch("Correction tool binding changed")
            ready = set(state.get("completed_action_ids", [])) | {
                record["action_id"]
                for record in state.get("records", [])
                if record.get("dependency_ready") is True
            }
            if not set(item["dependencies"]).issubset(ready):
                raise InvalidCorrectionBatch("Correction dependencies are not ready")
            if await verify_dependencies(deepcopy(item["action"]), list(item["dependencies"])) is not True:
                raise InvalidCorrectionBatch("Correction dependencies could not be reverified")
            if bind_request is not None:
                await bind_request(request, deepcopy(item))
            return await execution_boundary.awrap_tool_call(request, handler)
        except GraphBubbleUp:
            raise
        except BaseException as exc:
            if failure_handler is not None:
                try:
                    await failure_handler(exc)
                except BaseException:
                    pass
            raise

    node = ToolNode(
        tool_definitions(workspace, interactive=True, patch_schema_v2=True),
        awrap_tool_call=wrapped_tool_call,
        handle_tool_errors=False,
    )

    def prepare(state, index):
        batch = state["batch"]
        if not 0 <= index < len(batch):
            raise InvalidCorrectionBatch("Correction graph position is invalid")
        item = batch[index]
        ready = set(state.get("completed_action_ids", [])) | {
            record["action_id"]
            for record in state.get("records", [])
            if record.get("dependency_ready") is True
        }
        if not set(item["dependencies"]).issubset(ready):
            raise InvalidCorrectionBatch("Correction graph dependency is not ready")
        cfg = get_config().get("configurable", {})
        scope = cfg.get("omni_task_scope")
        if (
            scope != frozen
            or frozen["id"] != item["action"].get("task_id")
            or cfg.get("thread_id") != item["action"].get("run_id")
        ):
            raise InvalidCorrectionBatch("Correction graph task scope changed")
        return {"index": index, "messages": [AIMessage(content="", tool_calls=[item["tool_call"]])]}

    def initialize(state):
        batch = validate_correction_batch(
            missing_actions=state.get("missing_actions"),
            tool_calls=state.get("tool_calls"),
            dependency_graph=state.get("dependency_graph"),
            completed_action_ids=state.get("completed_action_ids", []),
        )
        if len({entry["action"]["task_id"] for entry in batch}) != 1:
            raise InvalidCorrectionBatch("Correction graph requires one frozen task scope")
        return {
            **prepare({"batch": batch, "completed_action_ids": state.get("completed_action_ids", [])}, 0),
            "batch": batch,
            "records": [],
            "stop_reason": None,
        }

    async def confirm(state):
        messages = state.get("messages")
        if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], ToolMessage):
            return {"stop_reason": "tool_result_unavailable"}
        index, message = state["index"], messages[0]
        item = state["batch"][index]
        if message.tool_call_id != item["tool_call"]["id"] or message.status == "error":
            return {"stop_reason": "tool_failed"}
        result = await confirm_record(item["action"], message)
        if (
            not isinstance(result, dict)
            or result.get("status") != "RECORDED"
            or not isinstance(result.get("operation_id"), str)
            or not result["operation_id"]
            or type(result.get("dependency_ready")) is not bool
        ):
            return {"stop_reason": "host_record_unavailable"}
        records = [
            *state.get("records", []),
            {
                "action_id": item["action"]["id"],
                "operation_id": result["operation_id"],
                "dependency_ready": result["dependency_ready"],
            },
        ]
        reason = (
            None if result["dependency_ready"] or index + 1 == len(state["batch"]) else "dependency_not_ready"
        )
        return {"records": records, "stop_reason": reason}

    def after_first(state):
        return "done" if state.get("stop_reason") or len(state["batch"]) == 1 else "next"

    graph = StateGraph(_State)
    graph.add_node("validate", initialize)
    graph.add_node("tool_first", node)
    graph.add_node("confirm_first", confirm)
    graph.add_node("prepare_second", lambda state: prepare(state, 1))
    graph.add_node("tool_second", node)
    graph.add_node("confirm_second", confirm)
    graph.add_edge(START, "validate")
    graph.add_edge("validate", "tool_first")
    graph.add_edge("tool_first", "confirm_first")
    graph.add_conditional_edges("confirm_first", after_first, {"done": END, "next": "prepare_second"})
    graph.add_edge("prepare_second", "tool_second")
    graph.add_edge("tool_second", "confirm_second")
    graph.add_edge("confirm_second", END)
    return graph.compile(checkpointer=checkpointer)
