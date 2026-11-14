import pytest

from omniharness.m4_contract import M4_V9_PLUGIN_VERSIONS, M4_V10_PLUGIN_VERSIONS, workspace_prompts
from omniharness.m4_invalid_audit import _domain


def test_v10_freezes_separate_plan_and_audit_domains():
    assert workspace_prompts("stage-agent-v10")["execute"] == workspace_prompts("stage-agent-v9")["execute"]
    assert "action_candidates" in workspace_prompts("stage-agent-v10")["plan"]
    assert "action_candidates" not in workspace_prompts("stage-agent-v9")["plan"]
    assert M4_V10_PLUGIN_VERSIONS == {**M4_V9_PLUGIN_VERSIONS, "builtin-action-contract": "2",
                                     "builtin-native-execution-inventory": "1"}
    assert set(_domain("stage-agent-v10")).isdisjoint(_domain("stage-agent-v9"))


def test_native_inventory_screen_requires_matching_complete_tool_free_response(environment):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    from omniharness.m3_agents import _hash
    from omniharness.services import operation_id
    from omniharness.v10_closure import model_scope_mac, screen_zero_tool_execution_inventory

    settings = environment[0]
    run = SimpleNamespace(id="run", workspace_id="workspace", workflow_version="stage-agent-v10")
    invocation = _hash({"namespace": "child", "task_id": "graph-task", "model": True})
    reservation = SimpleNamespace(run_id="run", kind="model_calls", invocation_id=invocation,
                                  binding_fingerprint="binding")
    response = AIMessage(content="done", response_metadata={"finish_reason": "stop"}).model_dump(mode="json")
    scope = {"run_id": "run", "task_id": "task", "checkpoint_ns": "child", "graph_task_id": "graph-task",
             "invocation_id": invocation}
    record = SimpleNamespace(run_id="run", workflow_version="stage-agent-v10", status="completed",
        node_id="agent:execute:model_calls", operation_id=operation_id("run", "agent:execute:model_calls",
        {"invocation_id": invocation}), result={"purpose": "task", "binding_fingerprint": "binding",
            "response": {"messages": [response]}, "v10_model_scope": scope})
    def sign():
        record.result["v10_model_scope_mac"] = model_scope_mac(settings, run, record.operation_id, scope,
                                                              record.result["response"])
    sign()
    def screen(records, reservations):
        return screen_zero_tool_execution_inventory(run=run, settings=settings, task_id="task",
                                                    records=records, reservations=reservations)
    assert screen([record], [reservation]) == (True, "inventory_screened_not_closed")
    assert screen([record], [])[0] is False
    response["content"] = "altered"
    assert screen([record], [reservation]) == (False, "model_scope_authentication_failed")
    response["content"] = "done"
    response["response_metadata"] = {}
    sign()
    assert screen([record], [reservation])[0] is False
    response["response_metadata"] = {"finish_reason": "stop"}
    response["additional_kwargs"] = {"function_call": {"name": "unknown"}}
    sign()
    assert screen([record], [reservation])[0] is False
    response["additional_kwargs"] = {}
    response["content"] = [{"type": "text", "text": "done"},
                           {"type": "server_tool_use", "name": "unknown", "id": "x", "input": {}}]
    sign()
    assert screen([record], [reservation]) == (False, "native_response_has_or_may_have_tool_calls")


def test_checkpoint_pin_requires_current_owner_and_exact_root_watermark():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from omniharness.checkpoint_fencing import WORKER_LOCK_KEY, FenceTicket
    from omniharness.v10_closure import CheckpointPinUnavailable, pin_checkpoint_watermark

    started = datetime.now(timezone.utc)
    ticket = FenceTicket("run", 3, "stage-agent-v10", 41, started, "checkpoints_test")
    owner = {"epoch": 3, "workflow_version": "stage-agent-v10", "owner_pid": 41,
             "current_pid": 41, "owner_started": started, "lock_key": WORKER_LOCK_KEY, "owner_live": True}
    class Result:
        def __init__(self, value): self.value = value
        def mappings(self): return self
        def one_or_none(self): return self.value
        def all(self): return self.value
    class DB:
        def __init__(self): self.calls = []
        def get_bind(self): return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        def in_transaction(self): return True
        def execute(self, statement, params):
            self.calls.append(str(statement))
            return Result(owner if len(self.calls) == 1 else [{"checkpoint_ns": "", "checkpoint_id": "root"},
                {"checkpoint_ns": "child", "checkpoint_id": "child-id"}])
    db = DB()
    assert pin_checkpoint_watermark(db, ticket, "root") == {"": "root", "child": "child-id"}
    assert "FOR UPDATE" in db.calls[0]
    with pytest.raises(CheckpointPinUnavailable):
        pin_checkpoint_watermark(DB(), ticket, "outdated")
    owner["current_pid"] = 42
    with pytest.raises(CheckpointPinUnavailable):
        pin_checkpoint_watermark(DB(), ticket, "root")


async def test_v10_invalid_tool_replay_and_foreign_marker_fail_closed(environment, monkeypatch):
    import pytest
    from sqlalchemy import select
    from test_m3_host import prepared
    from test_v8_invalid_audit import TestHost, call, error

    from omniharness import m4_invalid_audit as audit
    from omniharness.m3_host import AgentOperationNeedsReview, FencedAgentHost
    from omniharness.models import OperationRecord, Run, ToolOperation
    from omniharness.services import operation_id
    from omniharness.worker import Worker, WorkerLock

    class V10Host(TestHost):
        workflow_version = "stage-agent-v10"

        def __init__(self, worker, run_id):
            FencedAgentHost.__init__(self, worker, run_id, self.workflow_version, role_versions={},
                                     plugin_versions=self.plugin_versions, role_bindings={"execute": "execute"})

    settings, engine, factory, client = environment
    run_id = prepared(settings, engine, factory, client)
    with factory.begin() as db:
        db.get(Run, run_id).workflow_version = "stage-agent-v10"
    monkeypatch.setattr(audit, "_scope", lambda: {"id": "task-1", "write_paths": None})
    assert len({audit._key(settings, v) for v in ("stage-agent-v8", "stage-agent-v9", "stage-agent-v10")}) == 3
    worker = Worker(settings, engine)
    with WorkerLock(engine, settings) as owner:
        worker.lock = owner
        host = V10Host(worker, run_id)
        request = call(run_id, "same")
        saved = await audit.audit_invalid(host, request, "workspace_patch", error(request))
        assert await audit.audit_invalid(host, request, "workspace_patch", error(request)) == saved
        with factory() as db:
            rows = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
            assert len(rows) == 1 and rows[0].tool_version == audit.V10_POLICY
        with factory.begin() as db:
            db.add(OperationRecord(operation_id=operation_id(run_id, "invalid-tool-audit-key-v9-v1", {}),
                run_id=run_id, node_id="invalid-tool-audit-key-v9-v1", parameters_hash="foreign",
                status="completed", attempts=1, workflow_version="stage-agent-v9"))
        with pytest.raises(AgentOperationNeedsReview):
            await audit.audit_invalid(host, request, "workspace_patch", error(request))


@pytest.mark.parametrize("explicit_cleared", [False, True])
async def test_v10_graph_binds_candidate_to_original_message_and_task(environment, tmp_path, explicit_cleared, monkeypatch):
    import json
    from copy import deepcopy

    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver
    from sqlalchemy import select
    from test_m4_collaboration import prepare_v4

    from omniharness.action_outcome import compute_action_outcome
    from omniharness.artifacts import ArtifactStore
    from omniharness.m3_delegation import HierarchicalDelegation
    from omniharness.m3_stages import build_stage_graph
    from omniharness.m4_contract import workspace_role_versions
    from omniharness.m4_runtime import M4AgentHost, build_execution_agent
    from omniharness.models import Message, OperationRecord, Run, RunBudgetReservation
    from omniharness.stage_contract import STAGE_KEYS, STAGE_ROLE_BINDINGS
    from omniharness.worker import WorkerLock

    worker, run_id, workspace, broker, sandbox, project = prepare_v4(environment, tmp_path)
    del project
    versions = workspace_role_versions("stage-agent-v10")
    text = "请创建文件 hello.py"
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        source = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
        source.content = text
        source_id = source.id
        agent = {**run.config_snapshot["agent"], "workflow_version": "stage-agent-v10",
                 "plugin_versions": M4_V10_PLUGIN_VERSIONS}
        agent["roles"] = {role: {**agent["roles"][role], "role_version": versions[role]}
                          for role in STAGE_KEYS}
        run.config_snapshot = {**run.config_snapshot, "agent": agent}
        if explicit_cleared:
            from omniharness.action_contract import validate_explicit_actions
            run.config_snapshot = {**run.config_snapshot, "action_contract": {"version": 2,
                "request": {"requirements": None}, "explicit": validate_explicit_actions(
                    [], run_id=run.id, workspace_id=run.workspace_id)}}
        run.workflow_version = "stage-agent-v10"

    class Stage:
        def __init__(self, role):
            self.role = role

        async def ainvoke(self, messages):
            last = messages[-1]
            if last.id == "omni-interaction:preflight":
                return AIMessage(content=json.dumps({"status": "continue", "questions": []}))
            if self.role == "plan":
                assert source_id in str(last.content)
                return AIMessage(content=json.dumps({"goal": "create", "constraints": [], "steps": [
                    {"id": "s1", "task": "create", "acceptance": "exists", "depends_on": [],
                     "write_paths": ["hello.py"]}], "action_candidates": [{"kind": "workspace_file_write",
                     "path": "hello.py", "step_id": "s1", "source_message_id": source_id,
                     "source_excerpt": text}]}, ensure_ascii=False))
            if self.role == "review":
                return AIMessage(content=json.dumps({"summary": "unverified", "findings": [], "unverified": []}))
            return AIMessage(content="记录尚不足以验证。")

    class Execution(BaseChatModel):
        @property
        def _llm_type(self): return "v10-test"
        def bind_tools(self, tools, **kwargs): return self
        def _generate(self, messages, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content=json.dumps({"status": "completed", "content": "完成", "questions": []}, ensure_ascii=False),
                response_metadata={"finish_reason": "stop"}))])
        async def _agenerate(self, messages, **kwargs): return self._generate(messages, **kwargs)

    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        host = M4AgentHost(worker, run_id, "stage-agent-v10", role_versions=versions,
                           plugin_versions=M4_V10_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
        captured_identities = []
        replay = host.replay_invocation
        async def capture_replay(kind, role, tool_name, identity):
            if kind == "model_calls" and role == "execute":
                captured_identities.append(identity)
            return await replay(kind, role, tool_name, identity)
        host.replay_invocation = capture_replay
        scheduler = HierarchicalDelegation()
        host.scheduler = scheduler
        executor = build_execution_agent(model=Execution(), host=host, workspace=workspace, broker=broker,
            sandbox=sandbox, artifacts=ArtifactStore(worker.settings.data_dir / "artifacts"), scheduler=scheduler)
        saver = InMemorySaver()
        graph = build_stage_graph(models={role: Stage(role) for role in STAGE_KEYS}, hooks=host,
            checkpointer=saver, workflow_version="stage-agent-v10", execution_agent=executor,
            execution_strategy="compact")
        try:
            await graph.ainvoke({"messages": [HumanMessage(content=text, id=source_id)]},
                                {"configurable": {"thread_id": run_id}, "recursion_limit": 100}, durability="sync")
            if not explicit_cleared:
                from omniharness import m4_collaboration
                from omniharness.m3_host import AgentOperationNeedsReview
                from omniharness.v10_closure import model_scope_mac
                assert len(captured_identities) == 1
                identity = captured_identities[0]
                monkeypatch.setattr(m4_collaboration, "current_task_scope", lambda: {"id": "t1", "write_paths": None})
                assert await replay("model_calls", "execute", None, identity) is not None
                with worker.sessions() as db:
                    row = db.scalar(select(OperationRecord).where(OperationRecord.run_id == run_id,
                        OperationRecord.node_id == "agent:execute:model_calls"))
                    original = deepcopy(row.result)
                    operation_id = row.operation_id
                for field in ("missing_mac", "task_id", "checkpoint_ns", "graph_task_id", "invocation_id"):
                    with worker.sessions.begin() as db:
                        row = db.get(OperationRecord, operation_id)
                        result = deepcopy(original)
                        if field == "missing_mac":
                            result.pop("v10_model_scope_mac")
                        else:
                            result["v10_model_scope"][field] = "tampered"
                            result["v10_model_scope_mac"] = model_scope_mac(worker.settings, db.get(Run, run_id),
                                operation_id, result["v10_model_scope"], result["response"])
                        row.result = result
                    with pytest.raises(AgentOperationNeedsReview):
                        await replay("model_calls", "execute", None, identity)
                with worker.sessions.begin() as db:
                    db.get(OperationRecord, operation_id).result = original
                history = [item async for item in graph.aget_state_history({"configurable": {"thread_id": run_id}})]
                stale = next(item for item in history if item.next == ("execute_task",))
                with pytest.raises(AgentOperationNeedsReview):
                    await graph.ainvoke(None, {**stale.config, "recursion_limit": 100}, durability="sync")
        finally:
            await scheduler.aclose()
        with worker.sessions() as db:
            run = db.get(Run, run_id)
            envelope = run.outcome["v10_actions"]
            records = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run_id,
                OperationRecord.node_id == "agent:execute:model_calls")))
            assert len(records) == 1
            scope = records[0].result["v10_model_scope"]
            child_ns = scope["checkpoint_ns"].rsplit("|", 1)[0]
            child = await saver.aget_tuple({"configurable": {"thread_id": run_id, "checkpoint_ns": child_ns}})
            child_state = await executor.aget_state({"configurable": {"thread_id": run_id,
                "checkpoint_ns": child_ns, "__pregel_checkpointer": saver}})
            assert not child_state.next and not child_state.interrupts
            observation = run.outcome["v10_child_executions"][scope["task_id"]]
            assert observation["manifest"]["checkpoint_id"] == child.config["configurable"]["checkpoint_id"]
            from omniharness.v10_closure import child_terminal_mac
            assert observation["mac"] == child_terminal_mac(worker.settings, run, observation["manifest"])
            assert scope["run_id"] == run_id and scope["task_id"] and scope["checkpoint_ns"]
            reservation = db.get(RunBudgetReservation, {"run_id": run_id, "invocation_id": scope["invocation_id"]})
            from omniharness.v10_closure import screen_zero_tool_execution_inventory
            assert screen_zero_tool_execution_inventory(run=run, settings=worker.settings, task_id=scope["task_id"],
                records=records, reservations=[reservation]) == (True, "inventory_screened_not_closed")
            if explicit_cleared:
                assert envelope["assessed"] is False and envelope["actions"] == []
                return
            action = envelope["actions"][0]
            assert action["source_message_id"] == source_id
            assert action["task_id"]
            outcome = compute_action_outcome(db, run, ArtifactStore(worker.settings.data_dir / "artifacts"),
                                             worker.settings)
            assert outcome["status"] == "UNVERIFIED"
            assert outcome["actions"][0]["reason"] == "no_matching_record"
