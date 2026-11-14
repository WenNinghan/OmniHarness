"""Real graph + real official MCP HTTP server; local model, no paid requests."""

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select
from test_m4_mcp import mcp_server as mcp_server
from test_m4_runtime import ToolModel, graph_for, prepare

from omniharness.artifacts import ArtifactStore
from omniharness.m3_host import AgentOperationNeedsReview
from omniharness.m4_runtime import approval_resume_map
from omniharness.models import Approval, ArtifactVersion, Run, RunBudget, RunEvent, ToolOperation
from omniharness.worker import WorkerLock


class MCPCallModel(ToolModel):
    remote_tool: str = "record"

    def _generate(self, messages, **kwargs):
        if any(isinstance(message, ToolMessage) for message in messages):
            message = AIMessage(content="本地 MCP 调用流程结束，依据实际工具结果交付。")
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "mcp-counter-1",
                        "name": "mcp_call",
                        "args": {
                            "server_id": "local",
                            "tool_name": self.remote_tool,
                            "arguments": {"value": "one approved local effect"},
                        },
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def prepare_mcp(environment, tmp_path, live_server):
    worker, run_id, workspace, broker, sandbox, project = prepare(environment, tmp_path)
    mcp_settings, frozen, calls = live_server
    worker.settings.mcp_config_file = mcp_settings.mcp_config_file
    workspace["mcp"] = frozen
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        run.config_snapshot = {**run.config_snapshot, "workspace": workspace}
    return worker, run_id, workspace, broker, sandbox, calls


async def invoke_and_close(graph, scheduler, payload, config):
    try:
        return await graph.ainvoke(payload, config, durability="sync")
    finally:
        await scheduler.aclose()


def decision(client, identity, fingerprint, outcome="approve", request_id="mcp-decision-1"):
    return client.post(
        f"/api/v1/approvals/{identity}/decision",
        json={
            "fingerprint": fingerprint,
            "decision": outcome,
            "client_request_id": request_id,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["approve", "reject"])
async def test_real_mcp_approval_gate_duplicate_decision_and_rebuilt_graph(
    environment,
    tmp_path,
    mcp_server,
    outcome,
):
    worker, run_id, workspace, broker, sandbox, calls = prepare_mcp(environment, tmp_path, mcp_server)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
    client = environment[3]
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver, MCPCallModel())
        first = await invoke_and_close(
            graph, scheduler, {"messages": [("user", "记录一次本地测试计数")]}, config
        )
        assert first.get("__interrupt__")
        assert calls == []  # The remote readOnlyHint never grants permission.
        assert broker.calls == [] and sandbox.calls == []
        with worker.sessions() as db:
            approval = db.scalar(select(Approval))
            identity, fingerprint = approval.id, approval.fingerprint
            assert approval.status == "pending"
            operation = db.get(ToolOperation, approval.operation_id)
            assert operation.tool_name == "mcp_call" and operation.status == "awaiting_approval"
            assert operation.parameters["tool_name"] == "record"
        assert decision(client, identity, "0" * 64, outcome).status_code == 409
        assert calls == []
        for _ in range(2):
            response = decision(client, identity, fingerprint, outcome)
            assert response.status_code == 202, response.text
        assert calls == []  # The paired HTTP decision does not execute the tool.

        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver, MCPCallModel())
        state = await graph.aget_state(config)
        with worker.sessions() as db:
            signals = approval_resume_map(db, run_id, state.interrupts)
        assert signals
        result = await invoke_and_close(graph, scheduler, Command(resume=signals), config)
        assert not result.get("__interrupt__")
        expected = ["one approved local effect"] if outcome == "approve" else []
        assert calls == expected
        assert decision(client, identity, fingerprint, outcome).status_code == 202
        assert calls == expected

        # Rebuild the graph once more from the completed checkpoint; no replay.
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver, MCPCallModel())
        await invoke_and_close(graph, scheduler, None, config)
        assert calls == expected
        with worker.sessions() as db:
            operation = db.scalar(select(ToolOperation))
            assert not operation.slot_claimed
            assert db.get(RunBudget, run_id).tool_calls == (1 if outcome == "approve" else 0)
            events = list(db.scalars(select(RunEvent).where(RunEvent.event_type == "approval.decided")))
            assert len(events) == 1
            artifacts = list(db.scalars(select(ArtifactVersion).where(ArtifactVersion.kind == "mcp_result")))
            if outcome == "approve":
                assert operation.status == "completed"
                assert len(artifacts) == 1
                payload = ArtifactStore(worker.settings.data_dir / "artifacts").read(artifacts[0])
                assert json.loads(payload)["response"]["isError"] is False
            else:
                assert operation.status == "failed"
                assert operation.result["error"] == "APPROVAL_REJECTED"
                assert artifacts == []


@pytest.mark.asyncio
async def test_real_mcp_result_loss_is_not_replayed_after_approval_and_checkpoint_retry(
    environment,
    tmp_path,
    mcp_server,
):
    worker, run_id, workspace, broker, sandbox, calls = prepare_mcp(environment, tmp_path, mcp_server)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
    model = MCPCallModel(remote_tool="lost_response")
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver, model)
        result = await invoke_and_close(
            graph, scheduler, {"messages": [("user", "本地响应丢失故障注入")]}, config
        )
        assert result.get("__interrupt__") and calls == []
        with worker.sessions() as db:
            approval = db.scalar(select(Approval))
            identity, fingerprint = approval.id, approval.fingerprint
        assert decision(environment[3], identity, fingerprint).status_code == 202
        assert calls == []
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver, model)
        state = await graph.aget_state(config)
        with worker.sessions() as db:
            signals = approval_resume_map(db, run_id, state.interrupts)
        with pytest.raises(AgentOperationNeedsReview):
            await invoke_and_close(graph, scheduler, Command(resume=signals), config)
        assert calls == ["one approved local effect"]
        assert decision(environment[3], identity, fingerprint).status_code == 202
        graph, scheduler = graph_for(worker, run_id, workspace, broker, sandbox, saver, model)
        with pytest.raises(AgentOperationNeedsReview):
            await invoke_and_close(graph, scheduler, None, config)
        assert calls == ["one approved local effect"]
        with worker.sessions() as db:
            operation = db.scalar(select(ToolOperation))
            # A lost response cannot prove the remote effect has finished; keep
            # the durable slot until reconciliation, rather than replaying it.
            assert operation.status == "uncertain" and operation.slot_claimed
            assert db.get(RunBudget, run_id).tool_calls == 1
            assert not list(db.scalars(select(ArtifactVersion).where(ArtifactVersion.kind == "mcp_result")))
