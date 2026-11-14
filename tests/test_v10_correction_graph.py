from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt
from test_v10_correction_batch import dependencies, inputs

from omniharness.correction_graph import build_correction_tool_graph
from omniharness.m3_agents import invocation_identity
from omniharness.m4_runtime import ExecutionBoundary


def make_graph(gateway, confirm, verify=None):
    host = SimpleNamespace(workflow_version="stage-agent-v10")
    boundary = ExecutionBoundary(host=host, gateway=gateway, scheduler=None)

    async def verify_dependencies(action, dependencies):
        return True if verify is None else await verify(action, dependencies)

    return build_correction_tool_graph(
        workspace={},
        execution_boundary=boundary,
        frozen_task_scope={"id": "t1", "write_paths": ["src/a.py"]},
        verify_dependencies=verify_dependencies,
        confirm_record=confirm,
        checkpointer=InMemorySaver(),
    )


def config():
    return {
        "configurable": {"thread_id": "run", "omni_task_scope": {"id": "t1", "write_paths": ["src/a.py"]}}
    }


@pytest.mark.asyncio
async def test_real_toolnode_runs_sequentially_with_real_runtime():
    actions, calls = inputs()
    observed = []

    class Gateway:
        async def invoke(self, request):
            identity = invocation_identity(
                request, kind="tool_calls", role="execute", tool_name=request.tool_call["name"]
            )
            observed.append((request.tool_call["id"], identity.thread_id, identity.task_id))
            return ToolMessage(
                content="{}", tool_call_id=request.tool_call["id"], name=request.tool_call["name"]
            )

    async def confirm(action, message):
        assert observed[-1][0] == message.tool_call_id
        return {"status": "RECORDED", "operation_id": action["id"], "dependency_ready": True}

    result = await make_graph(Gateway(), confirm).ainvoke(
        {"missing_actions": actions, "tool_calls": calls, "dependency_graph": dependencies(actions)},
        config(),
        durability="sync",
    )
    assert [x[0] for x in observed] == ["call1", "call2"]
    assert all(run == "run" and task for _, run, task in observed)
    assert len(result["records"]) == 2 and result["stop_reason"] is None


@pytest.mark.asyncio
async def test_approval_interrupt_resumes_current_node_without_first_reentry():
    actions, calls = inputs()
    observed, identities = [], []

    class Gateway:
        async def invoke(self, request):
            name = request.tool_call["id"]
            identity = invocation_identity(
                request, kind="tool_calls", role="execute", tool_name=request.tool_call["name"]
            )
            observed.append(name)
            if name == "call2":
                identities.append(identity.invocation_id)
                assert interrupt({"type": "approval", "call_id": name}) == "approved"
            return ToolMessage(content="{}", tool_call_id=name, name=request.tool_call["name"])

    async def confirm(action, message):
        return {"status": "RECORDED", "operation_id": action["id"], "dependency_ready": True}

    graph = make_graph(Gateway(), confirm)
    first = await graph.ainvoke(
        {"missing_actions": actions, "tool_calls": calls, "dependency_graph": dependencies(actions)},
        config(),
        durability="sync",
    )
    assert first["__interrupt__"] and observed == ["call1", "call2"]
    final = await graph.ainvoke(Command(resume="approved"), config(), durability="sync")
    assert observed == ["call1", "call2", "call2"]
    assert identities[0] == identities[1]
    assert len(final["records"]) == 2


@pytest.mark.asyncio
async def test_unconfirmed_record_prevents_second_dispatch():
    actions, calls = inputs()
    observed = []

    class Gateway:
        async def invoke(self, request):
            observed.append(request.tool_call["id"])
            return ToolMessage(content="{}", tool_call_id=request.tool_call["id"])

    async def confirm(action, message):
        return {"status": "UNKNOWN"}

    result = await make_graph(Gateway(), confirm).ainvoke(
        {"missing_actions": actions, "tool_calls": calls, "dependency_graph": dependencies(actions)},
        config(),
        durability="sync",
    )
    assert observed == ["call1"] and result["stop_reason"] == "host_record_unavailable"


@pytest.mark.asyncio
async def test_graph_rejects_wrong_order_before_any_tool():
    actions, calls = inputs()
    calls.reverse()
    observed = []

    class Gateway:
        async def invoke(self, request):
            observed.append(request)

    async def confirm(action, message):
        raise AssertionError("Must not execute")

    from omniharness.correction_batch import InvalidCorrectionBatch

    with pytest.raises(InvalidCorrectionBatch):
        await make_graph(Gateway(), confirm).ainvoke(
            {"missing_actions": actions, "tool_calls": calls, "dependency_graph": dependencies(actions)},
            config(),
            durability="sync",
        )
    assert observed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("id", "other"), ("write_paths", ["other.py"])])
async def test_tampered_frozen_task_scope_rejects_before_dispatch(field, value):
    actions, calls = inputs()
    observed = []

    class Gateway:
        async def invoke(self, request):
            observed.append(request)

    async def confirm(action, message):
        raise AssertionError("Unexpected confirmation")

    cfg = config()
    cfg["configurable"]["omni_task_scope"][field] = value
    from omniharness.correction_batch import InvalidCorrectionBatch

    with pytest.raises(InvalidCorrectionBatch):
        await make_graph(Gateway(), confirm).ainvoke(
            {"missing_actions": actions, "tool_calls": calls, "dependency_graph": dependencies(actions)},
            cfg,
            durability="sync",
        )
    assert observed == []


@pytest.mark.asyncio
async def test_dependency_is_reverified_immediately_before_second_dispatch():
    actions, calls = inputs()
    observed, checked = [], []

    class Gateway:
        async def invoke(self, request):
            observed.append(request.tool_call["id"])
            return ToolMessage(content="{}", tool_call_id=request.tool_call["id"])

    async def confirm(action, message):
        return {"status": "RECORDED", "operation_id": action["id"], "dependency_ready": True}

    async def verify(action, deps):
        checked.append(action["id"])
        return len(checked) == 1

    from omniharness.correction_batch import InvalidCorrectionBatch

    with pytest.raises(InvalidCorrectionBatch):
        await make_graph(Gateway(), confirm, verify=verify).ainvoke(
            {"missing_actions": actions, "tool_calls": calls, "dependency_graph": dependencies(actions)},
            config(),
            durability="sync",
        )
    assert observed == ["call1"] and checked == [actions[0]["id"], actions[1]["id"]]
