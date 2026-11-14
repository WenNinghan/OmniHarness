import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from omniharness.m3_stages import STAGE_KEYS, StageOutputError, build_stage_graph


class Hooks:
    def __init__(self):
        self.started = []
        self.finished = []
        self.uncertain = []

    async def check_control(self, *_args):
        return None

    async def stage_requirements(self, stage, existing):
        if stage == "plan" and not any(getattr(m, "id", None) == "omni-command:one" for m in existing):
            return [HumanMessage(id="omni-command:one", content="增加一个约束")]
        return []

    async def reserve_invocation(self, _kind, stage, _tool, identity):
        self.started.append((stage, identity))
        return True

    async def finish_invocation(self, _kind, stage, _tool, identity):
        self.finished.append(stage)

    async def mark_invocation_uncertain(self, _kind, stage, _tool, identity):
        self.uncertain.append(stage)


class StageModel:
    def __init__(self, stage, replies, observed):
        self.stage, self.replies, self.observed = stage, replies, observed

    async def ainvoke(self, messages):
        self.observed.append((self.stage, list(messages)))
        return AIMessage(content=self.replies[self.stage])


def run_graph(plan=None):
    replies = {
        "think": "目标是回答用户问题。",
        "plan": plan or '{"steps":[{"id":"s1","task":"回答","acceptance":"回答清晰","depends_on":[]}]}',
        "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"回答问题","depends_on":[]}]}',
        "execute": "执行模型给出答案 42。",
        "review": '{"summary":"审查模型认为答案仍需验证。","findings":[],"unverified":["未运行测试"]}',
    }
    observed, hooks = [], Hooks()
    models = {stage: StageModel(stage, replies, observed) for stage in STAGE_KEYS}
    graph = build_stage_graph(models=models, hooks=hooks, checkpointer=InMemorySaver())
    return graph, hooks, observed


def test_five_distinct_stage_calls_share_checkpointed_results_and_commands():
    async def check():
        graph, hooks, observed = run_graph()
        result = await graph.ainvoke({"messages": [("user", "问题是什么？")]},
                                     {"configurable": {"thread_id": "run-1"}}, durability="sync")
        assert [x[0] for x in observed] == list(STAGE_KEYS)
        assert [x[0] for x in hooks.started] == list(STAGE_KEYS)
        assert hooks.finished == list(STAGE_KEYS)
        assert len({x[1].invocation_id for x in hooks.started}) == 5
        assert "one" in hooks.started[1][1].command_ids
        assert any("目标是回答" in str(m.content) for m in observed[1][1])
        assert any("执行模型给出答案" in str(m.content) for m in observed[4][1])
        assert "独立审查" in result["messages"][-1].content
        assert "没有文件、命令或联网工具" in result["messages"][-1].content
    asyncio.run(check())


def test_invalid_plan_stops_before_dispatch():
    async def check():
        graph, hooks, observed = run_graph('{"steps":[{"id":"s1","task":"x","acceptance":"x","depends_on":["bad"]}]}')
        with pytest.raises(StageOutputError):
            await graph.ainvoke({"messages": [("user", "hello")]},
                                {"configurable": {"thread_id": "run-2"}}, durability="sync")
        assert [x[0] for x in observed] == ["think", "plan"]
        assert hooks.finished == ["think", "plan"]
    asyncio.run(check())


def test_stage_response_length_stops_before_delegation_and_preserves_partial_output():
    async def check():
        hooks = Hooks()

        class Truncated:
            async def ainvoke(self, messages):
                return AIMessage(content="partial task analysis", additional_kwargs={"reasoning_content": "reason"},
                                 response_metadata={"finish_reason": "length"})

        graph = build_stage_graph(models={stage: Truncated() for stage in STAGE_KEYS}, hooks=hooks,
                                  checkpointer=InMemorySaver())
        with pytest.raises(StageOutputError) as exc:
            await graph.ainvoke({"messages": [("user", "test")]},
                               {"configurable": {"thread_id": "length-test"}}, durability="sync")
        assert exc.value.code == "OUTPUT_TRUNCATED"
        assert exc.value.output.text == "partial task analysis"
        assert exc.value.output.reasoning == "reason"
        assert exc.value.stage == "think"
        assert len(hooks.started) == 1
        assert hooks.finished == ["think"]

    asyncio.run(check())


def test_stage_requests_only_share_answer_artifacts_not_native_reasoning():
    async def check():
        graph, _, observed = run_graph()
        prior = AIMessage(content=[{"type": "thinking", "thinking": "private-thought", "signature": "sig"},
                                   {"type": "text", "text": "public result"}],
                          additional_kwargs={"reasoning_content": "private-native"})
        await graph.ainvoke({"messages": [prior, HumanMessage(content="next task")]},
                           {"configurable": {"thread_id": "public-artifacts"}}, durability="sync")
        for _, messages in observed:
            for message in messages:
                if isinstance(message, AIMessage):
                    assert "private-thought" not in str(message.content)
                    assert "private-native" not in str(message.additional_kwargs)
                    assert "signature" not in str(message.content)

    asyncio.run(check())


def test_pause_between_stages_resumes_without_repeating_completed_calls():
    from langgraph.types import Command, interrupt

    class PausingHooks(Hooks):
        async def check_control(self, _kind, stage, _tool):
            if stage == "dispatch" and not hasattr(self, "resumed"):
                answer = interrupt({"type": "pause"})
                assert answer == "continue"
                self.resumed = True

    async def check():
        original, _, observed = run_graph()
        del original
        hooks = PausingHooks()
        replies = {"think": "understand", "plan": '{"steps":[{"id":"s1","task":"do","acceptance":"ok"}]}',
                   "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"do"}]}',
                   "execute": "done", "review": '{"summary":"unverified","findings":[],"unverified":["not checked"]}'}
        graph = build_stage_graph(models={s: StageModel(s, replies, observed) for s in STAGE_KEYS},
                                  hooks=hooks, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "pause-run"}}
        first = await graph.ainvoke({"messages": [("user", "hi")]}, config, durability="sync")
        assert first.get("__interrupt__")
        assert [row[0] for row in observed] == ["think", "plan"]
        result = await graph.ainvoke(Command(resume="continue"), config, durability="sync")
        assert [row[0] for row in observed] == list(STAGE_KEYS)
        assert "独立审查" in result["messages"][-1].content
    asyncio.run(check())


def test_independent_tasks_run_at_most_two_in_parallel_then_dependents():
    async def check():
        hooks, observed = Hooks(), []
        active, peak = 0, 0
        replies = {
            "think": "understand",
            "plan": '{"steps":[{"id":"s1","task":"a","acceptance":"ok"},{"id":"s2","task":"b","acceptance":"ok"},{"id":"s3","task":"c","acceptance":"ok","depends_on":["s1","s2"]}]}',
            "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"a"},{"id":"t2","step_id":"s2","instruction":"b"},{"id":"t3","step_id":"s3","instruction":"c","depends_on":["t1","t2"]}]}',
            "review": '{"summary":"review all outputs","findings":[],"unverified":["not checked"]}',
        }
        class Model(StageModel):
            async def ainvoke(self, messages):
                nonlocal active, peak
                if self.stage != "execute":
                    return await super().ainvoke(messages)
                instruction = messages[-1].content
                task_id = next(t for t in ("t1", "t2", "t3") if f'"id": "{t}"' in instruction)
                if task_id == "t3":
                    assert 'OUT-t1' in instruction and 'OUT-t2' in instruction
                active += 1
                peak = max(peak, active)
                observed.append((task_id, "start"))
                await asyncio.sleep(.02)
                observed.append((task_id, "end"))
                active -= 1
                return AIMessage(content=f"OUT-{task_id}")
        models = {s: Model(s, replies, observed) for s in STAGE_KEYS}
        graph = build_stage_graph(models=models, hooks=hooks, checkpointer=InMemorySaver())
        result = await graph.ainvoke({"messages": [("user", "hi")]},
                                     {"configurable": {"thread_id": "parallel"}}, durability="sync")
        assert peak == 2
        assert observed.index(("t3", "start")) > observed.index(("t1", "end"))
        assert observed.index(("t3", "start")) > observed.index(("t2", "end"))
        assert all(f"OUT-{t}" in result["messages"][-1].content for t in ("t1", "t2", "t3"))
        executions = [identity.invocation_id for stage, identity in hooks.started if stage == "execute"]
        assert len(executions) == len(set(executions)) == 3
    asyncio.run(check())


def test_uncertain_parallel_call_is_not_replayed_after_resume():
    from omniharness.m3_agents import AgentExternalCallFailed
    from omniharness.m3_host import AgentOperationNeedsReview

    class DurableHooks(Hooks):
        def __init__(self):
            super().__init__()
            self.identities = set()
        async def reserve_invocation(self, kind, stage, tool, identity):
            if identity.invocation_id in self.identities:
                raise AgentOperationNeedsReview("previous call requires inspection")
            self.identities.add(identity.invocation_id)
            return await super().reserve_invocation(kind, stage, tool, identity)

    async def check():
        hooks, observed = DurableHooks(), []
        replies = {"think": "understand",
            "plan": '{"steps":[{"id":"s1","task":"a","acceptance":"ok"},{"id":"s2","task":"b","acceptance":"ok"}]}',
            "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"a"},{"id":"t2","step_id":"s2","instruction":"b"}]}',
            "review": "must not run"}
        class FailingModel(StageModel):
            async def ainvoke(self, messages):
                if self.stage != "execute":
                    return await super().ainvoke(messages)
                text = messages[-1].content
                task_id = "t1" if '"id": "t1"' in text else "t2"
                observed.append(("execute", task_id))
                if task_id == "t2":
                    await asyncio.sleep(.02)
                    raise RuntimeError("synthetic failure")
                return AIMessage(content="successful child")
        graph = build_stage_graph(models={s: FailingModel(s, replies, observed) for s in STAGE_KEYS},
                                  hooks=hooks, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "failed-child"}}
        with pytest.raises(AgentExternalCallFailed):
            await graph.ainvoke({"messages": [("user", "hi")]}, config, durability="sync")
        assert hooks.uncertain == ["execute"]
        with pytest.raises(AgentOperationNeedsReview):
            await graph.ainvoke(None, config, durability="sync")
        assert [x for x in observed if x[0] == "execute"].count(("execute", "t1")) == 1
        assert [x for x in observed if x[0] == "execute"].count(("execute", "t2")) == 1
    asyncio.run(check())


def test_stage_failure_uses_optional_diagnostics_without_changing_transport_or_retrying():
    from omniharness.m3_agents import AgentExternalCallFailed

    class DiagnosticHooks(Hooks):
        async def mark_invocation_uncertain_with_diagnostics(
            self, kind, stage, tool, identity, *, error, transport_mode,
        ):
            self.diagnostic = (kind, stage, tool, type(error), transport_mode)
            await self.mark_invocation_uncertain(kind, stage, tool, identity)

    async def check():
        hooks, observed = DiagnosticHooks(), []

        class FailingModel:
            async def ainvoke(self, messages):
                observed.append(messages)
                raise TimeoutError("private-error-canary")

        graph = build_stage_graph(models={s: FailingModel() for s in STAGE_KEYS},
                                  hooks=hooks, checkpointer=InMemorySaver())
        with pytest.raises(AgentExternalCallFailed) as error:
            await graph.ainvoke({"messages": [("user", "hi")]},
                               {"configurable": {"thread_id": "diagnostic-failure"}}, durability="sync")
        assert hooks.diagnostic == ("model_calls", "think", None, TimeoutError, "ainvoke_default")
        assert hooks.uncertain == ["think"] and hooks.finished == []
        assert len(observed) == 1
        assert "private-error-canary" not in str(error.value)

    asyncio.run(check())


def test_dispatch_must_preserve_plan_dependencies():
    async def check():
        hooks, observed = Hooks(), []
        replies = {
            "think": "understand",
            "plan": '{"steps":[{"id":"s1","task":"a","acceptance":"ok"},{"id":"s2","task":"b","acceptance":"ok","depends_on":["s1"]}]}',
            "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"a"},{"id":"t2","step_id":"s2","instruction":"b"}]}',
        }
        graph = build_stage_graph(models={s: StageModel(s, replies, observed) for s in STAGE_KEYS},
                                  hooks=hooks, checkpointer=InMemorySaver())
        with pytest.raises(StageOutputError):
            await graph.ainvoke({"messages": [("user", "hi")]},
                                {"configurable": {"thread_id": "bad-dependency"}}, durability="sync")
        assert [x[0] for x in observed] == ["think", "plan", "dispatch"]
    asyncio.run(check())


def test_invalid_review_status_stops_without_fabricating_success():
    async def check():
        graph, _, observed = run_graph()
        # Replace only the review model while retaining the distinct-stage setup.
        responses = {"think": "ok", "plan": '{"steps":[{"id":"s1","task":"a","acceptance":"ok"}]}',
                     "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"a"}]}',
                     "execute": "answer", "review": '{"summary":"ok","findings":[{"location":"x",'
                     '"severity":"LOW","description":"x","status":"VERIFIED",'
                     '"recommendation":"check"}],"unverified":[]}'}
        del graph
        graph = build_stage_graph(models={s: StageModel(s, responses, observed) for s in STAGE_KEYS},
                                  hooks=Hooks(), checkpointer=InMemorySaver())
        with pytest.raises(StageOutputError):
            await graph.ainvoke({"messages": [("user", "hi")]},
                                {"configurable": {"thread_id": "bad-review"}}, durability="sync")
    asyncio.run(check())


def test_v1_stage_workflow_retains_free_text_review():
    from omniharness.stage_contract import STAGE_V1_WORKFLOW_VERSION

    async def check():
        observed = []
        replies = {"think": "ok", "plan": '{"steps":[{"id":"s1","task":"a","acceptance":"ok"}]}',
                   "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"a"}]}',
                   "execute": "answer", "review": "legacy free text review"}
        graph = build_stage_graph(models={s: StageModel(s, replies, observed) for s in STAGE_KEYS},
                                  hooks=Hooks(), checkpointer=InMemorySaver(),
                                  workflow_version=STAGE_V1_WORKFLOW_VERSION)
        result = await graph.ainvoke({"messages": [("user", "hi")]},
                                     {"configurable": {"thread_id": "v1"}}, durability="sync")
        assert result["review"] == "legacy free text review"
        assert "review_data" not in result
    asyncio.run(check())
