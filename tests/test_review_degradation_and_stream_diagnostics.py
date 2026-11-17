"""Offline regressions for stream integrity diagnostics, review degradation, workspace stress fixes and evaluation metrics."""
from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_compatible_native_stream import frame, model, response, usage
from test_m4_runtime import ToolModel, prepare

from omniharness.artifacts import ArtifactStore
from omniharness.m3_agents import AgentExternalCallFailed, _raise_sanitized
from omniharness.m3_delegation import HierarchicalDelegation
from omniharness.m3_host import AgentCancellationRequested, AgentOperationNeedsReview
from omniharness.m3_stages import StageOutputError, build_stage_graph
from omniharness.m3_worker import _complete_checkpoint, _release_terminal_run
from omniharness.m4_contract import (
    COLLAB_WORKFLOW_VERSION,
    M4_PLUGIN_VERSIONS,
    workspace_role_versions,
)
from omniharness.m4_runtime import M4AgentHost, build_execution_agent
from omniharness.model_output import require_complete_output
from omniharness.models import (
    ArtifactVersion,
    Message,
    OperationRecord,
    Run,
    RunBudget,
    RunEvent,
    ToolOperation,
    WorkspaceLease,
)
from omniharness.providers import ProviderError
from omniharness.request_diagnostics import failure_diagnostic
from omniharness.services import cancel_run, freeze_stage_run_config, run_output
from omniharness.settings import Settings
from omniharness.stage_contract import STAGE_KEYS, STAGE_ROLE_BINDINGS
from omniharness.worker import WorkerLock
from omniharness.workspace_io import DictWorkspaceRegistry, WorkspaceError, WorkspaceGrant, WorkspaceIO

CANARY = "private-review-stream-canary"

SPEC = importlib.util.spec_from_file_location("m4_evaluate", Path(__file__).parents[1] / "scripts/m4_evaluate.py")
EVALUATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALUATION)


class ConfigurableReviewModel(BaseChatModel):
    stage: str
    review_behavior: str = "valid"

    def __init__(self, stage: str, *, review_behavior: str = "valid", calls: list | None = None):
        super().__init__(stage=stage, review_behavior=review_behavior)
        object.__setattr__(self, "_calls", calls if calls is not None else [])

    @property
    def _llm_type(self):
        return "configurable-stage-model"

    def _generate(self, messages, **kwargs):
        raise NotImplementedError

    async def ainvoke(self, messages):
        self._calls.append(self.stage)
        if self.stage != "review":
            replies = {
                "think": "修改指定项目并验证。",
                "plan": json.dumps({
                    "goal": "输出42",
                    "constraints": ["只改main.py"],
                    "steps": [{"id": "s1", "task": "修复", "acceptance": "输出42", "depends_on": []}],
                }),
                "dispatch": '{"tasks":[{"id":"t1","step_id":"s1","instruction":"修复并测试","depends_on":[]}]}',
            }
            return AIMessage(content=replies[self.stage])
        if self.review_behavior == "markdown":
            return AIMessage(content="### 审查通过\n代码逻辑正确，但本段是 Markdown 而非 JSON。")
        if self.review_behavior == "truncated":
            return AIMessage(content='{"summary":"未写完的审查', response_metadata={"finish_reason": "length"})
        if self.review_behavior == "network_timeout":
            raise httpx.ReadTimeout(CANARY)
        return AIMessage(content='{"summary":"依据记录审查通过","findings":[],"unverified":[]}')


@pytest.mark.parametrize("finish,expected_code", [
    ("end_turn", None),
    ("stop_sequence", None),
    ("max_tokens", "OUTPUT_TRUNCATED"),
    ("MAX_TOKENS", "OUTPUT_TRUNCATED"),
    ("SAFETY", "OUTPUT_FILTERED"),
])
@pytest.mark.asyncio
async def test_native_stream_accepts_compatible_finish_synonyms_and_preserves_message(finish, expected_code):
    rows = [frame({"role": "assistant", "content": "streamed text"}), frame(finish=finish), frame(counts=usage())]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(rows))) as client:
        answer = await model(client).ainvoke("offline")
    assert answer.content == "streamed text"
    if expected_code is None:
        assert require_complete_output(answer) == "streamed text"
    else:
        with pytest.raises(ProviderError) as caught:
            require_complete_output(answer)
        assert caught.value.code == expected_code


@pytest.mark.parametrize("frames,expected_reason", [
    ([frame({"role": "assistant", "content": "partial"}), frame(counts=usage())], "missing_finish"),
    ([frame({"role": "assistant", "content": "partial"}), frame(finish="unrecognized")], "invalid_finish"),
    ([frame({"role": "assistant", "content": "partial"}), frame(finish="stop"), frame(finish="length")], "conflicting_finish"),
])
@pytest.mark.asyncio
async def test_native_stream_incomplete_subreason_survives_sanitized_host_diagnostics(frames, expected_reason):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(frames))) as client:
        try:
            await model(client).ainvoke("offline")
        except ProviderError as raw_exc:
            assert raw_exc.code == "OUTPUT_INCOMPLETE"
            assert getattr(raw_exc, "omni_incomplete_reason", None) == expected_reason
            try:
                _raise_sanitized(raw_exc)
            except AgentExternalCallFailed as sanitized:
                diag = failure_diagnostic(sanitized, "compatible-native-stream-v1")
            else:
                pytest.fail("Expected AgentExternalCallFailed")
        else:
            pytest.fail("Expected ProviderError")
    assert diag["error_code"] == "OUTPUT_INCOMPLETE"
    assert diag["exception_category"] == "output_integrity"
    assert diag["incomplete_reason"] == expected_reason
    assert CANARY not in json.dumps(diag)


def test_unallowlisted_provider_error_code_stays_redacted():
    secret_error = ProviderError(CANARY, CANARY)
    diag = failure_diagnostic(secret_error, "ainvoke_default")
    assert diag["error_code"] == "PROVIDER_ERROR"
    assert diag["exception_category"] == "unknown"
    assert "incomplete_reason" not in diag
    assert CANARY not in json.dumps(diag)


def prepare_v4(environment, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    worker, run_id, workspace, broker, sandbox, project = prepare(environment, tmp_path)
    _, _, factory, _ = environment
    v4_roles = workspace_role_versions(COLLAB_WORKFLOW_VERSION)
    with factory.begin() as db:
        run = db.get(Run, run_id)
        snapshot = dict(run.config_snapshot)
        agent = dict(snapshot["agent"])
        agent["workflow_version"] = COLLAB_WORKFLOW_VERSION
        agent["strategy"] = "compact"
        roles = {k: {**v, "role_version": v4_roles[k]} for k, v in agent["roles"].items()}
        agent["roles"] = roles
        snapshot["agent"] = agent
        run.config_snapshot = snapshot
        run.workflow_version = COLLAB_WORKFLOW_VERSION
    return worker, run_id, workspace, broker, sandbox, project, v4_roles


@pytest.mark.parametrize("behavior,expected_status,expects_artifact", [
    ("markdown", "INVALID_OUTPUT", True),
    ("truncated", "INCOMPLETE", True),
    ("network_timeout", "UNKNOWN", False),
    ("budget", "NOT_RUN_BUDGET", False),
    ("valid", "COMPLETED", False),
])
@pytest.mark.asyncio
async def test_deliver_unreviewed_handles_all_four_review_failures_and_normal_completion(
    environment, tmp_path, behavior, expected_status, expects_artifact,
):
    worker, run_id, workspace, broker, sandbox, project, v4_roles = prepare_v4(environment, tmp_path)
    _, _, factory, _ = environment
    if behavior == "budget":
        with factory.begin() as db:
            budget = db.get(RunBudget, run_id)
            budget.max_model_calls = 5  # compact: plan(1) + execute(4 tool rounds) = 5, leaving 0 for review.
    calls = []
    saver = InMemorySaver()
    with WorkerLock(worker.engine, worker.settings) as lock:
        worker.lock = lock
        host = M4AgentHost(worker, run_id, COLLAB_WORKFLOW_VERSION, role_versions=v4_roles,
                           plugin_versions=M4_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
        scheduler = HierarchicalDelegation()
        executor = build_execution_agent(
            model=ToolModel(), host=host, workspace=workspace, broker=broker, sandbox=sandbox,
            artifacts=ArtifactStore(worker.settings.data_dir / "artifacts"), scheduler=scheduler,
        )
        models = {stage: ConfigurableReviewModel(stage, review_behavior=behavior, calls=calls) for stage in STAGE_KEYS}
        graph = build_stage_graph(
            models=models, hooks=host, checkpointer=saver, workflow_version=COLLAB_WORKFLOW_VERSION,
            execution_agent=executor, execution_strategy="compact", review_failure_policy="deliver_unreviewed",
        )
        state = await graph.ainvoke(
            {"messages": [AIMessage(content="prior"), ("user", "修复并测试")]},
            {"configurable": {"thread_id": run_id}},
        )
        _complete_checkpoint(worker, run_id, state, started=None)
        _release_terminal_run(worker, run_id)

    assert (project / "main.py").read_text() == "print(42)\n"
    with factory() as db:
        run = db.get(Run, run_id)
        assistant = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
        projected = run_output(run)
        events = [e.event_type for e in db.scalars(select(RunEvent).where(RunEvent.run_id == run_id))]
        raw_artifacts = list(db.scalars(select(ArtifactVersion).where(
            ArtifactVersion.run_id == run_id, ArtifactVersion.kind == "review_raw_output")))
        review_ops = list(db.scalars(select(OperationRecord).where(
            OperationRecord.run_id == run_id, OperationRecord.node_id == "agent:review:model_calls")))
        lease = db.get(WorkspaceLease, workspace["id"])

    assert projected["status"] == "completed"
    assert projected["review_status"] == expected_status
    assert lease is None
    if expected_status == "COMPLETED":
        assert "独立审查：" in assistant.content
        assert "review_status" not in (assistant.output_metadata or {})
        assert "agent.review_degraded" not in events
    else:
        assert "独立审查未完成：" in assistant.content
        assert expected_status in assistant.content
        assert assistant.output_metadata["review_status"] == expected_status
        assert assistant.output_metadata["output_status"] == "complete"
        assert "agent.review_degraded" in events
    assert (len(raw_artifacts) == 1) is expects_artifact
    if behavior == "network_timeout":
        assert calls.count("review") == 1
        assert len(review_ops) == 1 and review_ops[0].status == "uncertain"
        assert CANARY not in json.dumps(review_ops[0].result)
    if behavior == "budget":
        assert calls.count("review") == 0
        assert review_ops == []


@pytest.mark.asyncio
async def test_review_failure_does_not_degrade_when_policy_disabled_or_tool_uncertain_or_cancelled(
    environment, tmp_path,
):
    # 1. Default policy ('fail') keeps StageOutputError on invalid review format.
    worker, run_id, workspace, broker, sandbox, _, v4_roles = prepare_v4(environment, tmp_path / "default-fail")
    with WorkerLock(worker.engine, worker.settings) as lock:
        worker.lock = lock
        host = M4AgentHost(worker, run_id, COLLAB_WORKFLOW_VERSION, role_versions=v4_roles,
                           plugin_versions=M4_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
        executor = build_execution_agent(
            model=ToolModel(), host=host, workspace=workspace, broker=broker, sandbox=sandbox,
            artifacts=ArtifactStore(worker.settings.data_dir / "artifacts"), scheduler=HierarchicalDelegation(),
        )
        graph = build_stage_graph(
            models={s: ConfigurableReviewModel(s, review_behavior="markdown") for s in STAGE_KEYS},
            hooks=host, checkpointer=InMemorySaver(), workflow_version=COLLAB_WORKFLOW_VERSION,
            execution_agent=executor, execution_strategy="compact",
        )
        with pytest.raises(StageOutputError):
            await graph.ainvoke({"messages": [("user", "修复并测试")]}, {"configurable": {"thread_id": run_id}})

    # 2. Uncertain tool slot blocks degradation even when deliver_unreviewed is enabled.
    worker2, run_id2, workspace2, broker2, sandbox2, _, v4_roles2 = prepare_v4(environment, tmp_path / "uncertain-tool")
    _, _, factory, _ = environment
    with factory.begin() as db:
        db.add(ToolOperation(
            id="uncertain-tool-op", run_id=run_id2, task_id="t1",
            tool_call_id="call-x", tool_name="run_command", tool_version="builtin-workspace-v1",
            parameters={"command": "pytest"}, binding_fingerprint="f" * 64, status="uncertain", slot_claimed=True,
        ))
    with WorkerLock(worker2.engine, worker2.settings) as lock:
        worker2.lock = lock
        host2 = M4AgentHost(worker2, run_id2, COLLAB_WORKFLOW_VERSION, role_versions=v4_roles2,
                            plugin_versions=M4_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
        executor2 = build_execution_agent(
            model=ToolModel(), host=host2, workspace=workspace2, broker=broker2, sandbox=sandbox2,
            artifacts=ArtifactStore(worker2.settings.data_dir / "artifacts"), scheduler=HierarchicalDelegation(),
        )
        graph2 = build_stage_graph(
            models={s: ConfigurableReviewModel(s, review_behavior="markdown") for s in STAGE_KEYS},
            hooks=host2, checkpointer=InMemorySaver(), workflow_version=COLLAB_WORKFLOW_VERSION,
            execution_agent=executor2, execution_strategy="compact", review_failure_policy="deliver_unreviewed",
        )
        with pytest.raises(StageOutputError):
            await graph2.ainvoke({"messages": [("user", "修复并测试")]}, {"configurable": {"thread_id": run_id2}})

    # 3. Cancellation requested during review failure is never swallowed into completed.
    worker3, run_id3, workspace3, broker3, sandbox3, _, v4_roles3 = prepare_v4(environment, tmp_path / "cancel-wins")

    class CancellingReviewModel(ConfigurableReviewModel):
        async def ainvoke(self, messages):
            if self.stage == "review":
                with factory.begin() as db:
                    cancel_run(db, run_id3)
                raise httpx.ReadTimeout(CANARY)
            return await super().ainvoke(messages)

    with WorkerLock(worker3.engine, worker3.settings) as lock:
        worker3.lock = lock
        host3 = M4AgentHost(worker3, run_id3, COLLAB_WORKFLOW_VERSION, role_versions=v4_roles3,
                            plugin_versions=M4_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
        executor3 = build_execution_agent(
            model=ToolModel(), host=host3, workspace=workspace3, broker=broker3, sandbox=sandbox3,
            artifacts=ArtifactStore(worker3.settings.data_dir / "artifacts"), scheduler=HierarchicalDelegation(),
        )
        graph3 = build_stage_graph(
            models={s: CancellingReviewModel(s) for s in STAGE_KEYS},
            hooks=host3, checkpointer=InMemorySaver(), workflow_version=COLLAB_WORKFLOW_VERSION,
            execution_agent=executor3, execution_strategy="compact", review_failure_policy="deliver_unreviewed",
        )
        with pytest.raises(AgentCancellationRequested):
            await graph3.ainvoke({"messages": [("user", "修复并测试")]}, {"configurable": {"thread_id": run_id3}})


@pytest.mark.asyncio
async def test_crash_window_reentry_stops_for_review_without_duplicate_call_or_degradation(environment, tmp_path):
    worker, run_id, workspace, broker, sandbox, _, v4_roles = prepare_v4(environment, tmp_path)
    saver = InMemorySaver()
    calls = []

    class CrashAfterUncertainModel(ConfigurableReviewModel):
        async def ainvoke(self, messages):
            if self.stage == "review":
                calls.append("review")
                raise httpx.ReadTimeout(CANARY)
            return await super().ainvoke(messages)

    with WorkerLock(worker.engine, worker.settings) as lock:
        worker.lock = lock
        host = M4AgentHost(worker, run_id, COLLAB_WORKFLOW_VERSION, role_versions=v4_roles,
                           plugin_versions=M4_PLUGIN_VERSIONS, role_bindings=STAGE_ROLE_BINDINGS)
        executor = build_execution_agent(
            model=ToolModel(), host=host, workspace=workspace, broker=broker, sandbox=sandbox,
            artifacts=ArtifactStore(worker.settings.data_dir / "artifacts"), scheduler=HierarchicalDelegation(),
        )
        # First pass without degradation simulates crash after review OperationRecord became uncertain
        # and before call_review committed a checkpointed output.
        first_graph = build_stage_graph(
            models={s: CrashAfterUncertainModel(s) for s in STAGE_KEYS},
            hooks=host, checkpointer=saver, workflow_version=COLLAB_WORKFLOW_VERSION,
            execution_agent=executor, execution_strategy="compact", review_failure_policy="fail",
        )
        with pytest.raises(AgentExternalCallFailed):
            await first_graph.ainvoke({"messages": [("user", "修复并测试")]}, {"configurable": {"thread_id": run_id}})
        assert calls == ["review"]

        # Re-entering call_review even with deliver_unreviewed enabled must hit the uncertain reservation
        # and raise AgentOperationNeedsReview rather than calling the model again or fabricating completion.
        resumed_graph = build_stage_graph(
            models={s: CrashAfterUncertainModel(s) for s in STAGE_KEYS},
            hooks=host, checkpointer=saver, workflow_version=COLLAB_WORKFLOW_VERSION,
            execution_agent=executor, execution_strategy="compact", review_failure_policy="deliver_unreviewed",
        )
        with pytest.raises(AgentOperationNeedsReview):
            await resumed_graph.ainvoke(None, {"configurable": {"thread_id": run_id}})
        assert calls == ["review"]


def test_settings_freeze_review_failure_policy_only_when_enabled(environment):
    settings, _, factory, client = environment
    from test_m3_agent_config import model as create_model

    chosen = create_model(client, "freeze-policy", url="http://localhost:11434/v1", key=None)
    conversation = client.post("/api/v1/conversations", json={}).json()
    from omniharness.schemas import SendMessageRequest
    from omniharness.services import enqueue

    opt_in_settings = Settings(
        database_url=settings.database_url, allow_sqlite=True, enable_agent=True,
        data_dir=settings.data_dir, secret_dir=settings.secret_dir, static_dir=settings.static_dir,
        review_failure_policy="deliver_unreviewed",
    )
    with factory.begin() as db:
        first = enqueue(db, conversation["id"], SendMessageRequest(
            content="a", model_config_id=chosen["id"], client_request_id="req-default"))
        run_default = db.get(Run, first["run_id"])
        run_default.config_snapshot = {**run_default.config_snapshot, "workflow_type": "agent"}
        freeze_stage_run_config(db, run_default, settings)
        assert "review_failure_policy" not in run_default.config_snapshot["agent"]
        assert run_output(run_default)["execution_config"]["review_failure_policy"] == "fail"

        second = enqueue(db, conversation["id"], SendMessageRequest(
            content="b", model_config_id=chosen["id"], client_request_id="req-degrade"))
        run_opt_in = db.get(Run, second["run_id"])
        run_opt_in.config_snapshot = {**run_opt_in.config_snapshot, "workflow_type": "agent"}
        freeze_stage_run_config(db, run_opt_in, opt_in_settings)
        assert run_opt_in.config_snapshot["agent"]["review_failure_policy"] == "deliver_unreviewed"
        assert run_output(run_opt_in)["execution_config"]["review_failure_policy"] == "deliver_unreviewed"


@pytest.mark.parametrize("bad_path", [
    "COM\u00b9", "COM\u00b2.txt", "src/LPT\u00b3.py", "CONIN$", "CONOUT$.log", "NUL .txt", "src/com\u00b9 .md",
])
def test_workspace_rejects_superscript_and_spaced_windows_device_names(tmp_path, bad_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    registry = DictWorkspaceRegistry([WorkspaceGrant("ws", "local", str(root), write_allow=(".",))])
    io = WorkspaceIO(registry, tmp_path / "storage", protected_roots=(), require_write_lease=lambda *_: None)
    with pytest.raises(WorkspaceError) as exc:
        io.read("ws", "local", 1, bad_path)
    assert exc.value.code == "invalid_path"


def test_parallel_workspace_instances_same_operation_id_always_raise_workspace_error(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    storage = tmp_path / "storage"
    registry = DictWorkspaceRegistry([WorkspaceGrant("ws", "local", str(root), write_allow=("src",))])
    instances = [
        WorkspaceIO(registry, storage, protected_roots=(), require_write_lease=lambda *_: None)
        for _ in range(8)
    ]
    barrier = Barrier(8)

    def attempt(index):
        barrier.wait()
        try:
            return instances[index].patch(
                "ws", "local", 1, path="src/item.txt", content=f"v-{index}",
                expected_sha256=None, operation_id="shared-op-id", run_id="run-1",
            )
        except WorkspaceError as exc:
            return {"workspace_error": exc.code}

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))

    assert sum(item.get("state") == "completed" for item in outcomes) == 1
    assert sum(item.get("workspace_error") == "operation_exists" for item in outcomes) == 7


def test_evaluation_separates_delivered_unreviewed_from_resolved_and_supports_context_v3():
    unreviewed = {
        "split": "formal", "condition": "Compact-Mixed", "task_id": "task-1", "repetition": 1,
        "model_calls": 6, "acceptance": {"status": "PASS"}, "run_status": "completed",
        "review_status": "UNKNOWN", "delivered_unreviewed": True, "resolved": False,
        "latency_seconds": 40, "cost_estimate": {"reference_cost": "0.8"},
    }
    reviewed = {
        "split": "formal", "condition": "Single-Pro", "task_id": "task-1", "repetition": 1,
        "model_calls": 5, "acceptance": {"status": "PASS"}, "run_status": "completed",
        "review_status": None, "delivered_unreviewed": False, "resolved": True,
        "latency_seconds": 60, "cost_estimate": {"reference_cost": "1.2"},
    }
    summary = EVALUATION.summarize_optimization([reviewed, unreviewed])
    mixed = summary["conditions"]["Compact-Mixed"]
    assert mixed["workflow_completed"] == 1
    assert mixed["code_passed"] == 1
    assert mixed["delivered_unreviewed"] == 1
    assert mixed["resolved"] == 0

    v3_policy = {"schema_version": 1, "policy": {"version": "context-v3"}}
    models = {
        "strong": {"id": "strong", "version": 1, "context_policy": v3_policy, "capabilities": {"tools": {"status": "supported"}}},
        "cheap": {"id": "cheap", "version": 1, "context_policy": v3_policy, "capabilities": {"tools": {"status": "supported"}}},
    }
    stages = {
        stage: {"id": "cheap" if stage == "execute" else "strong", "version": 1,
                "max_output_tokens": 100000, "context_policy": v3_policy, "transport": "adapter-default"}
        for stage in EVALUATION.STAGES
    }
    record = {
        "split": "formal", "condition": "Compact-Mixed", "agent_strategy": "compact",
        "execution_config": {
            "workflow_version": "stage-agent-v4", "models": stages,
            "budget": {"max_model_calls": 50, "max_tool_calls": 100, "max_active_seconds": 1800},
            "sandbox": {"image": "immutable"},
        },
        "tool_policy": {"network": "deny"},
    }
    assert EVALUATION.optimization_fairness(
        [record], frozen_models=models,
        frozen_selection={"strong_model_id": "strong", "economy_model_id": "cheap", "context_policy_version": "context-v3"},
    )["status"] == "PASS"
    assert EVALUATION.optimization_fairness(
        [record], frozen_models=models,
        frozen_selection={"strong_model_id": "strong", "economy_model_id": "cheap"},
    )["status"] == "FAIL"
