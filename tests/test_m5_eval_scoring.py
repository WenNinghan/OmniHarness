import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omniharness.eval_scoring import (
    DATASET_SPLITS,
    REFERENCE_CODE_SOLUTIONS,
    TASK_TO_SPLIT,
    build_research_prompt,
    grade_python_task,
    grade_research_task,
    grade_task,
    grade_typescript_task,
    load_regression_dataset,
    summarize_eval_records,
)

ROOT = Path(__file__).resolve().parents[1]
_HAS_WEB_TSC = (ROOT / "web" / "node_modules" / "typescript" / "bin" / "tsc").is_file()


def _tasks_by_id():
    dataset, _ = load_regression_dataset()
    return {task["id"]: task for task in dataset["tasks"]}


def test_dataset_splits_and_sha_validation():
    dataset, sha = load_regression_dataset()
    assert len(sha) == 64
    tasks = dataset["tasks"]
    assert len(tasks) == 20
    all_split_ids = [tid for ids in DATASET_SPLITS.values() for tid in ids]
    assert len(all_split_ids) == 20
    assert len(set(all_split_ids)) == 20
    assert set(TASK_TO_SPLIT) == {task["id"] for task in tasks}
    assert len(DATASET_SPLITS["A"]) == 8
    assert len(DATASET_SPLITS["B"]) == 6
    assert len(DATASET_SPLITS["C"]) == 6


def test_build_research_prompt_includes_sources_and_omits_expected_answers():
    tasks = _tasks_by_id()
    leaks = {
        "research-001": ["4.85", "2.4", "0.65"],
        "research-002": ["0.75", "0.15"],
        "research-004": ["12000000", "15000000", "DISPUTED"],
        "research-005": ["public_beta"],
        "research-006": ["0.1"],
        "research-008": ["0.9125"],
        "research-009": ["0.06666666666666667"],
    }
    for task_id in [tid for tid in tasks if tid.startswith("research-")]:
        task = tasks[task_id]
        prompt = build_research_prompt(task)
        for doc in task["input"]["source_documents"]:
            assert doc["document_id"] in prompt
        skeleton_section = prompt.split("请仅输出符合以下字段结构的单个 JSON 对象", 1)[1]
        assert "<string>" in skeleton_section or "0.0" in skeleton_section
        for secret_answer in leaks.get(task_id, []):
            assert secret_answer not in prompt
        for line_doc in task["input"]["source_documents"]:
            assert f"{line_doc['document_id']}:1" not in skeleton_section


def test_all_10_research_tasks_pass_reference_raw_and_fenced():
    tasks = _tasks_by_id()
    for i in range(1, 11):
        tid = f"research-{i:03d}"
        task = tasks[tid]
        raw = json.dumps(task["expected"], ensure_ascii=False)
        fenced = f"分析完成，结果如下：\n```json\n{raw}\n```"
        assert grade_research_task(task, raw)["passed"] is True, tid
        assert grade_research_task(task, fenced)["passed"] is True, tid
        assert grade_task(task, task["expected"])["passed"] is True, tid


def test_research_grading_rejects_targeted_adversarial_failures():
    tasks = _tasks_by_id()

    # research-001: numeric outside 1e-9 / wrong unit
    bad1 = copy.deepcopy(tasks["research-001"]["expected"])
    bad1["total"] = 4.8501
    assert grade_research_task(tasks["research-001"], bad1)["passed"] is False
    bad1_unit = copy.deepcopy(tasks["research-001"]["expected"])
    bad1_unit["unit"] = "Wh"
    assert grade_research_task(tasks["research-001"], bad1_unit)["passed"] is False

    # research-003: measured_new_latency_ms = 100 or forbidden inference in prose
    bad3 = copy.deepcopy(tasks["research-003"]["expected"])
    bad3["measured_new_latency_ms"] = 100
    assert grade_research_task(tasks["research-003"], bad3)["passed"] is False
    prose3 = "新系统实测耗时100 ms。\n```json\n" + json.dumps(tasks["research-003"]["expected"], ensure_ascii=False) + "\n```"
    assert grade_research_task(tasks["research-003"], prose3)["passed"] is False

    # research-005: post-cutoff R005-B leaked into evidence
    bad5 = copy.deepcopy(tasks["research-005"]["expected"])
    bad5["evidence"].append("R005-B:1")
    assert grade_research_task(tasks["research-005"], bad5)["passed"] is False

    # research-006: irrelevant R006-A:3 placed in comparability_evidence
    bad6 = copy.deepcopy(tasks["research-006"]["expected"])
    bad6["comparability_evidence"].append("R006-A:3")
    assert grade_research_task(tasks["research-006"], bad6)["passed"] is False

    # research-008: duplicate R008-B included in evidence
    bad8 = copy.deepcopy(tasks["research-008"]["expected"])
    bad8["evidence"].append("R008-B:1")
    assert grade_research_task(tasks["research-008"], bad8)["passed"] is False

    # research-009: site B treated as 0 instead of null/UNVERIFIED, and bool rejected as int
    bad9 = copy.deepcopy(tasks["research-009"]["expected"])
    bad9["sites"][1]["failure_fraction"] = 0
    assert grade_research_task(tasks["research-009"], bad9)["passed"] is False
    bad9_bool = copy.deepcopy(tasks["research-009"]["expected"])
    bad9_bool["known_failures"] = True
    assert grade_research_task(tasks["research-009"], bad9_bool)["passed"] is False


def test_all_python_tasks_fail_starter_and_pass_reference():
    tasks = _tasks_by_id()
    for i in range(1, 6):
        tid = f"python-{i:03d}"
        task = tasks[tid]
        starter = grade_python_task(task, task["input"]["source_code"])
        assert starter["passed"] is False, tid
        assert "ModuleNotFoundError" not in starter["stderr"], tid
        res = grade_python_task(task, REFERENCE_CODE_SOLUTIONS[tid])
        assert res["passed"] is True, f"{tid}: {res}"


@pytest.mark.skipif(not _HAS_WEB_TSC, reason="web/node_modules/typescript not installed; run npm ci in web/")
def test_all_typescript_tasks_fail_starter_and_pass_reference():
    tasks = _tasks_by_id()
    for i in range(1, 6):
        tid = f"typescript-{i:03d}"
        task = tasks[tid]
        assert grade_typescript_task(task, task["input"]["source_code"])["passed"] is False, tid
        res = grade_typescript_task(task, REFERENCE_CODE_SOLUTIONS[tid])
        assert res["passed"] is True, f"{tid}: {res}"


def test_summarize_eval_records_metrics_variance_and_unknown_cost():
    records = [
        {"task_id": "research-001", "condition": "m1", "status": "completed", "passed": True,
         "input_tokens": 100, "output_tokens": 50, "cost_usd": None},
        {"task_id": "research-001", "condition": "m1", "status": "completed", "passed": False,
         "input_tokens": 120, "output_tokens": 60, "cost_usd": None},
        {"task_id": "python-001", "condition": "m1", "status": "completed", "passed": True,
         "review_status": "INVALID_OUTPUT", "input_tokens": 80, "output_tokens": 40, "cost_usd": 0.01},
        {"task_id": "typescript-004", "condition": "m1", "status": "uncertain", "passed": False,
         "input_tokens": 50, "output_tokens": 10, "cost_usd": 0.01},
    ]
    summary = summarize_eval_records(records)
    overall = summary["overall"]
    assert overall["total"] == 4
    assert overall["resolved"] == 1
    assert overall["delivered_unreviewed"] == 1
    assert overall["failed"] == 1
    assert overall["uncertain_or_skipped"] == 1
    assert overall["pass_rate"] == pytest.approx(0.25)
    assert overall["repeat_variance"]["by_group"]["research-001::m1"] == pytest.approx(0.25)
    assert overall["repeat_variance"]["mean"] == pytest.approx(0.25)
    assert overall["total_input_tokens"] == 350
    assert overall["total_output_tokens"] == 160
    assert overall["cost_usd"] is None

    assert summary["by_split"]["A"]["total"] == 3
    assert summary["by_split"]["C"]["total"] == 1
    assert summary["by_category"]["research"]["total"] == 2
    assert summary["by_category"]["python"]["delivered_unreviewed"] == 1
    assert summary["by_category"]["typescript"]["uncertain_or_skipped"] == 1


@pytest.mark.skipif(not _HAS_WEB_TSC, reason="web/node_modules/typescript not installed; run npm ci in web/")
def test_m5_evaluate_cli_selftest():
    script = ROOT / "scripts" / "m5_evaluate.py"
    self_proc = subprocess.run(
        [sys.executable, str(script), "--self-test"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert self_proc.returncode == 0, self_proc.stderr + self_proc.stdout
    self_data = json.loads(self_proc.stdout)
    assert self_data["passed"] is True
    assert self_data["starter_failures_verified"] == 10
    assert self_data["reference_passes_verified"] == 20
    assert self_data["total_checks"] == 30


def test_m5_evaluate_cli_plan_and_holdout_guard(tmp_path):
    script = ROOT / "scripts" / "m5_evaluate.py"
    plan_proc = subprocess.run(
        [sys.executable, str(script), "--plan"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert plan_proc.returncode == 0, plan_proc.stderr
    plan_data = json.loads(plan_proc.stdout)
    assert plan_data["total_tasks"] == 20
    assert set(plan_data["splits"]) == {"A", "B", "C"}

    score_jsonl = tmp_path / "holdout.jsonl"
    score_jsonl.write_text(
        json.dumps({"task_id": "python-005", "output": REFERENCE_CODE_SOLUTIONS["python-005"]}) + "\n",
        encoding="utf-8",
    )
    blocked = subprocess.run(
        [sys.executable, str(script), "--score-file", str(score_jsonl)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert blocked.returncode != 0
    assert "Holdout split C" in blocked.stderr

    allowed = subprocess.run(
        [sys.executable, str(script), "--score-file", str(score_jsonl), "--allow-holdout-c", "--frozen-candidate-id", "cand-v1"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert allowed.returncode == 0, allowed.stderr
    allowed_data = json.loads(allowed.stdout)
    assert allowed_data["summary"]["by_split"]["C"]["resolved"] == 1
