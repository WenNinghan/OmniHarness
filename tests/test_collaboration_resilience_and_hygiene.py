"""Regression tests for structured JSON extraction, interactive review degradation,
read-only mid-task clarification, multi-way disjoint batching, compound shell
exit-masking metadata, and public repository sync preflight checks.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from test_m4_runtime import prepare

from omniharness.ai_routing import _parse_response
from omniharness.artifacts import ArtifactStore
from omniharness.m3_delegation import HierarchicalDelegation
from omniharness.m3_stages import StageOutputError, build_stage_graph
from omniharness.m4_collaboration import independent_batch
from omniharness.m4_contract import (
    INTERACTIVE_V9_WORKFLOW_VERSION,
    M4_V9_PLUGIN_VERSIONS,
    workspace_role_versions,
)
from omniharness.m4_runtime import M4AgentHost
from omniharness.models import OperationRecord, Run, ToolOperation, WorkspaceSnapshot
from omniharness.review_evidence import build_review_evidence
from omniharness.shell_command_audit import detect_compound_exit_masking
from omniharness.stage_contract import STAGE_KEYS, STAGE_ROLE_BINDINGS
from omniharness.structured_json import extract_json_payload, supports_stage_json_mode
from omniharness.worker import WorkerLock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_public_repo as pub_sync  # noqa: E402


def test_extract_json_payload_accepts_single_closed_fence_with_prose_and_rejects_ambiguity():
    raw = '{"summary":"ok","findings":[],"unverified":[]}'
    assert extract_json_payload(raw) == raw
    assert extract_json_payload(f"```json\n{raw}\n```") == raw

    wrapped = f"已完成核对，结构化审查结果如下：\n```json\n{raw}\n```\n以上结论仅基于已记录工件。"
    assert extract_json_payload(wrapped) == raw

    tilde_wrapped = f"说明：\n~~~JSON\n{raw}\n~~~\n完毕。"
    assert extract_json_payload(tilde_wrapped) == raw

    # Non-JSON code block alongside a single explicit json block remains unambiguous.
    mixed_lang = f"参考补丁：\n```python\nx = 1\n```\n最终输出：\n```json\n{raw}\n```"
    assert extract_json_payload(mixed_lang) == raw

    # Reject unclosed fences (truncated output).
    with pytest.raises(ValueError, match="Unclosed"):
        extract_json_payload(f"结论如下：\n```json\n{raw}")

    # Reject multiple json blocks (ambiguous).
    with pytest.raises(ValueError, match="unambiguous"):
        extract_json_payload(f"候选一：\n```json\n{raw}\n```\n候选二：\n```json\n{raw}\n```")

    # Reject json + untagged block ambiguity.
    with pytest.raises(ValueError, match="unambiguous"):
        extract_json_payload(f"```json\n{raw}\n```\n```\n{raw}\n```")


def test_ai_routing_parser_accepts_fenced_json_with_preamble_and_blocks_secret_leak():
    stages = [
        {"stage": s, "model_config_id": "m-fast" if s == "execute" else "m-pro", "reason": "按阶段能力匹配"}
        for s in STAGE_KEYS
    ]
    text = "根据任务复杂度分析，推荐分工如下：\n```json\n" + json.dumps({"stages": stages}, ensure_ascii=False) + "\n```"
    parsed = _parse_response(AIMessage(content=text), {"m-pro", "m-fast"}, {"secret-canary-123"})
    assert parsed["execute"].model_config_id == "m-fast"
    assert parsed["review"].model_config_id == "m-pro"


def test_supports_stage_json_mode_requires_explicit_capability_or_parameter():
    from omniharness.providers import (
        ProviderError,
        RuntimeModelConfig,
        summary_capability_binding,
        validate_config,
    )
    from omniharness.request_policy import provider_parameters

    assert supports_stage_json_mode(None) is False
    assert supports_stage_json_mode({"parameters": {}, "capabilities": {}}) is False
    assert supports_stage_json_mode({"capabilities": {"json_mode": {"status": "unsupported"}}}) is False
    assert supports_stage_json_mode({"capabilities": {"json_mode": {"status": "supported"}}}) is True
    assert supports_stage_json_mode({"parameters": {"omni_stage_json_mode": True}}) is True

    base_cfg = RuntimeModelConfig(
        id="m1", adapter_type="openai-compatible", model_name="demo",
        base_url="http://localhost:11434/v1", parameters={"temperature": 0.2},
    )
    json_cfg = RuntimeModelConfig(
        id="m1", adapter_type="openai-compatible", model_name="demo",
        base_url="http://localhost:11434/v1", parameters={"temperature": 0.2, "omni_stage_json_mode": True},
    )
    assert validate_config(json_cfg) == {"temperature": 0.2}
    assert provider_parameters(json_cfg.parameters) == {"temperature": 0.2}
    assert summary_capability_binding(base_cfg) == summary_capability_binding(json_cfg)

    bad_cfg = RuntimeModelConfig(
        id="m1", adapter_type="openai-compatible", model_name="demo",
        base_url="http://localhost:11434/v1", parameters={"omni_stage_json_mode": "yes"},
    )
    with pytest.raises(ProviderError):
        validate_config(bad_cfg)


def test_stage_graph_binds_json_object_response_format_only_for_structured_stages(environment, tmp_path):
    worker, run_id, _, _, _, _ = prepare(environment, tmp_path)
    _, _, factory, _ = environment
    with factory.begin() as db:
        run = db.get(Run, run_id)
        snap = dict(run.config_snapshot)
        agent = dict(snap["agent"])
        roles = {}
        for k, v in agent["roles"].items():
            model_cfg = {**v["model"], "capabilities": {"json_mode": {"status": "supported"}}}
            roles[k] = {**v, "model": model_cfg}
        agent["roles"] = roles
        snap["agent"] = agent
        run.config_snapshot = snap

    bound_calls: list[tuple[str, dict | None]] = []

    class BindableStageModel:
        def __init__(self, stage: str, bound_kwargs: dict | None = None):
            self.stage = stage
            self.bound_kwargs = bound_kwargs

        def bind(self, **kwargs):
            return BindableStageModel(self.stage, kwargs)

        async def ainvoke(self, messages):
            del messages
            bound_calls.append((self.stage, self.bound_kwargs))
            if self.stage == "think":
                return AIMessage(content="自由文本分析")
            if self.stage == "plan":
                return AIMessage(content='说明：\n```json\n{"steps":[{"id":"s1","task":"a","acceptance":"ok"}]}\n```')
            if self.stage == "dispatch":
                return AIMessage(content='{"tasks":[{"id":"t1","step_id":"s1","instruction":"a"}]}')
            return AIMessage(content='前言\n```json\n{"summary":"通过","findings":[],"unverified":[]}\n```')

    class DummyExecutor:
        async def ainvoke(self, state, config):
            del state, config
            return {"messages": [AIMessage(content="执行完毕")]}

    from omniharness.m4_contract import M4_PLUGIN_VERSIONS, M4_STAGE_VERSIONS, M4_WORKFLOW_VERSION

    async def run_check():
        with WorkerLock(worker.engine, worker.settings) as lock:
            worker.lock = lock
            host = M4AgentHost(
                worker, run_id, M4_WORKFLOW_VERSION,
                role_versions=M4_STAGE_VERSIONS,
                plugin_versions=M4_PLUGIN_VERSIONS,
                role_bindings=STAGE_ROLE_BINDINGS,
            )
            host.scheduler = HierarchicalDelegation()
            graph = build_stage_graph(
                models={s: BindableStageModel(s) for s in STAGE_KEYS},
                hooks=host,
                checkpointer=InMemorySaver(),
                workflow_version=M4_WORKFLOW_VERSION,
                execution_agent=DummyExecutor(),
                execution_strategy="staged",
            )
            out = await graph.ainvoke({"messages": [("user", "检查 JSON 模式绑定")]}, {"configurable": {"thread_id": run_id}})
            assert "通过" in out["review"]

    import asyncio
    asyncio.run(run_check())
    by_stage = dict(bound_calls)
    assert by_stage["think"] is None
    assert by_stage["plan"] == {"response_format": {"type": "json_object"}}
    assert by_stage["dispatch"] == {"response_format": {"type": "json_object"}}
    assert by_stage["review"] == {"response_format": {"type": "json_object"}}


def test_independent_batch_runs_up_to_four_disjoint_tasks_and_skips_overlapping_middle():
    ready = [
        {"id": "t1", "write_paths": ["src/a.py"]},
        {"id": "t2", "write_paths": ["SRC/A.PY"]},  # collides with t1 (case-insensitive)
        {"id": "t3", "write_paths": ["src/b.py"]},
        {"id": "t4", "write_paths": []},            # read-only task is disjoint with everything
        {"id": "t5", "write_paths": ["docs/guide.md"]},
        {"id": "t6", "write_paths": ["tests/test_x.py"]},
    ]
    batch = independent_batch(ready)
    assert [item["id"] for item in batch] == ["t1", "t3", "t4", "t5"]
    assert [item["id"] for item in independent_batch(ready, max_parallel=2)] == ["t1", "t3"]
    assert [item["id"] for item in independent_batch([{"id": "u", "write_paths": None}, *ready])] == ["u"]


def test_detect_compound_exit_masking_and_review_evidence_projection(environment, tmp_path):
    assert detect_compound_exit_masking(["pytest", "-q"]) == ()
    assert detect_compound_exit_masking("pytest -q && ruff check src") == ()
    assert detect_compound_exit_masking("echo 'a; b | c' \"x || y\" >| out.txt # ; | comment") == ()
    assert detect_compound_exit_masking("pytest -q; echo done") == (";",)
    assert detect_compound_exit_masking("pytest -q | tail -n 5") == ("|",)
    assert detect_compound_exit_masking("pytest -q || true") == ("||",)
    assert detect_compound_exit_masking("pytest -q\necho ok") == ("\\n",)

    worker, run_id, workspace, _, _, _ = prepare(environment, tmp_path)
    _, _, factory, _ = environment
    store = ArtifactStore(worker.settings.data_dir / "artifacts")
    with factory.begin() as db:
        run = db.get(Run, run_id)
        snap = WorkspaceSnapshot(
            id="snap-1", workspace_id=workspace["id"], run_id=run_id, task_id="t1",
            policy_version=workspace["policy_version"],
            manifest={"run_id": run_id, "workspace_id": workspace["id"], "snapshot_id": "snap-1", "files": []},
            workdir=str(tmp_path),
        )
        db.add(snap)
        db.add(OperationRecord(
            operation_id="c" * 64, run_id=run_id, node_id="tool:run_command",
            parameters_hash="d" * 64, status="completed",
        ))
        db.flush()
        db.add(ToolOperation(
            id="op-compound", run_id=run_id, operation_record_id="c" * 64, task_id="t1",
            tool_call_id="call-1", tool_name="run_command", tool_version="builtin-workspace-v1",
            parameters={"command": "pytest -q; echo masked", "cwd": "."},
            binding_fingerprint="f" * 64, status="completed", snapshot_id="snap-1",
            result={"exit_code": 0, "host_task_id": "t1"},
        ))
        db.add(ToolOperation(
            id="op-clean", run_id=run_id, operation_record_id="c" * 64, task_id="t1",
            tool_call_id="call-2", tool_name="run_command", tool_version="builtin-workspace-v1",
            parameters={"command": "pytest -q && echo ok", "cwd": "."},
            binding_fingerprint="e" * 64, status="completed", snapshot_id="snap-1",
            result={"exit_code": 0, "host_task_id": "t1"},
        ))
        db.flush()
        packet = build_review_evidence(db, run, store)

    by_op = {item["operation_id"]: item for item in packet["operations"]}
    assert by_op["op-compound"]["compound_exit_masking"] == [";"]
    assert by_op["op-compound"]["exit_code_scope"] == "terminal_segment_only"
    assert "compound_exit_masking" not in by_op["op-clean"]


@pytest.mark.asyncio
async def test_interactive_v9_deliver_unreviewed_and_readonly_midtask_clarification(environment, tmp_path):
    worker, run_id, workspace, broker, sandbox, project = prepare(environment, tmp_path)
    (project / "spec.txt").write_text("ambiguous-spec", encoding="utf-8")
    _, _, factory, _ = environment
    v9_roles = workspace_role_versions(INTERACTIVE_V9_WORKFLOW_VERSION)
    with factory.begin() as db:
        run = db.get(Run, run_id)
        snap = dict(run.config_snapshot)
        agent = dict(snap["agent"])
        agent["workflow_version"] = INTERACTIVE_V9_WORKFLOW_VERSION
        agent["strategy"] = "compact"
        agent["review_failure_policy"] = "deliver_unreviewed"
        agent["plugin_versions"] = M4_V9_PLUGIN_VERSIONS
        agent["roles"] = {k: {**v, "role_version": v9_roles[k]} for k, v in agent["roles"].items()}
        snap["agent"] = agent
        run.config_snapshot = snap
        run.workflow_version = INTERACTIVE_V9_WORKFLOW_VERSION

        # Record a completed mutating tool from an earlier subtask t0 and a completed
        # read-only inspection tool on current subtask t1: neither should block t1's clarification.
        db.add(ToolOperation(
            id="prior-task-patch", run_id=run_id, task_id="lg-0", tool_call_id="c0",
            tool_name="workspace_patch", tool_version="builtin-workspace-v1",
            parameters={"path": "init.txt", "content": "ok", "expected_sha256": None},
            binding_fingerprint="a" * 64, status="completed", slot_claimed=False,
            result={"sha256": "b" * 64, "host_task_id": "t0"},
        ))
        db.add(ToolOperation(
            id="current-task-read", run_id=run_id, task_id="lg-1", tool_call_id="c1",
            tool_name="workspace_read", tool_version="builtin-workspace-v1",
            parameters={"path": "spec.txt", "offset": 0, "limit": 4000},
            binding_fingerprint="c" * 64, status="completed", slot_claimed=False,
            result={"content": "ambiguous-spec", "sha256": "d" * 64, "host_task_id": "t1"},
        ))

    final_prompts: list[str] = []

    class V9StageModel:
        def __init__(self, stage: str):
            self.stage = stage

        async def ainvoke(self, messages):
            last = str(messages[-1].content)
            if "interaction-preflight" in str(getattr(messages[-1], "id", "")) or "omni-interaction:preflight" in str(getattr(messages[-1], "id", "")):
                return AIMessage(content='{"status":"continue","questions":[]}')
            if self.stage == "think":
                if "needs_user" in last and "continue" in last:
                    return AIMessage(content='{"status":"continue","questions":[]}')
                final_prompts.append(last)
                return AIMessage(content="已完成任务，后置独立审查未完成。")
            if self.stage == "plan":
                return AIMessage(content='{"goal":"核对规范","constraints":[],"steps":[{"id":"s1","task":"读取并完成","acceptance":"准确","write_paths":["out.txt"]}]}')
            if self.stage == "review":
                # Deliberately return invalid non-JSON prose to trigger deliver_unreviewed in v9.
                return AIMessage(content="代码语义正确，但未输出 JSON 对象。")
            return AIMessage(content="ok")

    class ClarifyingExecutor:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, state, config):
            del state, config
            self.calls += 1
            if self.calls == 1:
                return {"messages": [AIMessage(content='{"status":"needs_user","content":"已只读检查 spec.txt","questions":[{"question":"采用严格模式吗？","reason":"规范未指明","options":["严格","宽松"]}]}')]}
            return {"messages": [AIMessage(content='{"status":"completed","content":"已按严格模式完成","questions":[]}')]}

    saver = InMemorySaver()
    with WorkerLock(worker.engine, worker.settings) as lock:
        worker.lock = lock
        host = M4AgentHost(
            worker, run_id, INTERACTIVE_V9_WORKFLOW_VERSION,
            role_versions=v9_roles,
            plugin_versions=M4_V9_PLUGIN_VERSIONS,
            role_bindings=STAGE_ROLE_BINDINGS,
        )
        host.scheduler = HierarchicalDelegation()
        graph = build_stage_graph(
            models={s: V9StageModel(s) for s in STAGE_KEYS},
            hooks=host,
            checkpointer=saver,
            workflow_version=INTERACTIVE_V9_WORKFLOW_VERSION,
            execution_agent=ClarifyingExecutor(),
            execution_strategy="compact",
            review_failure_policy="deliver_unreviewed",
        )
        cfg = {"configurable": {"thread_id": run_id}}
        paused = await graph.ainvoke({"messages": [HumanMessage(content="按 spec.txt 实现", id="u1")]}, cfg, durability="sync")
        assert "__interrupt__" in paused
        intr = paused["__interrupt__"][0]
        q_id = intr.value["clarification_id"]

        from omniharness.models import Clarification
        with factory.begin() as db:
            q_row = db.get(Clarification, q_id)
            q_row.status = "answered"
            q_row.answer = "严格"

        finished = await graph.ainvoke(
            Command(resume={intr.id: {"clarification_id": q_id, "answer": "严格"}}),
            cfg,
            durability="sync",
        )
        assert finished["review_status"] == "INVALID_OUTPUT"
        assert any("review_status=INVALID_OUTPUT" in p and "严禁声称独立审查已完成或通过" in p for p in final_prompts)

    # Now verify that if current subtask t1 HAS executed workspace_patch, mid-task clarification IS blocked!
    mutated_root = tmp_path / "mutated"
    mutated_root.mkdir(parents=True, exist_ok=True)
    worker2, run_id2, _, _, _, _ = prepare(environment, mutated_root)
    with factory.begin() as db:
        run2 = db.get(Run, run_id2)
        snap2 = dict(run2.config_snapshot)
        agent2 = dict(snap2["agent"])
        agent2["workflow_version"] = INTERACTIVE_V9_WORKFLOW_VERSION
        agent2["strategy"] = "compact"
        agent2["plugin_versions"] = M4_V9_PLUGIN_VERSIONS
        agent2["roles"] = {k: {**v, "role_version": v9_roles[k]} for k, v in agent2["roles"].items()}
        snap2["agent"] = agent2
        run2.config_snapshot = snap2
        run2.workflow_version = INTERACTIVE_V9_WORKFLOW_VERSION
        db.add(ToolOperation(
            id="current-task-patch", run_id=run_id2, task_id="lg-2", tool_call_id="c2",
            tool_name="workspace_patch", tool_version="builtin-workspace-v1",
            parameters={"path": "out.txt", "content": "mutated", "expected_sha256": None},
            binding_fingerprint="e" * 64, status="completed", slot_claimed=False,
            result={"sha256": "f" * 64, "host_task_id": "t1"},
        ))

    with WorkerLock(worker2.engine, worker2.settings) as lock2:
        worker2.lock = lock2
        host2 = M4AgentHost(
            worker2, run_id2, INTERACTIVE_V9_WORKFLOW_VERSION,
            role_versions=v9_roles,
            plugin_versions=M4_V9_PLUGIN_VERSIONS,
            role_bindings=STAGE_ROLE_BINDINGS,
        )
        host2.scheduler = HierarchicalDelegation()
        graph2 = build_stage_graph(
            models={s: V9StageModel(s) for s in STAGE_KEYS},
            hooks=host2,
            checkpointer=InMemorySaver(),
            workflow_version=INTERACTIVE_V9_WORKFLOW_VERSION,
            execution_agent=ClarifyingExecutor(),
            execution_strategy="compact",
        )
        with pytest.raises(StageOutputError) as exc_info:
            await graph2.ainvoke({"messages": [HumanMessage(content="测试写操作后澄清拦截", id="u2")]}, {"configurable": {"thread_id": run_id2}})
        assert exc_info.value.code == "CLARIFICATION_REQUIRES_REVIEW"


def test_public_sync_preflight_scanner_catches_leaks_and_verifies_ast():
    pub_sync.verify_python_ast_equivalence("x = 1\n", "# comment\nx = 1\r\n", label="demo.py")
    with pytest.raises(ValueError, match="AST mismatch"):
        pub_sync.verify_python_ast_equivalence("x = 1\n", "x = 2\n", label="demo.py")

    assert pub_sync.check_forbidden_tree_paths(["src/omniharness/api.py", "README.md"]) == []
    assert pub_sync.check_forbidden_tree_paths([".ai-governance/goal.md", ".env"]) == [
        ".ai-governance/goal.md", ".env",
    ]

    clean_diff = (
        "+++ b/.github/workflows/ci.yml\n"
        "+      OMNI_TEST_POSTGRES_URL: postgresql+psycopg://postgres@127.0.0.1:5432/omniharness_test\n"
    )
    assert pub_sync.scan_added_diff_lines(clean_diff) == []

    leaky_diff = (
        "+++ b/docs/note.md\n"
        "+token = 'sk-" + ("A" * 24) + "'\n"
        "+dsn = 'postgresql" + "://admin:pass@10.0.0.5:5432/prod'\n"
    )
    rules = {item["rule"] for item in pub_sync.scan_added_diff_lines(leaky_diff)}
    assert "openai_like_key" in rules
    assert "non_fixture_postgres_dsn" in rules
