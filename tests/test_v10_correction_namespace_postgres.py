"""Disposable PostgreSQL namespace mapping experiment; no tools or models."""

from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from sqlalchemy import text
from test_postgres import postgres_environment  # noqa: F401
from test_v10_saver_drain_postgres import input_checkpoint, prepared

from omniharness.v10_namespace_prototype import CorrectionNamespacePrototype


@pytest.mark.asyncio
async def test_nested_correction_namespace_keeps_run_thread_and_original_root(request, tmp_path):
    pg = request.getfixturevalue("postgres_environment")
    async with prepared(pg, tmp_path, create_writer=False, legacy=True) as (
        _,
        run_id,
        factory,
        base,
        ticket,
        owner,
        settings,
    ):
        root_config, checkpoint, metadata, versions = input_checkpoint(run_id)
        original = await base.aput(root_config, checkpoint, metadata, versions)
        original_id = original["configurable"]["checkpoint_id"]
        prefix = "v10corr-" + "a" * 64
        saver = CorrectionNamespacePrototype(base, run_id=run_id, prefix=prefix)
        observed = []

        class State(TypedDict):
            value: str

        async def wait_for_approval(state, runtime: Runtime):
            info = runtime.execution_info
            observed.append((info.thread_id, info.checkpoint_ns, info.task_id))
            answer = interrupt({"type": "approval"})
            return {"value": answer}

        child_builder = StateGraph(State)
        child_builder.add_node("approval", wait_for_approval)
        child_builder.add_edge(START, "approval")
        child_builder.add_edge("approval", END)
        parent_builder = StateGraph(State)
        parent_builder.add_node("correction", child_builder.compile())
        parent_builder.add_edge(START, "correction")
        parent_builder.add_edge("correction", END)
        graph = parent_builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": run_id}}
        waiting = await graph.ainvoke({"value": "start"}, config, durability="sync")
        assert waiting["__interrupt__"]
        assert (await base.aget_tuple(root_config)).config["configurable"]["checkpoint_id"] == original_id
        finished = await graph.ainvoke(Command(resume="approved"), config, durability="sync")
        assert finished["value"] == "approved"
        assert len(observed) == 2 and observed[0] == observed[1]
        assert observed[0][0] == run_id and observed[0][1].startswith("correction:")
        assert (await base.aget_tuple(root_config)).config["configurable"]["checkpoint_id"] == original_id
        with factory() as db:
            namespaces = set(
                db.scalars(
                    text(
                        f"SELECT DISTINCT checkpoint_ns FROM {ticket.schema}.checkpoints WHERE thread_id=:run"
                    ),
                    {"run": run_id},
                )
            )
        assert "" in namespaces and prefix in namespaces
        assert any(value.startswith(prefix + "|correction:") for value in namespaces)
        assert all(value == "" or value == prefix or value.startswith(prefix + "|") for value in namespaces)
