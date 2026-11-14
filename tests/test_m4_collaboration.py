"""v4 contracts: deterministic dispatch, conservative writes, real graph evidence."""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from sqlalchemy import select
from test_m4_runtime import StageModel, ToolModel, prepare

from omniharness.artifacts import ArtifactStore
from omniharness.m3_delegation import HierarchicalDelegation
from omniharness.m3_stages import build_stage_graph
from omniharness.m4_collaboration import CollaborationPlan, deterministic_dispatch, independent_batch
from omniharness.m4_contract import COLLAB_STAGE_VERSIONS, COLLAB_WORKFLOW_VERSION, M4_PLUGIN_VERSIONS
from omniharness.m4_runtime import M4AgentHost, build_execution_agent
from omniharness.models import Run, RunBudget, ToolOperation
from omniharness.review_evidence import build_review_evidence
from omniharness.stage_contract import STAGE_KEYS, STAGE_ROLE_BINDINGS
from omniharness.worker import WorkerLock


def test_compact_dispatch_keeps_acceptance_and_dependencies():
    plan = CollaborationPlan.model_validate({"goal": "goal", "constraints": ["keep API"], "steps": [
        {"id": "a", "task": "change a", "acceptance": "a works", "write_paths": ["a.py"]},
        {"id": "b", "task": "verify b", "acceptance": "b works", "depends_on": ["a"], "write_paths": []},
    ]})
    tasks = deterministic_dispatch(plan.model_dump())["tasks"]
    assert tasks[1]["depends_on"] == ["t1"]
    assert tasks[1]["acceptance"] == "b works" and tasks[1]["write_paths"] == []


@pytest.mark.parametrize("path", ["../a.py", "/etc/a", "C:/a.py", "x\\a.py", "*.py", "src/../a", "a//b"])
def test_plan_cannot_declare_unsafe_write_paths(path):
    with pytest.raises(ValidationError):
        CollaborationPlan.model_validate({"steps": [{"id": "a", "task": "fix", "acceptance": "works", "write_paths": [path]}]})


@pytest.mark.parametrize("left,right,parallel", [(None, ["b.py"], False), (["A.py"], ["a.py"], False),
    (["src"], ["src/b.py"], False), (["src/a.py"], ["src/b.py"], True), ([], [], True)])
def test_write_overlap_or_unknown_serializes(left, right, parallel):
    tasks = [{"id": "a", "write_paths": left}, {"id": "b", "write_paths": right}]
    assert len(independent_batch(tasks)) == (2 if parallel else 1)


def prepare_v4(environment, tmp_path, *, approval=False):
    prepared = prepare(environment, tmp_path, approval=approval)
    worker, run_id, *_ = prepared
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        agent = {**run.config_snapshot["agent"], "workflow_version": COLLAB_WORKFLOW_VERSION}
        for role in STAGE_KEYS:
            agent["roles"][role] = {**agent["roles"][role], "role_version": COLLAB_STAGE_VERSIONS[role]}
        run.config_snapshot = {**run.config_snapshot, "agent": agent}
        run.workflow_version = COLLAB_WORKFLOW_VERSION
    return prepared


class InspectStageModel(StageModel):
    calls: list = []
    plan_paths: list[str] | None = None

    async def ainvoke(self, messages):
        self.calls.append((self.stage, messages))
        if self.stage == "plan":
            return AIMessage(content=json.dumps({"goal": "修复并测试", "constraints": ["保留接口"], "steps": [
                {"id": "s1", "task": "修复 main.py 并测试", "acceptance": "输出42",
                 "write_paths": self.plan_paths}]}))
        return await super().ainvoke(messages)


def graph_v4(worker, run_id, workspace, broker, sandbox, saver, *, strategy="compact", stages=None, executor_model=None):
    host = M4AgentHost(worker, run_id, COLLAB_WORKFLOW_VERSION, role_versions=COLLAB_STAGE_VERSIONS,
                       plugin_versions=M4_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
    scheduler = HierarchicalDelegation()
    execution = build_execution_agent(model=executor_model or ToolModel(), host=host, workspace=workspace,
        broker=broker, sandbox=sandbox, artifacts=ArtifactStore(worker.settings.data_dir / "artifacts"), scheduler=scheduler)
    graph = build_stage_graph(models=stages or {role: InspectStageModel(role) for role in STAGE_KEYS}, hooks=host,
        checkpointer=saver, workflow_version=COLLAB_WORKFLOW_VERSION, execution_agent=execution, execution_strategy=strategy)
    return graph, scheduler


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy,calls", [("single", 4), ("compact", 6), ("staged", 8)])
async def test_v4_real_graph_modes_have_expected_model_calls_and_host_evidence(environment, tmp_path, strategy, calls):
    worker, run_id, workspace, broker, sandbox, project = prepare_v4(environment, tmp_path)
    stages = {role: InspectStageModel(role) for role in STAGE_KEYS}
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_v4(worker, run_id, workspace, broker, sandbox, InMemorySaver(), strategy=strategy, stages=stages)
        try:
            result = await graph.ainvoke({"messages": [HumanMessage(content="修复并测试")]},
                {"configurable": {"thread_id": run_id}, "recursion_limit": 100}, durability="sync")
        finally:
            await scheduler.aclose()
    assert (project / "main.py").read_text() == "print(42)\n"
    assert sandbox.calls == ["python main.py"]
    assert len(result["task_results"]) == 1
    with worker.sessions() as db:
        assert db.get(RunBudget, run_id).model_calls == calls
        operations = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
        assert len(operations) == 3
        assert all(op.result.get("host_task_id") == ("single" if strategy == "single" else "t1") for op in operations)
    if strategy != "single":
        review_messages = stages["review"].calls[0][1]
        packet = next(json.loads(m.content)["host_evidence"] for m in review_messages if m.id == "omni-evidence:review")
        command = next(op for op in packet["operations"] if op["tool"] == "run_command")
        patch = next(op for op in packet["operations"] if op["tool"] == "workspace_patch")
        assert command["snapshot_scope_valid"] and command["target_versions_match"]
        assert command["exit_code"] == 0 and command["artifacts"][0]["verified"]
        assert patch["payload_digest_matches"] and patch["resulting_source"]["text"] == "print(42)\n"
        assert packet["assessment"] == "UNVERIFIED"  # Host never invents overall PASS.
    if strategy == "compact":
        assert len(stages["plan"].calls) == 1 and not stages["think"].calls and not stages["dispatch"].calls


@pytest.mark.asyncio
async def test_evidence_refuses_changed_snapshot_and_cross_run_ids(environment, tmp_path):
    worker, run_id, workspace, broker, sandbox, _ = prepare_v4(environment, tmp_path)
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_v4(worker, run_id, workspace, broker, sandbox, InMemorySaver(), strategy="single")
        try:
            await graph.ainvoke({"messages": [("user", "修复并测试")]}, {"configurable": {"thread_id": run_id}})
        finally:
            await scheduler.aclose()
    from omniharness.models import WorkspaceSnapshot
    with worker.sessions.begin() as db:
        op = db.scalar(select(ToolOperation).where(ToolOperation.tool_name == "run_command"))
        snapshot = db.get(WorkspaceSnapshot, op.snapshot_id)
        snapshot.manifest = {**snapshot.manifest, "files": []}
        store = ArtifactStore(worker.settings.data_dir / "artifacts")
        packet = build_review_evidence(db, db.get(Run, run_id), store)
        command = next(x for x in packet["operations"] if x["tool"] == "run_command")
        assert not command["target_versions_match"] and command["evidence_status"] == "UNVERIFIED"
        assert build_review_evidence(db, db.get(Run, run_id), store, operation_ids=["foreign-operation"])["operations"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("paths", [[], ["other.py"]])
async def test_task_write_boundary_cannot_expand_workspace_grant(environment, tmp_path, paths):
    worker, run_id, workspace, broker, sandbox, project = prepare_v4(environment, tmp_path)
    stages = {role: InspectStageModel(role) for role in STAGE_KEYS}
    stages["plan"].plan_paths = paths
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_v4(worker, run_id, workspace, broker, sandbox, InMemorySaver(), stages=stages)
        try:
            await graph.ainvoke({"messages": [("user", "修复并测试")]}, {"configurable": {"thread_id": run_id}})
        finally:
            await scheduler.aclose()
    assert "patch" not in broker.calls
    assert (project / "main.py").read_text() == "print(0)\n"


class ParallelPlanModel(InspectStageModel):
    task_paths: list = []

    async def ainvoke(self, messages):
        if self.stage != "plan":
            return await super().ainvoke(messages)
        return AIMessage(content=json.dumps({"constraints": ["keep API"], "steps": [
            {"id": f"s{i}", "task": f"task{i}", "acceptance": f"accept{i}", "write_paths": paths}
            for i, paths in enumerate(self.task_paths)]}))


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes,expected", [([["a.py"], ["b.py"]], 2),
    ([["main.py"], ["MAIN.py"]], 1), ([None, ["b.py"]], 1)])
async def test_graph_projects_only_own_task_and_parallelizes_safe_scopes(environment, tmp_path, scopes, expected):
    worker, run_id, *_ = prepare_v4(environment, tmp_path)
    scheduler = HierarchicalDelegation()
    stages = {role: ParallelPlanModel(role) for role in STAGE_KEYS}
    stages["plan"].task_paths = scopes

    class RecordingExecutor:
        active = peak = 0
        inputs = []

        async def ainvoke(self, payload, config):
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.inputs.append(payload["messages"])
            await asyncio.sleep(0.05)
            self.active -= 1
            return {"messages": [*payload["messages"], AIMessage(content="independent result")]}

    executor = RecordingExecutor()
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        host = M4AgentHost(worker, run_id, COLLAB_WORKFLOW_VERSION, role_versions=COLLAB_STAGE_VERSIONS,
            plugin_versions=M4_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
        host.scheduler = scheduler
        graph = build_stage_graph(models=stages, hooks=host, checkpointer=InMemorySaver(),
            workflow_version=COLLAB_WORKFLOW_VERSION, execution_agent=executor, execution_strategy="compact")
        try:
            await graph.ainvoke({"messages": [("user", "修复并测试")]}, {"configurable": {"thread_id": run_id}})
        finally:
            await scheduler.aclose()
    assert executor.peak == expected and len(executor.inputs) == 2
    for messages in executor.inputs:
        assert len(messages) == 2
        task = json.loads(messages[-1].content)
        assert task["task"]["acceptance"].startswith("accept")
        assert task["constraints"] == ["keep API"]
        assert task["dependencies"] == []
        assert all(not isinstance(message, AIMessage) for message in messages)


class ScopedToolModel(ToolModel):
    seen_tasks: list = []

    def _generate(self, messages, **kwargs):
        task_messages = [m for m in messages if m.id and m.id.startswith("omni-task:")]
        self.seen_tasks.append([(m.id, json.loads(m.content)["task"]["write_paths"]) for m in task_messages])
        task = json.loads(task_messages[0].content)["task"]
        path = task["write_paths"][0]
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            name, arguments = "workspace_read", {"path": path}
        elif len(results) == 1:
            name, arguments = "workspace_patch", {"path": path, "content": "print(42)\n",
                "expected_sha256": json.loads(results[0].content)["sha256"]}
        elif len(results) == 2:
            name, arguments = "run_command", {"command": ["python", path]}
        else:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="scoped task finished"))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
            "id": f"same-provider-call-id-{len(results)}", "name": name, "args": arguments}]))])


@pytest.mark.asyncio
async def test_parallel_real_tool_graphs_keep_task_scopes_and_operation_identities_separate(environment, tmp_path, monkeypatch):
    worker, run_id, workspace, broker, sandbox, project = prepare_v4(environment, tmp_path)
    for name in ("a.py", "b.py"):
        (project / name).write_text("print(0)\n")
    stages = {role: ParallelPlanModel(role) for role in STAGE_KEYS}
    stages["plan"].task_paths = [["a.py"], ["b.py"]]
    model = ScopedToolModel()
    from omniharness.m4_runtime import ExecutionBoundary
    original = ExecutionBoundary.abefore_model
    boundaries = []

    async def capture(self, state, runtime):
        boundaries.append((runtime.execution_info.checkpoint_ns,
                           [m.id for m in state["messages"] if m.id and m.id.startswith("omni-task:")]))
        return await original(self, state, runtime)

    monkeypatch.setattr(ExecutionBoundary, "abefore_model", capture)
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_v4(worker, run_id, workspace, broker, sandbox, InMemorySaver(),
                                   stages=stages, executor_model=model)
        try:
            result = await graph.ainvoke({"messages": [("user", "修复并测试")]},
                {"configurable": {"thread_id": run_id}, "recursion_limit": 100}, durability="sync")
        finally:
            await scheduler.aclose()
    assert all(len(tasks) == 1 for tasks in model.seen_tasks), boundaries
    assert len({namespace.rsplit("|", 1)[0] for namespace, _ in boundaries}) == 2
    assert all((project / name).read_text() == "print(42)\n" for name in ("a.py", "b.py")), model.seen_tasks
    assert len(result["task_results"]) == 2
    with worker.sessions() as db:
        operations = list(db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)))
        assert len(operations) == 6 and len({op.id for op in operations}) == 6
        for task, path in (("t1", "a.py"), ("t2", "b.py")):
            owned = [op for op in operations if op.result["host_task_id"] == task]
            assert len(owned) == 3
            assert all(op.parameters.get("path", path) == path for op in owned)
            refs = next(item["operation_ids"] for item in result["task_results"] if item["task_id"] == task)
            assert set(refs) == {op.id for op in owned}


@pytest.mark.asyncio
async def test_review_evidence_detects_artifact_tampering_and_enforces_packet_budget(environment, tmp_path):
    worker, run_id, workspace, broker, sandbox, _ = prepare_v4(environment, tmp_path)
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_v4(worker, run_id, workspace, broker, sandbox, InMemorySaver(), strategy="single")
        try:
            await graph.ainvoke({"messages": [("user", "修复并测试")]}, {"configurable": {"thread_id": run_id}})
        finally:
            await scheduler.aclose()
    from omniharness.models import ArtifactVersion
    with worker.sessions.begin() as db:
        artifact = db.scalar(select(ArtifactVersion).where(ArtifactVersion.run_id == run_id,
                                                          ArtifactVersion.kind == "command_log"))
        store = ArtifactStore(worker.settings.data_dir / "artifacts")
        (store.root / artifact.id).write_text("changed test evidence")
        packet = build_review_evidence(db, db.get(Run, run_id), store)
        command = next(x for x in packet["operations"] if x["tool"] == "run_command")
        assert command["evidence_status"] == "UNVERIFIED"
        assert any(a.get("error") == "EVIDENCE_UNAVAILABLE" for a in command["artifacts"])
        assert all(a.get("excerpt", {}).get("text") != "changed test evidence" for a in command["artifacts"])
        small = build_review_evidence(db, db.get(Run, run_id), store, max_characters=1200)
        assert len(json.dumps(small, ensure_ascii=False)) <= 1200 and small["omitted_operations"] > 0


@pytest.mark.asyncio
async def test_v4_actual_model_guard_uses_frozen_context_v2_counter(environment, tmp_path, monkeypatch):
    worker, run_id, workspace, broker, sandbox, _ = prepare_v4(environment, tmp_path)
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        roles = {name: {**role, "model": {**role["model"], "context_policy": {
            "schema_version": 1, "policy": {"version": "context-v2"}}}}
                 for name, role in run.config_snapshot["agent"]["roles"].items()}
        run.config_snapshot = {**run.config_snapshot, "agent": {**run.config_snapshot["agent"], "roles": roles}}
    import omniharness.context_policy as policies
    original = policies.counter_for_policy
    observed = []

    def counter(policy):
        observed.append(policy.version)
        return original(policy)

    monkeypatch.setattr(policies, "counter_for_policy", counter)
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_v4(worker, run_id, workspace, broker, sandbox, InMemorySaver(), strategy="compact")
        try:
            await graph.ainvoke({"messages": [("user", "修复并测试")]}, {"configurable": {"thread_id": run_id}})
        finally:
            await scheduler.aclose()
    assert observed and set(observed) == {"context-v2"}
