"""Offline metric contracts; no model, HTTP, sandbox or report-source mutation."""
import json
from copy import deepcopy

import pytest

from scripts import m4_evaluation_report as reporting


def fixture_report():
    tasks = {name: {"id": name, "split": split, "complexity": complexity}
             for name, split, complexity in (("formal-low", "formal", "low"),
                                              ("formal-high", "formal", "high"),
                                              ("dev-low", "development", "low"))}
    queue = [{"sequence": i + 1, "phase": task["split"], "task_id": task_id,
              "repetition": repetition, "condition": condition}
             for i, (task_id, task, repetition, condition) in enumerate(
                 (task_id, task, repetition, condition) for task_id, task in tasks.items()
                 for repetition in (1, 2, 3) for condition in reporting.CONDITIONS)]
    return {"session": "offline", "status": "running", "queue": queue, "records": []}, tasks


def record(entry, tasks, *, status="completed", passed=True, charge="2", wall=100, cost_status="COMPLETE"):
    return {**entry, "split": tasks[entry["task_id"]]["split"],
            "complexity": tasks[entry["task_id"]]["complexity"], "run_id": f"run-{entry['sequence']}",
            "run_status": status, "resolved": status == "completed" and passed,
            "acceptance": {"status": "PASS" if passed else "FAIL", "seconds": 5,
                           "groups": [{"group": i, "status": "PASS" if passed or i == 1 else "FAIL"}
                                      for i in range(1, 6)]},
            "cost_estimate": {"status": cost_status, "reference_cost": charge if cost_status == "COMPLETE" else None,
                              "observed_reference_cost": charge, "unit": "site_reference_units",
                              "quote_version": "frozen-v1", "basis": "highest_band_no_cache_before_group"},
            "evidence_directory": f"evidence-{entry['sequence']}", "latency_seconds": wall,
            "timing": {"wall_seconds": wall, "summary_attempts": 0, "summary_recovery_attempts": 0},
            "model_calls": 2, "tool_calls": 1}


def cell(result, condition="Single-Pro", split="formal", level="low"):
    return next(x for x in result["splits"][split]["cells"]
                if x["condition"] == condition and x["complexity"] == level)


def test_formal_labels_come_from_frozen_models_development_ids_are_preserved(tmp_path):
    report, tasks = fixture_report()
    report["plan"] = {"strong_model_id": "a", "economy_model_id": "b"}
    report["formal_freeze"] = {"models": {"a": {"id": "a", "model_name": "hy3"},
                                          "b": {"id": "b", "model_name": "glm-5.3-flash"}}}
    original = deepcopy(report)
    result = reporting.aggregate_report(report, tasks, tmp_path)
    formal = result["splits"]["formal"]
    assert formal["condition_display_names"] == {
        "Single-Pro": "Single-hy3", "Single-Flash": "Single-glm-5.3-flash", "Full-Pro": "Full-hy3",
        "Compact-Pro": "Compact-hy3", "Compact-Mixed": "Compact-hy3+glm-5.3-flash"}
    assert cell(result)["condition_display"] == "Single-hy3"
    assert formal["slots"][0]["condition"] == "Single-Pro"
    assert formal["slots"][0]["condition_display"] == "Single-hy3"
    pair = formal["paired_by_complexity"][0]
    assert pair["baseline"] == "Single-Pro" and pair["baseline_display"] == "Single-hy3"
    dev = result["splits"]["development"]
    assert dev["condition_display_names"]["Single-Pro"] == "Single-Pro"
    markdown = reporting.render_markdown(result)
    formal_md, development_md = markdown.split("## 开发集：仅用于调试，不作正式结论")
    assert "Single-hy3" in formal_md and "Compact-hy3+glm-5.3-flash" in formal_md
    development_md = development_md.split("## 正式集同题同重复比较")[0]
    assert "Single-Pro" in development_md and "Single-hy3" not in development_md
    assert report == original


def test_formal_labels_prioritize_frozen_model_ids_over_plan(tmp_path):
    report, tasks = fixture_report()
    report["plan"] = {"strong_model_id": "old-a", "economy_model_id": "old-b"}
    report["formal_freeze"] = {"strong_model_id": "a", "economy_model_id": "b", "models": {
        "a": {"id": "a", "model_name": "hy3"}, "b": {"id": "b", "model_name": "glm-5.3-flash"},
        "old-a": {"id": "old-a", "model_name": "deepseek-v4-pro"},
        "old-b": {"id": "old-b", "model_name": "deepseek-flash"}}}
    names = reporting.aggregate_report(report, tasks, tmp_path)["splits"]["formal"]["condition_display_names"]
    assert names["Single-Pro"] == "Single-hy3" and names["Single-Flash"] == "Single-glm-5.3-flash"
    del report["plan"]
    names = reporting.aggregate_report(report, tasks, tmp_path)["splits"]["formal"]["condition_display_names"]
    assert names["Single-Pro"] == "Single-hy3" and names["Single-Flash"] == "Single-glm-5.3-flash"
    del report["formal_freeze"]["economy_model_id"]
    names = reporting.aggregate_report(report, tasks, tmp_path)["splits"]["formal"]["condition_display_names"]
    assert names["Single-Flash"].startswith("模型信息未知")
    assert names["Compact-Mixed"].startswith("模型信息未知")


def test_unfrozen_or_missing_model_name_never_claims_a_model(tmp_path):
    report, tasks = fixture_report()
    report["plan"] = {"strong_model_id": "a", "economy_model_id": "b"}
    report["models"] = {"a": {"id": "a", "model_name": "deepseek-v4-pro"}}
    result = reporting.aggregate_report(report, tasks, tmp_path)
    assert result["splits"]["formal"]["condition_display_names"]["Single-Pro"] == "待冻结（Single-Pro）"
    report["formal_freeze"] = {"models": {"a": {"id": "different", "model_name": "deepseek-v4-pro"},
                                          "b": {"id": "b", "model_name": "unsafe|name"}}}
    names = reporting.aggregate_report(report, tasks, tmp_path)["splits"]["formal"]["condition_display_names"]
    assert all(name.startswith("模型信息未知") for name in names.values())


def test_retains_every_queue_slot_and_separates_development(tmp_path):
    report, tasks = fixture_report()
    entry = next(e for e in report["queue"] if e["task_id"] == "dev-low")
    report["records"] = [record(entry, tasks)]
    result = reporting.aggregate_report(report, tasks, tmp_path)
    assert result["formal_status"] == "NOT_STARTED"
    assert len(result["splits"]["formal"]["cells"]) == 15
    assert len(result["splits"]["formal"]["slots"]) == 30
    assert result["splits"]["formal"]["total"]["n"] == 0
    assert result["splits"]["development"]["total"]["resolved"] == 1
    assert "正式集尚未运行" in reporting.render_markdown(result)


def test_interrupted_wall_and_unfinished_model_intervals_are_unknown_not_zero(tmp_path):
    report, tasks = fixture_report()
    entry = next(e for e in report["queue"] if e["task_id"] == "dev-low")
    row = record(entry, tasks, status="cancelled", passed=False, wall=50000)
    row.update(elapsed_time_valid=False, elapsed_time_invalid_reason="cross-restart-cancel",
               end_to_end_seconds=60000)
    row["timing"].update(model_boundaries={"union_seconds": 49990, "complete": True},
                         tool_boundaries={"union_seconds": 2, "complete": True},
                         stages={"execute": {"union_seconds": 49990, "complete": True}})
    report["records"] = [row]
    original = deepcopy(report)
    directory = tmp_path / row["evidence_directory"]
    directory.mkdir()
    operations = [{"purpose": "context_summary", "operation_kind": "model_calls", "status": "uncertain",
                   "started_at": "2026-11-09T00:00:00+00:00", "finished_at": "2026-11-10T00:00:00+00:00"}]
    (directory / "operations.json").write_text(json.dumps(operations), encoding="utf-8")
    result = reporting.aggregate_report(report, tasks, tmp_path)
    slot = next(s for s in result["splits"]["development"]["slots"] if s["recorded"])
    assert slot["wall_seconds"] is None and slot["end_to_end_seconds"] is None
    assert slot["audit_original_latency_seconds"] == 50000
    assert slot["audit_original_timing"]["model_boundaries"]["union_seconds"] == 49990
    aggregate = cell(result, split="development")
    assert aggregate["all_attempt_wall_seconds"]["total"] is None
    assert aggregate["all_attempt_wall_seconds"]["unknown"] == 1
    assert aggregate["model_union_seconds"]["total"] is None
    assert aggregate["stage_model_union_seconds"]["execute"]["total"] is None
    assert aggregate["summary_union_seconds"]["total"] is None
    assert aggregate["tool_union_seconds"]["total"] == 2
    assert "序号 " + str(entry["sequence"]) + " 的运行跨重启中断" in reporting.render_markdown(result)
    assert report == original


def test_failed_costs_included_and_fast_failure_never_wins(tmp_path):
    report, tasks = fixture_report()
    for entry in report["queue"]:
        if entry["task_id"] == "formal-low" and entry["condition"] == "Single-Pro":
            report["records"].append(record(entry, tasks, passed=entry["repetition"] == 1))
        if entry["task_id"] == "formal-low" and entry["condition"] == "Compact-Mixed" and entry["repetition"] == 1:
            report["records"].append(record(entry, tasks, status="failed", passed=True, charge="0.1", wall=1))
    result = reporting.aggregate_report(report, tasks, tmp_path)
    aggregate = cell(result)
    assert aggregate["n"] == 3 and aggregate["resolved"] == 1
    assert aggregate["cost"]["all_attempt_reference_cost"] == "6"
    assert aggregate["cost"]["cost_per_resolved_including_failures"] == "6"
    assert aggregate["five_group_mean"] == pytest.approx((1 + .2 + .2) / 3)
    pair = next(p for p in result["splits"]["formal"]["pairs"]
                if p["task_id"] == "formal-low" and p["repetition"] == 1
                and p["baseline"] == "Single-Pro" and p["treatment"] == "Compact-Mixed")
    assert pair["outcome"] == "QUALITY_LOSS" and pair["advantage"] is None
    assert pair["equal_success_wall_ratio"] is None
    assert cell(result, "Compact-Mixed")["hidden_passed"] == 1
    assert cell(result, "Compact-Mixed")["resolved"] == 0


def test_partial_and_unknown_costs_never_become_zero_or_exact_total(tmp_path):
    report, tasks = fixture_report()
    rows = [e for e in report["queue"] if e["task_id"] == "formal-low" and e["condition"] == "Single-Pro"]
    report["records"] = [record(rows[0], tasks, charge="1.2"),
                         record(rows[1], tasks, charge="0.3", cost_status="PARTIAL"),
                         record(rows[2], tasks, charge=None, cost_status="UNKNOWN")]
    aggregate = cell(reporting.aggregate_report(report, tasks, tmp_path))
    assert aggregate["cost"]["status"] == "PARTIAL"
    assert aggregate["cost"]["observed_reference_cost"] == "1.5"
    assert aggregate["cost"]["all_attempt_reference_cost"] is None
    assert aggregate["cost"]["cost_per_resolved_including_failures"] is None
    assert [aggregate["cost"][k] for k in ("complete_records", "partial_records", "unknown_records")] == [1, 1, 1]
    assert reporting.cost({})["observed_reference_cost"] is None


def test_missing_groups_are_unknown_not_perfect_or_zero(tmp_path):
    report, tasks = fixture_report()
    row = record(report["queue"][0], tasks, passed=False)
    row["acceptance"]["groups"] = [{"group": 1, "status": "PASS"}]
    report["records"] = [row]
    aggregate = cell(reporting.aggregate_report(report, tasks, tmp_path))
    assert aggregate["five_group_mean"] is None
    assert aggregate["five_group_unknown"] == 1
    assert aggregate["groups"]["1"] == {"pass": 1, "known": 1}
    assert aggregate["groups"]["2"] == {"pass": 0, "known": 0}


def test_summary_timing_reuses_union_and_recovery_is_subset():
    def operation(start, end, recovery=None):
        return {"purpose": "context_summary", "operation_kind": "model_calls", "recovery_of": recovery,
                "started_at": f"2026-11-09T00:00:{start:02d}+00:00",
                "finished_at": f"2026-11-09T00:00:{end:02d}+00:00"}
    result = reporting.summary_timing([operation(0, 10), operation(5, 15, "first")])
    assert result["summary"]["calls"] == 2
    assert result["summary"]["union_seconds"] == 15
    assert result["summary"]["summed_seconds"] == 20
    assert result["recovery"]["union_seconds"] == 10


def test_pairs_match_task_and_repeat_and_show_cost_time_tradeoff(tmp_path):
    report, tasks = fixture_report()
    for entry in report["queue"]:
        if entry["task_id"] == "formal-low" and entry["repetition"] == 1:
            if entry["condition"] in {"Single-Pro", "Compact-Mixed"}:
                mixed = entry["condition"] == "Compact-Mixed"
                report["records"].append(record(entry, tasks, charge="1" if mixed else "2", wall=200 if mixed else 100))
        if entry["task_id"] == "formal-low" and entry["repetition"] == 2 and entry["condition"] == "Single-Pro":
            report["records"].append(record(entry, tasks))
    result = reporting.aggregate_report(report, tasks, tmp_path)
    pairs = [p for p in result["splits"]["formal"]["pairs"] if p["task_id"] == "formal-low"
             and p["baseline"] == "Single-Pro" and p["treatment"] == "Compact-Mixed"]
    assert len(pairs) == 3 and sum(p["complete_pair"] for p in pairs) == 1
    assert pairs[0]["advantage"] and pairs[0]["slower"] and not pairs[0]["more_expensive"]
    assert pairs[0]["equal_success_cost_ratio"] == "0.5"
    assert pairs[0]["equal_success_wall_ratio"] == 2
    assert pairs[1]["outcome"] == "INCOMPLETE_PAIR"


def test_started_but_unrecorded_cost_cannot_be_omitted(tmp_path):
    report, tasks = fixture_report()
    first = report["queue"][0]
    report["records"] = [record(first, tasks)]
    second = next(e for e in report["queue"] if e["task_id"] == "formal-low"
                  and e["condition"] == "Single-Pro" and e["repetition"] == 2)
    directory = tmp_path / f"{second['sequence']:03d}-{second['task_id']}-{second['condition']}-r2"
    directory.mkdir()
    (directory / "trial-state.json").write_text('{"run_id":"unrecorded","stage":"submitted"}')
    result = reporting.aggregate_report(report, tasks, tmp_path)
    aggregate = cell(result)
    assert aggregate["unrecorded_started"] == 1
    assert aggregate["not_run"] == 1
    assert aggregate["cost"]["status"] == "PARTIAL"
    assert aggregate["cost"]["all_attempt_reference_cost"] is None


def test_rejects_duplicate_slots_and_source_metadata_mismatch(tmp_path):
    report, tasks = fixture_report()
    report["records"] = [record(report["queue"][0], tasks)]
    duplicate = deepcopy(report)
    duplicate["records"] *= 2
    with pytest.raises(ValueError, match="Duplicate"):
        reporting.aggregate_report(duplicate, tasks, tmp_path)
    report["records"][0]["complexity"] = "high"
    with pytest.raises(ValueError, match="metadata"):
        reporting.aggregate_report(report, tasks, tmp_path)


@pytest.mark.parametrize("key,value", [
    ("unit", "other_units"), ("quote_version", "other_quote"), ("basis", "discounted"),
    ("unit", None), ("quote_version", None), ("basis", None),
])
def test_incomparable_prices_do_not_create_cost_ratio_or_aggregate_total(tmp_path, key, value):
    report, tasks = fixture_report()
    for entry in report["queue"]:
        if entry["task_id"] == "formal-low" and entry["repetition"] == 1 and entry["condition"] in {
            "Single-Pro", "Compact-Mixed"
        }:
            row = record(entry, tasks, charge="1" if entry["condition"] == "Compact-Mixed" else "2")
            if entry["condition"] == "Compact-Mixed":
                row["cost_estimate"][key] = value
            report["records"].append(row)
    result = reporting.aggregate_report(report, tasks, tmp_path)
    pair = next(p for p in result["splits"]["formal"]["pairs"] if p["task_id"] == "formal-low"
                and p["repetition"] == 1 and p["baseline"] == "Single-Pro" and p["treatment"] == "Compact-Mixed")
    assert pair["cost_basis_comparable"] is False
    assert pair["equal_success_cost_ratio"] is None and pair["advantage"] is False
    assert pair["equal_success_wall_ratio"] == 1
    assert result["splits"]["formal"]["total"]["cost"]["observed_reference_cost"] is None


def test_semantic_queue_duplicates_cannot_silently_replace_pairs(tmp_path):
    report, tasks = fixture_report()
    report["queue"].append({**report["queue"][0], "sequence": 999})
    with pytest.raises(ValueError, match="Duplicate task/repetition/condition"):
        reporting.aggregate_report(report, tasks, tmp_path)


@pytest.mark.parametrize("pending", [False, True])
def test_duplicate_run_ids_cannot_count_as_independent_trials(tmp_path, pending):
    report, tasks = fixture_report()
    first, second = report["queue"][:2]
    report["records"] = [record(first, tasks)]
    run_id = report["records"][0]["run_id"]
    if pending:
        directory = tmp_path / f"{second['sequence']:03d}-{second['task_id']}-{second['condition']}-r1"
        directory.mkdir()
        (directory / "trial-state.json").write_text(json.dumps({"run_id": run_id, "stage": "submitted"}))
    else:
        second_record = record(second, tasks)
        second_record["run_id"] = run_id
        report["records"].append(second_record)
    with pytest.raises(ValueError, match="Duplicate run_id"):
        reporting.aggregate_report(report, tasks, tmp_path)


def test_writer_is_repeatable_and_preserves_source(tmp_path, monkeypatch):
    report, tasks = fixture_report()
    report.update(manifest_sha256="fixture", plan={"output_root": str(tmp_path)})
    root = tmp_path / "session"
    root.mkdir()
    source = root / "report.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    original = source.read_bytes()
    monkeypatch.setattr(reporting, "load_optimization_tasks", lambda: ({}, tasks, "fixture"))
    outputs = reporting.write_report(source)
    first = [p.read_bytes() for p in outputs]
    reporting.write_report(source)
    assert [p.read_bytes() for p in outputs] == first
    assert source.read_bytes() == original
    assert json.loads(outputs[0].read_text(encoding="utf-8"))["formal_status"] == "NOT_STARTED"
