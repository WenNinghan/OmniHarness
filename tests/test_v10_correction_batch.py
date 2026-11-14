import asyncio

import pytest
from langgraph.errors import GraphBubbleUp, GraphInterrupt

from omniharness.correction_batch import (
    InvalidCorrectionBatch,
    execute_correction_batch,
    validate_correction_batch,
)


def inputs():
    common = {"run_id": "run", "workspace_id": "workspace", "task_id": "t1"}
    actions = [
        {**common, "id": "a" * 64, "kind": "workspace_file_write", "path": "src/a.py"},
        {**common, "id": "b" * 64, "kind": "sandbox_command", "argv": ["pytest", "-q"], "cwd": "."},
    ]
    calls = [
        {
            "id": "call1",
            "name": "workspace_patch",
            "args": {"path": "src/a.py", "content": "x", "expected_sha256": None},
            "type": "tool_call",
        },
        {
            "id": "call2",
            "name": "run_command",
            "args": {"command": ["pytest", "-q"], "cwd": "."},
            "type": "tool_call",
        },
    ]
    return actions, calls


def dependencies(actions):
    return {actions[0]["id"]: [], actions[1]["id"]: [actions[0]["id"]]}


@pytest.mark.asyncio
async def test_batch_is_whole_and_serial_and_preserves_patch():
    actions, calls = inputs()
    received = []

    async def gateway(action, call):
        received.append((action["id"], call))
        return {"status": "RECORDED", "operation_id": str(len(received)) * 64, "dependency_ready": True}

    result = await execute_correction_batch(
        missing_actions=actions, tool_calls=calls, dependency_graph=dependencies(actions), invoke=gateway
    )
    assert result["status"] == "RECORDED"
    assert [x[0] for x in received] == ["a" * 64, "b" * 64]
    assert received[0][1] == calls[0]
    assert [x["action_id"] for x in result["records"]] == ["a" * 64, "b" * 64]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "extra",
        "duplicate",
        "unmatched",
        "wrong_task_scope",
        "completed",
        "unknown_arg",
        "shell_string",
    ],
)
async def test_any_invalid_call_rejects_entire_batch(case):
    actions, calls = inputs()
    completed = []
    if case == "missing":
        calls.pop()
    if case == "extra":
        calls.append({**calls[1], "id": "third"})
    if case == "duplicate":
        calls[1] = {**calls[0], "id": "other"}
    if case == "unmatched":
        calls[1]["args"]["cwd"] = "other"
    if case == "wrong_task_scope":
        actions[1]["workspace_id"] = "other"
    if case == "completed":
        completed = [actions[0]["id"]]
    if case == "unknown_arg":
        calls[0]["args"]["arbitrary"] = True
    if case == "shell_string":
        calls[1]["args"]["command"] = "pytest -q"
    invoked = []

    async def gateway(*args):
        invoked.append(args)

    with pytest.raises(InvalidCorrectionBatch):
        await execute_correction_batch(
            missing_actions=actions,
            tool_calls=calls,
            dependency_graph=dependencies(actions),
            invoke=gateway,
            completed_action_ids=completed,
        )
    assert invoked == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["FAILED", "UNKNOWN", "WAITING_APPROVAL", "EXCEPTION", "NOT_READY"])
async def test_unconfirmed_first_action_stops_and_never_retries(first):
    actions, calls = inputs()
    invoked = []

    async def gateway(action, call):
        invoked.append(action["id"])
        if first == "EXCEPTION":
            raise RuntimeError("unknown")
        return {
            "status": "RECORDED" if first == "NOT_READY" else first,
            "operation_id": "f" * 64,
            "dependency_ready": first != "NOT_READY",
        }

    result = await execute_correction_batch(
        missing_actions=actions, tool_calls=calls, dependency_graph=dependencies(actions), invoke=gateway
    )
    assert invoked == [actions[0]["id"]]
    assert result["status"] == (
        "UNVERIFIED" if first == "NOT_READY" else "UNKNOWN" if first == "EXCEPTION" else first
    )


def test_host_scope_and_parameters_must_be_unambiguous():
    actions, calls = inputs()
    actions[1]["argv"] = ["pytest", "../x"]
    calls[1]["args"]["command"] = ["pytest", "../x"]
    assert (
        len(
            validate_correction_batch(
                missing_actions=actions, tool_calls=calls, dependency_graph=dependencies(actions)
            )
        )
        == 2
    )
    actions[0]["path"] = "../a.py"
    with pytest.raises(InvalidCorrectionBatch):
        validate_correction_batch(
            missing_actions=actions, tool_calls=calls, dependency_graph=dependencies(actions)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [GraphInterrupt, GraphBubbleUp, asyncio.CancelledError])
async def test_graph_interrupt_and_cancellation_propagate(control):
    actions, calls = inputs()
    invoked = []

    async def gateway(action, call):
        invoked.append(action["id"])
        raise control()

    with pytest.raises(control):
        await execute_correction_batch(
            missing_actions=actions, tool_calls=calls, dependency_graph=dependencies(actions), invoke=gateway
        )
    assert invoked == [actions[0]["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["reversed", "missing", "unknown", "cycle"])
async def test_unproven_or_invalid_dependencies_reject_whole_batch(case):
    actions, calls = inputs()
    graph = dependencies(actions)
    if case == "reversed":
        calls.reverse()
    elif case == "missing":
        graph = None
    elif case == "unknown":
        graph[actions[1]["id"]] = ["f" * 64]
    elif case == "cycle":
        graph[actions[0]["id"]] = [actions[1]["id"]]
    invoked = []

    async def gateway(action, call):
        invoked.append(call)

    with pytest.raises(InvalidCorrectionBatch):
        await execute_correction_batch(
            missing_actions=actions, tool_calls=calls, dependency_graph=graph, invoke=gateway
        )
    assert invoked == []
