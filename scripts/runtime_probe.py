"""M0 offline proof of the installed OSS runtime, never a live-provider test.

Run with ``python scripts/runtime_probe.py``. A deterministic local chat model
drives real Deep Agents tool calls; SQLite is closed and reopened before resume.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import Any, TypedDict


async def run_runtime_probe(work_dir: Path) -> dict[str, Any]:
    from deepagents import create_deep_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt
    from langsmith import tracing_context

    class DeterministicModel(BaseChatModel):
        scenario: str

        @property
        def _llm_type(self) -> str:
            return "omniharness-offline-runtime-probe"

        def bind_tools(self, tools: Any, **kwargs: Any) -> DeterministicModel:
            return self

        def _generate(
            self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any
        ) -> ChatResult:
            tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
            if tool_messages:
                response = AIMessage(
                    content="[模拟运行时探针] " + " | ".join(str(m.content) for m in tool_messages)
                )
            elif self.scenario == "supervisor":
                response = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "Run probe_work with label researcher.",
                                "subagent_type": "researcher",
                            },
                            "id": "delegate-researcher",
                            "type": "tool_call",
                        },
                        {
                            "name": "task",
                            "args": {
                                "description": "Run probe_work with label coder.",
                                "subagent_type": "coder",
                            },
                            "id": "delegate-coder",
                            "type": "tool_call",
                        },
                    ],
                )
            else:
                response = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "probe_work",
                            "args": {"label": self.scenario},
                            "id": f"work-{self.scenario}",
                            "type": "tool_call",
                        },
                    ],
                )
            return ChatResult(generations=[ChatGeneration(message=response)])

        async def _agenerate(
            self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any
        ) -> ChatResult:
            return self._generate(messages, stop=stop, **kwargs)

    barrier = asyncio.Event()
    activity: dict[str, Any] = {"active": 0, "max_parallel": 0, "completed": []}

    @tool
    async def probe_work(label: str) -> str:
        """Run a harmless local probe and rendezvous with the other subagent."""
        activity["active"] += 1
        activity["max_parallel"] = max(activity["max_parallel"], activity["active"])
        if activity["active"] == 2:
            barrier.set()
        try:
            await asyncio.wait_for(barrier.wait(), timeout=10)
            await asyncio.sleep(0.01)
            activity["completed"].append(label)
            return f"verified-tool-result:{label}"
        finally:
            activity["active"] -= 1

    with tracing_context(enabled=False):
        agent = create_deep_agent(
            model=DeterministicModel(scenario="supervisor"),
            system_prompt="Offline runtime probe; delegate the two requested tasks.",
            tools=[],
            subagents=[
                {
                    "name": label,
                    "description": f"Offline {label} probe",
                    "system_prompt": "Call probe_work once, then return its result.",
                    "model": DeterministicModel(scenario=label),
                    "tools": [probe_work],
                }
                for label in ("researcher", "coder")
            ],
        )
        async with asyncio.timeout(30):
            result = await agent.ainvoke({"messages": [("user", "Run both subagents concurrently.")]})
    final = result["messages"][-1].content
    parallel_ok = activity["max_parallel"] == 2 and sorted(activity["completed"]) == ["coder", "researcher"]
    tool_ok = all(f"verified-tool-result:{label}" in final for label in ("researcher", "coder"))

    class ApprovalState(TypedDict, total=False):
        value: int
        approved: bool
        result: int

    def approval(state: ApprovalState) -> dict:
        answer = interrupt({"type": "offline_probe_approval", "value": state["value"]})
        return {"approved": answer is True}

    def execute(state: ApprovalState) -> dict:
        return {"result": state["value"] * 2 if state["approved"] else 0}

    def make_graph(saver: AsyncSqliteSaver):
        builder = StateGraph(ApprovalState)
        builder.add_node("approval", approval)
        builder.add_node("execute", execute)
        builder.add_edge(START, "approval")
        builder.add_edge("approval", "execute")
        builder.add_edge("execute", END)
        return builder.compile(checkpointer=saver)

    work_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_file = work_dir / "runtime-probe.sqlite3"
    config = {"configurable": {"thread_id": "omniharness-m0-offline-probe"}}
    with tracing_context(enabled=False):
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_file)) as saver:
            paused = await make_graph(saver).ainvoke({"value": 21}, config, durability="sync")
            persisted_interrupt = bool(paused.get("__interrupt__"))
        # The first saver/connection and graph have both gone away. Resume using
        # freshly constructed objects reading the database, not an in-memory map.
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_file)) as reopened:
            graph = make_graph(reopened)
            before = await graph.aget_state(config)
            resumed = await graph.ainvoke(Command(resume=True), config, durability="sync")
            after = await graph.aget_state(config)
    restore_ok = (
        persisted_interrupt
        and before.next == ("approval",)
        and resumed.get("result") == 42
        and not after.next
    )
    return {
        "passed": parallel_ok and tool_ok and restore_ok,
        "mode": "offline-deterministic-model",
        "live_provider_verified": False,
        "versions": {
            name: version(name)
            for name in ("deepagents", "langgraph", "langchain-core", "langgraph-checkpoint-sqlite")
        },
        "deep_agents_tool_loop": {"passed": tool_ok, "completed_tools": sorted(activity["completed"])},
        "parallel_subagents": {"passed": parallel_ok, "max_parallel": activity["max_parallel"]},
        "sqlite_interrupt_resume": {
            "passed": restore_ok,
            "disk_database": checkpoint_file.is_file(),
            "fresh_connection": True,
            "result": resumed.get("result"),
        },
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="omniharness-runtime-probe-") as directory:
        report = asyncio.run(run_runtime_probe(Path(directory)))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
