"""Offline runner contracts; MockTransport never reaches a model or container."""
import importlib.util
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location("m4_evaluate", Path(__file__).parents[1] / "scripts/m4_evaluate.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


@pytest.fixture
def plan(tmp_path):
    return {"schema_version": 1, "api_url": "http://127.0.0.1:18792", "baseline_model_id": "strong",
        "stage_models": {stage: "cheap" if stage in {"execute", "dispatch"} else "strong" for stage in evaluation.STAGES},
        "workspace_root": str(tmp_path / "workspaces"), "output_root": str(tmp_path / "reports"),
        "session_cookie_file": str(tmp_path / "private-cookie"),
        "sandbox": {"endpoint": "http://127.0.0.1:19090", "key_file": str(tmp_path / "controller-key"),
                    "image": "sha256:" + "a" * 64, "snapshot_root": "/var/lib/eval/snapshots"}}


def models():
    return [{"id": identity, "model_name": identity, "adapter_type": "openai-compatible", "enabled": True,
        "base_url": "https://example.invalid/v1", "version": 2, "provider_version": 3,
        "max_output_tokens": 100000, "context_policy": {}, "capabilities": {"tools": {"status": "supported"}}}
        for identity in ("strong", "cheap")]


class FakeAcceptance:
    def __init__(self):
        self.calls = []

    async def evaluate(self, task, candidate, output):
        self.calls.append((task["id"], candidate, output))
        passed = candidate == evaluation.REFERENCE_SOLUTIONS[task["id"]].encode()
        return {"status": "PASS" if passed else "FAIL", "exit_code": 0 if passed else 1, "fixture_only": True}


class ApiFixture:
    def __init__(self, *, blocked=False):
        self.requests, self.runs, self.workspaces = [], {}, {}
        self.blocked = blocked

    def handle(self, request):
        path, method = request.url.path.removeprefix("/api/v1"), request.method
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path, body))
        if path == "/model-configs":
            return httpx.Response(200, json=models())
        if path == "/workspaces" and method == "POST":
            identity = "workspace-" + str(len(self.workspaces))
            self.workspaces[identity] = {**body, "id": identity, "ready": True}
            return httpx.Response(201, json=self.workspaces[identity])
        if path == "/conversations" and method == "POST":
            return httpx.Response(201, json={"id": "conversation-" + str(len(self.runs))})
        if path.startswith("/conversations/") and path.endswith("/messages"):
            if method == "GET":
                return httpx.Response(200, json=[{"role": "assistant", "content": "Fixture response only"}])
            identity = "run-" + str(len(self.runs))
            grant = self.workspaces[body["workspace_id"]]
            workspace = Path(grant["host_path"])
            filename = grant["write_allow"][0]
            task_id = "python-001" if filename.endswith(".py") else "typescript-001"
            (workspace / filename).write_bytes(evaluation.REFERENCE_SOLUTIONS[task_id].encode())
            self.runs[identity] = {"id": identity, "status": "awaiting_approval" if self.blocked else "completed",
                "agent_strategy": body["agent_strategy"], "usage": None,
                "workspace": {"tool_policy": {"network": "deny", "tools": ["read", "patch", "command"]}},
                "execution_config": {"budget": {"max_model_calls": 50, "max_tool_calls": 100},
                    "sandbox": {"image": "sha256:" + "a" * 64},
                    "models": {item["stage"]: {"id": item["model_config_id"], "max_output_tokens": 100000,
                                               "context_policy": {}} for item in body["stage_overrides"]}}}
            return httpx.Response(202, json={"run_id": identity})
        if path.startswith("/runs/"):
            parts = path.split("/")
            run = self.runs[parts[2]]
            if len(parts) == 3:
                return httpx.Response(200, json=run)
            endpoint = parts[3]
            if endpoint == "cancel":
                run["status"] = "cancelled"
                return httpx.Response(200, json=run)
            if endpoint == "events":
                return httpx.Response(200, text='id: 1\ndata: {"sequence_number":1,"event_type":"run.completed"}\n\n',
                                      headers={"Content-Type": "text/event-stream"})
            if endpoint == "budget":
                return httpx.Response(200, json={"model_calls": 3, "tool_calls": 2, "active_seconds": 1, "revisions": 0})
            if endpoint == "artifacts":
                return httpx.Response(200, json=[{"kind": "context_summary", "id": "summary-fixture"}])
            return httpx.Response(200, json=[])
        return httpx.Response(404)


def test_fixed_examples_match_dataset_but_never_contain_hidden_tests():
    _, tasks = evaluation.load_tasks()
    for task in tasks.values():
        root = evaluation.ROOT / "examples/evaluation/m4" / task["language"]
        assert (root / task["input"]["filename"]).read_bytes() == task["input"]["source_code"].encode()
        assert set(path.name for path in root.iterdir()) == {task["input"]["filename"], "README.md",
                                                          "smoke.py" if task["language"] == "python" else "smoke.ts"}
        assert task["verification_method"]["test_code"] not in evaluation.prompt(task)
        assert evaluation.REFERENCE_SOLUTIONS[task["id"]] not in evaluation.prompt(task)


def test_preview_schedule_has_exact_six_trials_and_reproducible_seed():
    first = evaluation.schedule(1, 43)
    assert len(first) == 6 and len(set(first)) == 6
    assert first == evaluation.schedule(1, 43)
    assert sorted(first) == [(task, condition, 1) for task in evaluation.TASKS for condition in "ABC"]
    assert len(evaluation.schedule(3, 43)) == 18


def test_versions_frozen_and_a_b_use_same_strong_model(plan):
    _, tasks = evaluation.load_tasks()
    chosen, _ = evaluation.model_preflight(plan, models())
    for condition in "ABC":
        payload = evaluation.submission(plan, chosen, condition, tasks["python-001"], "workspace", "trial")
        assert payload["agent_strategy"] == ("single" if condition == "A" else "staged")
        assert all(item["model_version"] == 2 and item["provider_version"] == 3 for item in payload["stage_overrides"])
        assert {item["model_config_id"] for item in payload["stage_overrides"]} == ({"strong", "cheap"} if condition == "C" else {"strong"})


@pytest.mark.parametrize("change", [
    {"adapter_type": "mock"}, {"enabled": False}, {"capabilities": {}}, {"max_output_tokens": 2048},
    {"context_policy": {"policy": {"input_target_tokens": 8000}}},
    {"context_policy": {"model_limits": {"context_window_tokens": 110000}}},
])
def test_unverified_or_incomparable_models_cannot_start(plan, change):
    rows = models()
    rows[1].update(change)
    with pytest.raises(evaluation.EvaluationError):
        evaluation.model_preflight(plan, rows)


@pytest.mark.parametrize("change", [
    {"api_url": "https://external.invalid"}, {"api_url": "http://token:secret@127.0.0.1:8787"},
    {"api_url": "http://127.0.0.1:8787/?secret=value"}, {"stage_models": {"think": "strong"}},
])
def test_plan_rejects_unsafe_or_incomplete_origins_and_bindings(plan, change):
    plan.update(change)
    with pytest.raises(evaluation.EvaluationError):
        evaluation.validate_plan(plan)


def test_result_root_must_not_be_inside_agent_workspace(plan):
    plan["output_root"] = str(Path(plan["workspace_root"]) / "reports")
    with pytest.raises(evaluation.EvaluationError):
        evaluation.validate_plan(plan)


def test_hidden_acceptance_uses_only_external_snapshot_and_confirmed_completion_marker():
    _, tasks = evaluation.load_tasks()
    for task in tasks.values():
        files, command = evaluation.acceptance_files(task, b"fixed trusted test fixture", "fresh-marker")
        assert command[0] in {"python", "node"}
        assert all("/" not in name and "\\" not in name for name in files)
        assert any("fresh-marker" in value for value in files.values())
        assert task["input"]["filename"] in files
        if task["language"] == "typescript":
            assert '"./candidate.ts"' in files["acceptance.ts"]
            assert "--experimental-strip-types" in command
    assert "subprocess" not in evaluation.SNAPSHOT_WRITER and "exec(" not in evaluation.SNAPSHOT_WRITER


@pytest.mark.asyncio
async def test_rest_six_run_contract_preserves_evidence_unknown_cost_and_separate_acceptance(plan):
    server, acceptance = ApiFixture(), FakeAcceptance()
    with httpx.Client(base_url=plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        report_path = await evaluation.run(plan, client=client, acceptance=acceptance)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "completed" and report["purpose"] == "wiring_smoke"
    assert report["fairness"]["status"] == "PASS" and report["quality_advantage_claim"] is None
    assert len(report["records"]) == 6 and len(acceptance.calls) == 10  # Four calibration containers, six trials.
    assert all(record["cost_estimate"] is None and record["billed_cost"] is None and record["usage"] is None
               for record in report["records"])
    assert all(record["summary_count"] == 1 and record["acceptance"]["status"] == "PASS" for record in report["records"])
    assert len({row["host_path"] for row in server.workspaces.values()}) == 6
    assert not any("/decision" in path for _, path, _ in server.requests)
    for record in report["records"]:
        directory = report_path.parent / record["evidence_directory"]
        assert all((directory / (name + ".json")).is_file() for name in ("run", "operations", "tools", "messages", "events", "acceptance"))


@pytest.mark.asyncio
async def test_bad_acceptance_calibration_prevents_any_model_run(plan):
    class AlwaysPass:
        async def evaluate(self, *args):
            return {"status": "PASS"}

    server = ApiFixture()
    with httpx.Client(base_url=plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        with pytest.raises(evaluation.EvaluationError, match="calibration"):
            await evaluation.run(plan, client=client, acceptance=AlwaysPass())
    assert not server.runs
    report = json.loads(next(Path(plan["output_root"]).glob("*/report.json")).read_text(encoding="utf-8"))
    assert report["status"] == "blocked" and not report["records"]


@pytest.mark.asyncio
async def test_runner_cancels_own_approval_wait_and_does_not_autoapprove(plan):
    server, acceptance = ApiFixture(blocked=True), FakeAcceptance()
    with httpx.Client(base_url=plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        with pytest.raises(evaluation.EvaluationError, match="no following"):
            await evaluation.run(plan, client=client, acceptance=acceptance)
    path = next(Path(plan["output_root"]).glob("*/report.json"))
    report = json.loads(path.read_text(encoding="utf-8"))
    assert all(item["run_status"] == "cancelled" and item["acceptance"] == {"status": "BLOCKED", "reason": "awaiting_approval"}
               for item in report["records"])
    assert len(acceptance.calls) == 4
    assert len([1 for method, path, _ in server.requests if method == "POST" and path.endswith("/cancel")]) == 1
    assert not any(path.endswith("/decision") for _, path, _ in server.requests)


def test_missing_or_changed_execution_metadata_never_counts_as_fair():
    assert evaluation.fairness_check([])["status"] == "UNVERIFIED"
    base = {"condition": "A", "agent_strategy": "single", "tool_policy": {"network": "deny"},
            "execution_config": {"budget": {"max_model_calls": 50}, "sandbox": {"image": "digest-a"},
                "models": {"execute": {"context_policy": {}, "max_output_tokens": 100000}}}}
    another = deepcopy(base)
    another["execution_config"]["sandbox"] = {"image": "digest-b"}
    assert evaluation.fairness_check([base, another])["status"] == "FAIL"
    another = deepcopy(base)
    another["agent_strategy"] = "staged"
    assert evaluation.fairness_check([another])["status"] == "FAIL"


@pytest.mark.asyncio
async def test_acceptance_requires_exit_zero_and_end_marker(plan, tmp_path):
    # Exercise the real acceptance wrapper using a local SDK adapter double.
    task = evaluation.load_tasks()[1]["python-001"]
    wrapper = object.__new__(evaluation.SandboxAcceptance)
    wrapper.config = SimpleNamespace(snapshot_root="/var/lib/eval/snapshots", image="fixed-image")
    captured = {}
    wrapper._transfer = lambda name, files: captured.update(files=files)

    class ExitedEarly:
        async def execute(self, **kwargs):
            await kwargs["created_sink"]("sandbox-fixture")
            await kwargs["started_sink"]("sandbox-fixture", "command-fixture")
            await kwargs["reclaimed_sink"]("sandbox-fixture")
            return SimpleNamespace(exit_code=0)

    wrapper.executor = ExitedEarly()
    result = await wrapper.evaluate(task, b"candidate fixture", tmp_path)
    assert result["status"] == "FAIL" and result["completion_marker_seen"] is False
    assert "acceptance.py" in captured["files"]


@pytest.fixture
def optimization_plan(plan, tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    return {**plan, "schema_version": 2, "strong_model_id": "strong", "economy_model_id": "cheap",
            "authorization_id": evaluation.AUTHORIZATION, "implementation_id": "immutable-fixture-build",
            "budget_ledger": str(tmp_path / ".omniharness/m4-optimization/budget.sqlite3")}


def optimization_model_rows():
    rows = models()
    for row in rows:
        row.update(context_policy={"schema_version": 1, "policy": {"version": "context-v2"}},
                   base_url="https://e-flowcode.cc/v1",
                   model_name="deepseek-v4-pro" if row["id"] == "strong" else "deepseek-flash")
    return rows


class OptimizationApi:
    def __init__(self, *, lose_first_response=False, failed=False, unknown=False):
        self.runs, self.workspaces, self.messages, self.requests = {}, {}, {}, []
        self.lose_first_response, self.failed, self.unknown = lose_first_response, failed, unknown
        self.model_rows = optimization_model_rows()
        self.tool_rows = []
        self.artifact_rows, self.artifact_content = [], {}

    def handle(self, request):
        path, method = request.url.path.removeprefix("/api/v1"), request.method
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path))
        if path.startswith("/artifacts/"):
            identity = path.split("/")[2]
            row = next(a for a in self.artifact_rows if a["id"] == identity)
            return httpx.Response(200, content=self.artifact_content[identity], headers={"X-Artifact-SHA256": row["sha256"]})
        if path == "/model-configs":
            return httpx.Response(200, json=self.model_rows)
        if path == "/workspaces":
            identity = "ws-" + str(len(self.workspaces))
            self.workspaces[identity] = {**body, "id": identity, "ready": True}
            return httpx.Response(201, json=self.workspaces[identity])
        if path == "/conversations":
            identity = "conversation-" + str(len(self.messages))
            self.messages[identity] = []
            return httpx.Response(201, json={"id": identity})
        if path.startswith("/conversations/") and path.endswith("/messages"):
            conversation = path.split("/")[2]
            if method == "GET":
                return httpx.Response(200, json=self.messages[conversation])
            self.messages[conversation].append({"role": "user", "content": body["content"]})
            workspace = self.workspaces[body["workspace_id"]]
            task = next(t for t in evaluation.load_optimization_tasks()[1].values() if t["prompt"] in body["content"])
            for name in ([] if self.unknown else task["write_allow"]):
                reference = evaluation.OPTIMIZATION_ROOT / task["id"] / "reference" / name
                (Path(workspace["host_path"]) / name).write_bytes(reference.read_bytes())
            identity = "run-" + str(len(self.runs))
            self.runs[identity] = {"id": identity, "conversation_id": conversation,
                "status": "failed" if self.failed else "completed", "error_code": "SUMMARY_INCOMPLETE" if self.failed else None,
                "agent_strategy": body["agent_strategy"], "finished_at": datetime.now(timezone.utc).isoformat(),
                "workspace": {"tool_policy": {"network": "deny", "tools": ["read", "patch", "command"]}},
                "execution_config": {"workflow_version": "stage-agent-v4",
                    "budget": {"max_model_calls": 50, "max_tool_calls": 100, "max_active_seconds": 1800},
                    "sandbox": {"image": "sha256:" + "a" * 64}, "models": {item["stage"]: {
                        "id": item["model_config_id"], "version": item["model_version"],
                        "max_output_tokens": 100000, "transport": "adapter-default",
                        "context_policy": {"schema_version": 1, "policy": {"version": "context-v2"}}}
                        for item in body["stage_overrides"]}}}
            if self.lose_first_response and len(self.runs) == 1:
                raise httpx.ReadError("Simulated response loss after durable server acceptance")
            return httpx.Response(202, json={"run_id": identity})
        if path == "/runs":
            return httpx.Response(200, json=list(self.runs.values()))
        if path.startswith("/runs/"):
            parts = path.split("/")
            run = self.runs[parts[2]]
            if len(parts) == 3:
                return httpx.Response(200, json=run)
            endpoint = parts[3]
            if endpoint == "events":
                return httpx.Response(200, text='data: {"sequence_number":1,"event_type":"run.completed"}\n\n')
            if endpoint == "budget":
                return httpx.Response(200, json={"model_calls": 1, "tool_calls": 2, "active_seconds": 1})
            if endpoint == "operations":
                return httpx.Response(200, json=[{"operation_id": run["id"], "node_id": "agent:execute:model_calls",
                    "status": "uncertain" if self.unknown else "completed"}])
            if endpoint == "tools":
                return httpx.Response(200, json=self.tool_rows)
            if endpoint == "artifacts":
                return httpx.Response(200, json=self.artifact_rows)
            return httpx.Response(200, json=[])
        return httpx.Response(404)


class OptimizationFakeAcceptance:
    config = SimpleNamespace(image="sha256:" + "a" * 64)

    async def evaluate(self, task, candidate, output):
        expected = {name: (evaluation.OPTIMIZATION_ROOT / task["id"] / "reference" / name).read_bytes()
                    for name in task["write_allow"]}
        passed = candidate == expected
        return {"status": "PASS" if passed else "FAIL", "groups_complete": True,
                "groups": [{"group": i, "status": "PASS" if passed else "FAIL"} for i in range(1, 6)]}


def test_optimization_105_schedule_is_frozen_blocked_and_disjoint():
    queue = evaluation.optimization_schedule(43)
    assert queue == evaluation.optimization_schedule(43) and len(queue) == 105
    assert [sum(row["phase"] == phase for row in queue) for phase in ("development", "first", "repeats")] == [15, 30, 60]
    assert len({(q["task_id"], q["condition"], q["repetition"]) for q in queue}) == 105
    for start in range(0, len(queue), 5):
        block = queue[start:start + 5]
        assert len({(q["task_id"], q["repetition"]) for q in block}) == 1
        assert {q["condition"] for q in block} == set(evaluation.CONDITIONS)


def test_optimization_five_actual_strategies_and_no_hidden_content(optimization_plan):
    _, tasks, _ = evaluation.load_optimization_tasks()
    chosen, _ = evaluation.optimization_models(optimization_plan, optimization_model_rows())
    task = next(iter(tasks.values()))
    for condition in evaluation.CONDITIONS:
        payload = evaluation.optimization_submission(optimization_plan, chosen, condition, task, "ws", "request")
        assert payload["agent_strategy"] == evaluation.STRATEGIES[condition]
        assert "OMNI_GROUP" not in payload["content"] and "acceptance.py" not in payload["content"]
        mapping = {r["stage"]: r["model_config_id"] for r in payload["stage_overrides"]}
        assert mapping["execute"] == ("cheap" if condition in {"Single-Flash", "Compact-Mixed"} else "strong")
        assert mapping["review"] == ("cheap" if condition == "Single-Flash" else "strong")


@pytest.mark.asyncio
async def test_optimization_normal_failure_is_retained_and_batch_continues(optimization_plan):
    server = OptimizationApi(failed=True)
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        path = await evaluation.run_optimization(optimization_plan, through="development", client=client,
                                               acceptance=OptimizationFakeAcceptance())
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["status"] == "phase_complete" and len(report["records"]) == 15
    assert all(not row["resolved"] and row["acceptance"]["status"] == "PASS" for row in report["records"])
    assert report["budget"]["observed_calls"] == 15
    assert report["fairness"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_response_loss_resume_reads_existing_run_without_paid_resubmit(optimization_plan):
    server = OptimizationApi(lose_first_response=True)
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        with pytest.raises(httpx.ReadError):
            await evaluation.run_optimization(optimization_plan, through="development", client=client,
                                              acceptance=OptimizationFakeAcceptance())
        root = next(Path(optimization_plan["output_root"]).glob("m4-opt-*"))
        path = await evaluation.run_optimization(optimization_plan, through="development", resume=root,
                    client=client, acceptance=OptimizationFakeAcceptance())
    report = json.loads(path.read_text(encoding="utf-8"))
    assert len(server.runs) == 15 and len(report["records"]) == 15
    assert sum(method == "POST" and path.endswith("/messages") for method, path in server.requests) == 15
    assert report["budget"]["observed_calls"] == 15


@pytest.mark.asyncio
async def test_unknown_operation_stops_batch_and_keeps_full_reservation(optimization_plan):
    server = OptimizationApi(unknown=True)
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        with pytest.raises(evaluation.EvaluationError, match="Uncertain"):
            await evaluation.run_optimization(optimization_plan, through="development", client=client,
                                              acceptance=OptimizationFakeAcceptance())
    ledger = evaluation.BudgetLedger(optimization_plan["budget_ledger"]).snapshot()
    assert len(server.runs) == 1 and ledger["committed"] == 50 and ledger["reservations"][0]["state"] == "unknown"


@pytest.mark.asyncio
async def test_insufficient_global_allowance_never_starts_a_paid_trial(optimization_plan):
    ledger = evaluation.BudgetLedger(optimization_plan["budget_ledger"])
    for i in range(29):
        ledger.reserve("prior-" + str(i), 50, "prior authorized requests")
    ledger.reserve("probe", 2, "capability")
    server = OptimizationApi()
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        path = await evaluation.run_optimization(optimization_plan, through="development", client=client,
                                               acceptance=OptimizationFakeAcceptance())
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["status"] == "NOT_RUN_BUDGET" and len(report["remaining"]) == 105 and not server.runs


def test_model_wall_union_does_not_double_count_parallel_boundaries():
    rows = [{"started_at": "2026-11-09T00:00:00Z", "finished_at": "2026-11-09T00:00:10Z"},
            {"started_at": "2026-11-09T00:00:05Z", "finished_at": "2026-11-09T00:00:12Z"}]
    metrics = evaluation.interval_metrics(rows)
    assert metrics["summed_seconds"] == 17 and metrics["union_seconds"] == 12
    assert evaluation.interval_metrics([*rows, {}])["complete"] is False
    summaries = [{**r, "node_id": "agent:execute:model_calls", "purpose": "context_summary",
                  "status": "completed", "output_status": "truncated" if i == 0 else "complete",
                  "recovery_of": "original" if i else None} for i, r in enumerate(rows)]
    result = evaluation.trial_metrics({}, summaries, [], "2026-11-09T00:00:00Z")
    assert result["summary_failed_attempts"] == 1 and result["summary_recovery_attempts"] == 1


def test_failed_cost_is_kept_but_failed_speed_never_a_success_win():
    base = {"split": "formal", "task_id": "task", "repetition": 1, "model_calls": 5,
            "acceptance": {"status": "PASS"}, "run_status": "completed", "resolved": True,
            "latency_seconds": 10, "cost_estimate": {"reference_cost": "1"}}
    failed = {**base, "condition": "Compact-Mixed", "run_status": "failed", "resolved": False,
              "latency_seconds": 2, "cost_estimate": {"reference_cost": "0.5"}}
    summary = evaluation.summarize_optimization([{**base, "condition": "Single-Pro"}, failed])
    group = summary["conditions"]["Compact-Mixed"]
    assert group["all_attempt_reference_cost"] == "0.5" and group["success_median_wall_seconds"] is None
    pair = next(p for p in summary["paired_task_repetitions"] if p["baseline"] == "Single-Pro")
    assert pair["complete_pair"] and pair["equal_success_wall_ratio"] is None and pair["equal_success_cost_ratio"] is None


def test_cross_restart_failed_latency_is_unknown_not_accumulated():
    record = {"split": "formal", "condition": "Single-Flash", "task_id": "task", "repetition": 1,
              "model_calls": 4, "acceptance": {"status": "PASS"}, "run_status": "cancelled", "resolved": False,
              "latency_seconds": 50000, "elapsed_time_valid": False, "cost_estimate": {"reference_cost": None}}
    item = evaluation.summarize_optimization([record])["conditions"]["Single-Flash"]
    assert item["failed_wall_seconds"] is None and item["failed_wall_seconds_complete"] is False


@pytest.mark.asyncio
async def test_formal_gate_allows_one_explicit_freeze_then_rejects_reconfiguration(optimization_plan, monkeypatch):
    monkeypatch.setattr(evaluation, "verify_formal_runtime", lambda plan, frozen: None)
    server = OptimizationApi()
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        path = await evaluation.run_optimization(optimization_plan, through="development", client=client,
                                               acceptance=OptimizationFakeAcceptance())
        changed = {**optimization_plan, "implementation_id": "post-development-frozen-build"}
        with pytest.raises(evaluation.EvaluationError, match="explicit development freeze"):
            await evaluation.run_optimization(optimization_plan, through="first", resume=path.parent, client=client,
                                              acceptance=OptimizationFakeAcceptance())
        with pytest.raises(evaluation.EvaluationError, match="completed frozen first 30"):
            await evaluation.run_optimization(optimization_plan, through="all", resume=path.parent, freeze_formal=True,
                                              client=client, acceptance=OptimizationFakeAcceptance())
        def reject_runtime(plan, frozen):
            raise evaluation.EvaluationError("Formal candidate runtime differs")
        monkeypatch.setattr(evaluation, "verify_formal_runtime", reject_runtime)
        with pytest.raises(evaluation.EvaluationError, match="runtime differs"):
            await evaluation.run_optimization(optimization_plan, through="first", resume=path.parent, freeze_formal=True,
                                              client=client, acceptance=OptimizationFakeAcceptance())
        assert len(server.runs) == 15
        monkeypatch.setattr(evaluation, "verify_formal_runtime", lambda plan, frozen: None)
        with pytest.raises(evaluation.EvaluationError, match="explicit development"):
            await evaluation.run_optimization(changed, through="first", resume=path.parent, client=client,
                                              acceptance=OptimizationFakeAcceptance())
        assert len(server.runs) == 15
        checks = []
        def fail_after_first_trial(plan, frozen):
            checks.append(True)
            if len(checks) == 2:
                raise evaluation.EvaluationError("Formal runtime changed after trial")
        monkeypatch.setattr(evaluation, "verify_formal_runtime", fail_after_first_trial)
        with pytest.raises(evaluation.EvaluationError, match="changed after trial"):
            await evaluation.run_optimization(changed, through="first", resume=path.parent, freeze_formal=True,
                            client=client, acceptance=OptimizationFakeAcceptance())
        blocked = json.loads(path.read_text(encoding="utf-8"))
        assert blocked["status"] == "blocked" and len(blocked["records"]) == 16 and len(server.runs) == 16
        monkeypatch.setattr(evaluation, "verify_formal_runtime", lambda plan, frozen: None)
        path = await evaluation.run_optimization(changed, through="first", resume=path.parent,
                        client=client, acceptance=OptimizationFakeAcceptance())
        report = json.loads(path.read_text(encoding="utf-8"))
        assert len(report["records"]) == 45 and report["formal_freeze"]["implementation_id"] == changed["implementation_id"]
        assert report["formal_freeze"]["strong_model_id"] == "strong"
        assert report["formal_freeze"]["economy_model_id"] == "cheap"
        assert report["development_plan"] == optimization_plan
        assert set(report["development_models"]) == {"strong", "cheap"}
        assert report["development_quote_snapshot"]["version"] == report["quote_snapshot"]["version"]
        queue = evaluation.optimization_schedule(43)
        evaluation.validate_formal_gate(report, queue, "all", False)
        incomplete = {**report, "records": report["records"][:-1]}
        evaluation.validate_formal_gate(incomplete, queue, "first", False)
        with pytest.raises(evaluation.EvaluationError, match="all 30"):
            evaluation.validate_formal_gate(incomplete, queue, "all", False)
        with pytest.raises(evaluation.EvaluationError, match="passing unchanged"):
            evaluation.validate_formal_gate({**report, "fairness": {"status": "FAIL"}}, queue, "all", False)
        bad = {**report, "records": [*report["records"][:-1], {**report["records"][-1], "condition": "wrong"}]}
        with pytest.raises(evaluation.EvaluationError, match="all 30"):
            evaluation.validate_formal_gate(bad, queue, "all", False)
        assert report["summary"]["conditions"]["Single-Pro"]["attempted"] == 6
        with pytest.raises(evaluation.EvaluationError, match="explicit development"):
            await evaluation.run_optimization(optimization_plan, through="all", resume=path.parent,
                        freeze_formal=True, client=client, acceptance=OptimizationFakeAcceptance())
        assert len(server.runs) == 45


@pytest.mark.parametrize("fault", [None, "api_image", "worker_image", "stopped", "port", "project", "sandbox"])
def test_formal_candidate_identity_check_is_read_only_and_fails_closed(monkeypatch, fault):
    image, sandbox = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    plan = {"api_url": "http://127.0.0.1:18792", "implementation_id": image,
            "sandbox": {"wsl_distro": "OmniHarnessCheckAlt", "image": sandbox}}
    frozen = {"implementation_id": image, "sandbox": plan["sandbox"]}
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "image" in args:
            value = ("sha256:" + "3" * 64) if fault == "sandbox" else sandbox
            return SimpleNamespace(stdout=value.encode())
        api_image = ("sha256:" + "3" * 64) if fault == "api_image" else image
        worker_image = ("sha256:" + "3" * 64) if fault == "worker_image" else image
        state = "false" if fault == "stopped" else "true"
        project = "other" if fault == "project" else "omni-m4-preview"
        port = "9999" if fault == "port" else "18792"
        rows = [f'/omni-m4-preview-api-1|true|{api_image}|{project}|[{{"HostIp":"127.0.0.1","HostPort":"{port}"}}]',
                f"/omni-m4-preview-worker-1|{state}|{worker_image}|omni-m4-preview|null"]
        return SimpleNamespace(stdout="\n".join(rows).encode())

    monkeypatch.setattr(evaluation.subprocess, "run", run)
    if fault:
        with pytest.raises(evaluation.EvaluationError, match="does not match"):
            evaluation.verify_formal_runtime(plan, frozen)
    else:
        evaluation.verify_formal_runtime(plan, frozen)
    assert all("inspect" in args and not any(op in args for op in ("start", "stop", "exec", "rm"))
               for args in calls)


def test_state_replace_contention_never_replays_or_deletes_previous_state(tmp_path, monkeypatch):
    if evaluation.os.name != "nt":
        pytest.skip("Windows-only observed atomic replace contention")
    path = tmp_path / "state.json"
    evaluation.save(path, {"stage": "before"})
    original, attempts = Path.replace, []

    def contention(source, target):
        attempts.append(str(source))
        if len(attempts) <= 2:
            assert json.loads(path.read_text(encoding="utf-8"))["stage"] == "before"
            raise PermissionError("Simulated transient local rename contention")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", contention)
    evaluation.save(path, {"stage": "after"})
    assert len(attempts) == 3 and json.loads(path.read_text(encoding="utf-8"))["stage"] == "after"


def development_audit(root, **changes):
    return {"schema_version": 1, "authorization_id": evaluation.AUTHORIZATION, "session": root.name,
            "operator": "authorized-test-operator", "reason": "Retain terminal unknown model failure without replay",
            "reviewed_at": "2026-11-09T09:40:00Z", "adopt_current_models": False,
            "runs": [{"run_id": "run-0", "terminal_status": "cancelled", "operation_ids": ["run-0"]}], **changes}


async def stopped_optimization(optimization_plan, client, server):
    with pytest.raises(evaluation.EvaluationError, match="Uncertain"):
        await evaluation.run_optimization(optimization_plan, through="development", client=client,
                                          acceptance=OptimizationFakeAcceptance())
    server.runs["run-0"]["status"] = "cancelled"
    return next(Path(optimization_plan["output_root"]).glob("m4-opt-*"))


@pytest.mark.asyncio
async def test_manual_development_review_runs_acceptance_without_any_model_replay(optimization_plan):
    server = OptimizationApi(unknown=True)
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        root = await stopped_optimization(optimization_plan, client, server)
        before = len(server.requests)
        path = await evaluation.run_optimization(optimization_plan, through="development", resume=root,
            development_review=development_audit(root), review_only=True, client=client,
            acceptance=OptimizationFakeAcceptance())
        assert all(method == "GET" for method, _ in server.requests[before:])
        report = json.loads(path.read_text(encoding="utf-8"))
        record = report["records"][0]
        assert report["status"] == "development_review_complete" and len(report["records"]) == 1
        assert not record["resolved"] and record["acceptance"]["status"] == "FAIL"
        assert record["cost_estimate"]["reference_cost"] is None and record["replayed"] is False
        assert report["budget"]["committed"] == 50 and report["budget"]["reservations"][0]["state"] == "unknown"
        # Once recorded, ordinary resume skips this original slot permanently.
        server.unknown = False
        path = await evaluation.run_optimization(optimization_plan, through="development", resume=root,
            client=client, acceptance=OptimizationFakeAcceptance())
    report = json.loads(path.read_text(encoding="utf-8"))
    assert len(server.runs) == len(report["records"]) == 15 and report["budget"]["committed"] == 64
    assert sum(method == "POST" and p.endswith("/messages") for method, p in server.requests) == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", ["unknown_tool", "held_slot", "wrong_operation", "running", "changed_workspace"])
async def test_development_review_rejects_unsafe_or_mismatched_evidence_without_control(optimization_plan, violation):
    server = OptimizationApi(unknown=True)
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        root = await stopped_optimization(optimization_plan, client, server)
        audit = development_audit(root)
        if violation == "unknown_tool":
            server.tool_rows = [{"tool_name": "workspace_read", "status": "uncertain"}]
        elif violation == "held_slot":
            server.tool_rows = [{"tool_name": "workspace_read", "status": "completed", "slot_claimed": True}]
        elif violation == "wrong_operation":
            audit["runs"][0]["operation_ids"] = ["a-different-operation"]
        elif violation == "running":
            server.runs["run-0"]["status"] = "running"
        else:
            state = json.loads(next(root.glob("*/trial-state.json")).read_text(encoding="utf-8"))
            name = next(iter(state["initial_sha256"]))
            (Path(state["workspace_path"]) / name).write_text("changed after the failure", encoding="utf-8")
        before = len(server.requests)
        with pytest.raises(evaluation.EvaluationError):
            await evaluation.run_optimization(optimization_plan, through="development", resume=root,
                development_review=audit, review_only=True, client=client, acceptance=OptimizationFakeAcceptance())
        assert all(method == "GET" for method, _ in server.requests[before:])
    assert evaluation.BudgetLedger(optimization_plan["budget_ledger"]).snapshot()["committed"] == 50


@pytest.mark.asyncio
async def test_development_model_revision_requires_explicit_audit_and_formal_refuses_it(optimization_plan):
    server = OptimizationApi(unknown=True)
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        root = await stopped_optimization(optimization_plan, client, server)
        for row in server.model_rows:
            row.update(timeout_seconds=600, version=row["version"] + 1)
        with pytest.raises(evaluation.EvaluationError, match="same frozen"):
            await evaluation.run_optimization(optimization_plan, through="development", resume=root,
                client=client, acceptance=OptimizationFakeAcceptance())
        path = await evaluation.run_optimization(optimization_plan, through="development", resume=root,
            development_review=development_audit(root, adopt_current_models=True), review_only=True,
            client=client, acceptance=OptimizationFakeAcceptance())
        report = json.loads(path.read_text(encoding="utf-8"))
        revision = report["development_model_revisions"][0]
        assert revision["before"] != revision["after"] and revision["after"]["strong"]["timeout_seconds"] == 600
        assert report["records"][0]["manual_development_review"]["operator"] == "authorized-test-operator"
        report["formal_freeze"] = {"already": "frozen"}
        evaluation.save(path, report)
        before = len(server.requests)
        with pytest.raises(evaluation.EvaluationError, match="formal freeze"):
            await evaluation.run_optimization(optimization_plan, through="development", resume=root,
                development_review=development_audit(root, adopt_current_models=True), review_only=True,
                client=client, acceptance=OptimizationFakeAcceptance())
        assert len(server.requests) == before


def completed_effect_audit(root, server):
    state = json.loads(next(root.glob("*/trial-state.json")).read_text(encoding="utf-8"))
    task = evaluation.load_optimization_tasks()[1][evaluation.optimization_schedule(43)[0]["task_id"]]
    run = server.runs["run-0"]
    policy = {"network": "deny", "snapshot_writeback": False}
    run["workspace"]["tool_policy"] = policy
    audit = development_audit(root)
    proof = {"run_id": run["id"], "frozen_tool_policy": policy, "patches": [], "commands": [],
             "docker_listing_exit_code": 0, "docker_sandbox_matches": {"sandbox-1": False}}
    audit["runs"][0]["completed_tool_evidence"] = proof

    def add_tool(identity, name, parameters, result, content, **ids):
        artifact = {"id": "artifact-" + identity, "run_id": run["id"], "operation_id": identity,
                    "sha256": evaluation.digest(content), "size_bytes": len(content),
                    "kind": "command_log" if name == "run_command" else "tool_result"}
        server.artifact_rows.append(artifact)
        server.artifact_content[artifact["id"]] = content
        server.tool_rows.append({"id": identity, "run_id": run["id"], "tool_name": name,
            "tool_version": "builtin-workspace-v1", "status": "completed", "parameters": parameters,
            "result": {**result, "artifacts": [artifact]}, "slot_claimed": False,
            "finished_at": "2026-11-09T09:30:00Z", "reclaimed_at": "2026-11-09T09:30:01Z", **ids})

    for index, name in enumerate(task["write_allow"]):
        identity = "patch-" + str(index)
        content = (evaluation.OPTIMIZATION_ROOT / task["id"] / "reference" / name).read_bytes()
        before, after = state["initial_sha256"][name], evaluation.digest(content)
        (Path(state["workspace_path"]) / name).write_bytes(content)
        result = {"path": name, "before_sha256": before, "sha256": after, "state": "completed",
                  "run_id": run["id"], "recovery_id": identity, "workspace_id": state["workspace"]["id"]}
        add_tool(identity, "workspace_patch", {"path": name, "content": content.decode(), "expected_sha256": before},
                 result, json.dumps(result).encode())
        proof["patches"].append({"operation_id": identity, "path": name,
                                 "receipt_before_sha256": before, "receipt_sha256": after})
    ids = {"sandbox_id": "sandbox-1", "command_id": "command-1", "snapshot_id": "snapshot-1"}
    add_tool("command", "run_command", {"command": "python -B smoke.py"}, {**ids, "exit_code": 0}, b"ok", **ids)
    proof["commands"].append({"operation_id": "command", **ids, "exit_code": 0, "slot_claimed": False,
                              "reclaimed_at": "2026-11-09T09:30:01Z", "controller_status": 404})
    return audit


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", [None, "receipt", "file", "artifact", "command", "container", "policy", "unknown"])
async def test_development_review_matches_completed_effects_and_retains_unknown_budget(optimization_plan, violation):
    server = OptimizationApi(unknown=True)
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        root = await stopped_optimization(optimization_plan, client, server)
        audit = completed_effect_audit(root, server)
        proof = audit["runs"][0]["completed_tool_evidence"]
        if violation == "receipt":
            server.tool_rows[0]["result"]["before_sha256"] = "changed"
        elif violation == "file":
            state = json.loads(next(root.glob("*/trial-state.json")).read_text(encoding="utf-8"))
            (Path(state["workspace_path"]) / next(iter(state["initial_sha256"]))).write_bytes(b"other change")
        elif violation == "artifact":
            server.artifact_content[server.artifact_rows[0]["id"]] = b"corrupted"
        elif violation == "command":
            proof["commands"][0]["snapshot_id"] = "another-snapshot"
        elif violation == "container":
            proof["docker_sandbox_matches"]["sandbox-1"] = True
        elif violation == "policy":
            server.runs["run-0"]["workspace"]["tool_policy"] = {"network": "allow", "snapshot_writeback": False}
        elif violation == "unknown":
            server.tool_rows[-1]["status"] = "uncertain"
        before = len(server.requests)
        kwargs = {"through": "development", "resume": root, "development_review": audit,
                  "review_only": True, "client": client, "acceptance": OptimizationFakeAcceptance()}
        if violation:
            with pytest.raises(evaluation.EvaluationError):
                await evaluation.run_optimization(optimization_plan, **kwargs)
        else:
            path = await evaluation.run_optimization(optimization_plan, **kwargs)
            record = json.loads(path.read_text(encoding="utf-8"))["records"][0]
            assert not record["resolved"] and record["acceptance"]["status"] == "PASS" and not record["replayed"]
            assert len([p for _, p in server.requests[before:] if p.startswith("/artifacts/")]) == len(server.artifact_rows)
        assert all(method == "GET" for method, _ in server.requests[before:])
        assert evaluation.BudgetLedger(optimization_plan["budget_ledger"]).snapshot()["committed"] == 50


@pytest.mark.asyncio
async def test_development_build_revision_preserves_slots_and_freezes_before_formal(optimization_plan):
    server = OptimizationApi(unknown=True)
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        root = await stopped_optimization(optimization_plan, client, server)
        changed = {**optimization_plan, "implementation_id": "explicit-new-build"}
        audit = development_audit(root, runs=[], implementation_revision={
            "before": optimization_plan["implementation_id"], "after": changed["implementation_id"]})
        with pytest.raises(evaluation.EvaluationError, match="all dispatched slots recorded"):
            await evaluation.run_optimization(changed, through="development", resume=root,
                development_review=audit, review_only=True, client=client, acceptance=OptimizationFakeAcceptance())
        path = await evaluation.run_optimization(optimization_plan, through="development", resume=root,
            development_review=development_audit(root), review_only=True, client=client,
            acceptance=OptimizationFakeAcceptance())
        old_record = json.loads(path.read_text(encoding="utf-8"))["records"][0]
        with pytest.raises(evaluation.EvaluationError, match="explicit development-to-formal"):
            await evaluation.run_optimization(changed, through="development", resume=root,
                client=client, acceptance=OptimizationFakeAcceptance())
        path = await evaluation.run_optimization(changed, through="development", resume=root,
            development_review=audit, review_only=True, client=client, acceptance=OptimizationFakeAcceptance())
        report = json.loads(path.read_text(encoding="utf-8"))
        assert report["records"] == [old_record]
        assert report["development_implementation_revisions"][0]["after_recorded_sequence"] == 1
        assert report["plan"]["implementation_id"] == changed["implementation_id"]
        server.unknown = False
        path = await evaluation.run_optimization(changed, through="development", resume=root,
            client=client, acceptance=OptimizationFakeAcceptance())
        report = json.loads(path.read_text(encoding="utf-8"))
        assert len(server.runs) == len(report["records"]) == 15
        assert report["records"][0] == old_record
        assert all(r["implementation_id"] == changed["implementation_id"] for r in report["records"][1:])
        report["formal_freeze"] = {"already": "frozen"}
        evaluation.save(path, report)
        before = len(server.requests)
        with pytest.raises(evaluation.EvaluationError, match="formal freeze"):
            await evaluation.run_optimization(changed, through="development", resume=root,
                development_review=audit, review_only=True, client=client, acceptance=OptimizationFakeAcceptance())
        assert len(server.requests) == before


@pytest.mark.parametrize("violation", [None, "missing", "default", "unfrozen", "stale_binding", "cross_condition"])
def test_formal_transport_matches_exact_frozen_model_capability(violation):
    from omniharness.providers import summary_capability_binding

    model = optimization_model_rows()[0]
    other_model = optimization_model_rows()[1]
    model["capabilities"]["tools"].update(
        transport=evaluation.NATIVE_AGENT_STREAM, transport_binding=summary_capability_binding(model))
    actual = {"id": model["id"], "version": model["version"], "max_output_tokens": 100000, "context_policy": model["context_policy"],
              "transport": evaluation.NATIVE_AGENT_STREAM}
    config = {"workflow_version": "stage-agent-v4", "models": {stage: deepcopy(actual) for stage in evaluation.STAGES},
              "budget": {"max_model_calls": 50, "max_tool_calls": 100, "max_active_seconds": 1800},
              "sandbox": {"image": "immutable-image"}}
    records = [{"split": "formal", "condition": "Single-Pro", "agent_strategy": "single",
                "execution_config": config, "tool_policy": {"network": "deny"}}]
    actual = config["models"]["execute"]
    frozen = {model["id"]: model, other_model["id"]: other_model}
    if violation == "missing":
        actual.pop("transport")
    elif violation == "default":
        actual["transport"] = "adapter-default"
    elif violation == "unfrozen":
        frozen = None
    elif violation == "stale_binding":
        model["capabilities"]["tools"]["transport_binding"] = "previous-parameters"
    elif violation == "cross_condition":
        other = deepcopy(records[0])
        other.update(condition="Compact-Pro", agent_strategy="compact")
        other["execution_config"]["models"]["execute"]["transport"] = "adapter-default"
        records.append(other)
    result = evaluation.optimization_fairness(records, frozen_models=frozen,
                    frozen_selection={"strong_model_id": model["id"], "economy_model_id": other_model["id"]})
    assert result["status"] == ("PASS" if violation is None else
                                "UNVERIFIED" if violation in {"missing", "unfrozen"} else "FAIL")
    # Explicit development revisions are retained; old snapshots lack transport.
    for record in records:
        record["split"] = "development"
    assert evaluation.optimization_fairness(records)["status"] == "PASS"


@pytest.mark.parametrize("condition", evaluation.CONDITIONS)
@pytest.mark.parametrize("fault", [None, "wrong_model", "wrong_version", "missing_stage"])
def test_formal_condition_checks_each_actual_stage_and_version(condition, fault):
    rows = {row["id"]: row for row in optimization_model_rows()}
    selection = {"strong_model_id": "strong", "economy_model_id": "cheap"}
    mapping = evaluation.condition_assignment(condition, "strong", "cheap")
    stages = {stage: {"id": identity, "version": rows[identity]["version"], "max_output_tokens": 100000,
                      "context_policy": rows[identity]["context_policy"], "transport": "adapter-default"}
              for stage, identity in mapping.items()}
    if fault == "wrong_model":
        stages["review"]["id"] = "cheap" if mapping["review"] == "strong" else "strong"
    elif fault == "wrong_version":
        stages["execute"]["version"] += 1
    elif fault == "missing_stage":
        stages.pop("execute")
    record = {"split": "formal", "condition": condition, "agent_strategy": evaluation.STRATEGIES[condition],
              "execution_config": {"workflow_version": "stage-agent-v4", "models": stages,
                                   "budget": {"max_model_calls": 50, "max_tool_calls": 100, "max_active_seconds": 1800},
                                   "sandbox": {"image": "immutable"}},
              "tool_policy": {"network": "deny"}}
    actual = evaluation.optimization_fairness([record], frozen_models=rows, frozen_selection=selection)
    assert actual["status"] == ("PASS" if fault is None else "UNVERIFIED" if fault == "missing_stage" else "FAIL")


@pytest.mark.asyncio
async def test_formal_switch_failed_preflight_keeps_original_development_report(optimization_plan, monkeypatch):
    monkeypatch.setattr(evaluation, "verify_formal_runtime", lambda plan, frozen: None)
    server = OptimizationApi()
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        path = await evaluation.run_optimization(optimization_plan, through="development", client=client,
                                                acceptance=OptimizationFakeAcceptance())
        before = path.read_bytes()
        changed = {**optimization_plan, "strong_model_id": "replacement"}
        server.model_rows.append({**server.model_rows[0], "id": "replacement", "capabilities": {}})
        with pytest.raises(evaluation.EvaluationError, match="tested tool support"):
            await evaluation.run_optimization(changed, through="first", resume=path.parent, freeze_formal=True,
                                              client=client, acceptance=OptimizationFakeAcceptance())
        assert path.read_bytes() == before
        assert len(server.runs) == 15


@pytest.mark.asyncio
async def test_formal_capability_drift_stops_before_next_run(optimization_plan, monkeypatch):
    monkeypatch.setattr(evaluation, "verify_formal_runtime", lambda plan, frozen: None)
    server = OptimizationApi()
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        path = await evaluation.run_optimization(optimization_plan, through="development", client=client,
                                                acceptance=OptimizationFakeAcceptance())
    count = 0
    def drift(request):
        nonlocal count
        if request.url.path.endswith("/model-configs"):
            count += 1
            if count == 4:
                server.model_rows[0]["capabilities"]["tools"]["new_probe_metadata"] = True
        return server.handle(request)
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(drift)) as client:
        with pytest.raises(evaluation.EvaluationError, match="capabilities changed before dispatch"):
            await evaluation.run_optimization(optimization_plan, through="first", resume=path.parent,
                                              freeze_formal=True, client=client, acceptance=OptimizationFakeAcceptance())
    report = json.loads(path.read_text(encoding="utf-8"))
    assert len(report["records"]) == 16 and len(server.runs) == 16
    assert report["formal_freeze"]["strong_model_id"] == "strong"


@pytest.mark.asyncio
async def test_formal_preflight_capability_drift_keeps_original_report(optimization_plan, monkeypatch):
    monkeypatch.setattr(evaluation, "verify_formal_runtime", lambda plan, frozen: None)
    server = OptimizationApi()
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(server.handle)) as client:
        path = await evaluation.run_optimization(optimization_plan, through="development", client=client,
                                                acceptance=OptimizationFakeAcceptance())
    original = path.read_bytes()
    count = 0
    def drift(request):
        nonlocal count
        if request.url.path.endswith("/model-configs"):
            count += 1
            if count == 2:
                server.model_rows[0]["capabilities"]["tools"]["new_probe_metadata"] = True
        return server.handle(request)
    with httpx.Client(base_url=optimization_plan["api_url"], transport=httpx.MockTransport(drift)) as client:
        with pytest.raises(evaluation.EvaluationError, match="capabilities changed before dispatch"):
            await evaluation.run_optimization(optimization_plan, through="first", resume=path.parent,
                                              freeze_formal=True, client=client, acceptance=OptimizationFakeAcceptance())
    assert path.read_bytes() == original and len(server.runs) == 15
