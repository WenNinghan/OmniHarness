"""Read-only M4.1 aggregation; never contacts an API or recalculates provider prices.

Run: python -m scripts.m4_evaluation_report PATH/TO/report.json
Writes aggregate-report.json and aggregate-report.md beside the original report.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, median

from scripts.m4_evaluate import CONDITIONS, interval_metrics, load_optimization_tasks

COMPLEXITIES = ("low", "medium", "high")
LABELS = {"low": "低", "medium": "中", "high": "高"}
COMPARISONS = tuple((single, multi) for single in CONDITIONS[:2] for multi in CONDITIONS[2:]) + (
    ("Full-Pro", "Compact-Pro"), ("Compact-Pro", "Compact-Mixed"))


def formal_condition_names(report):
    """Use the frozen model names; old comparison IDs remain unchanged."""
    freeze = report.get("formal_freeze")
    plan = report.get("plan")
    if not isinstance(freeze, dict) or not isinstance(freeze.get("models"), dict):
        return {condition: f"待冻结（{condition}）" for condition in CONDITIONS}
    plan = plan if isinstance(plan, dict) else {}
    models = freeze["models"]

    has_frozen_assignments = "strong_model_id" in freeze or "economy_model_id" in freeze

    def name(key):
        identity = freeze.get(key) if has_frozen_assignments else plan.get(key)
        model = models.get(identity) if isinstance(identity, str) else None
        if not isinstance(model, dict) or model.get("id") != identity:
            return None
        value = model.get("model_name")
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value) else None

    strong, economy = name("strong_model_id"), name("economy_model_id")
    def label(prefix, model, condition):
        return f"{prefix}-{model}" if model else f"模型信息未知（{condition}）"

    return {"Single-Pro": label("Single", strong, "Single-Pro"),
            "Single-Flash": label("Single", economy, "Single-Flash"),
            "Full-Pro": label("Full", strong, "Full-Pro"),
            "Compact-Pro": label("Compact", strong, "Compact-Pro"),
            "Compact-Mixed": f"Compact-{strong}+{economy}" if strong and economy else "模型信息未知（Compact-Mixed）"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def money(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def distribution(values):
    values = [v for v in (number(v) for v in values) if v is not None]
    return {"n": len(values), "median": median(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def totals(values):
    values = [number(v) for v in values]
    observed = [v for v in values if v is not None]
    return {"total": sum(observed) if values and len(observed) == len(values) else None,
            "observed_total": sum(observed) if observed else None,
            "known": len(observed), "unknown": len(values) - len(observed)}


def quality(record):
    acceptance = record.get("acceptance") or {}
    groups = acceptance.get("groups") or []
    valid = (len(groups) == 5 and {g.get("group") for g in groups} == set(range(1, 6))
             and all(g.get("status") in {"PASS", "FAIL"} for g in groups))
    scores = {str(i): next((int(g["status"] == "PASS") for g in groups
                           if g.get("group") == i and g.get("status") in {"PASS", "FAIL"}), None)
              for i in range(1, 6)}
    completed = record.get("run_status") == "completed"
    passed = acceptance.get("status") == "PASS"
    return {"workflow_completed": completed, "hidden_passed": passed,
            "resolved": completed and passed, "group_scores": scores,
            "group_score": sum(scores.values()) / 5 if valid else None,
            "typecheck": acceptance.get("typescript_typecheck", "UNVERIFIED")}


def cost(record):
    value = record.get("cost_estimate") or {}
    exact = money(value.get("reference_cost")) if value.get("status") == "COMPLETE" else None
    observed = exact if exact is not None else money(value.get("observed_reference_cost"))
    status = "COMPLETE" if exact is not None else "PARTIAL" if observed is not None else "UNKNOWN"
    return {"status": status, "reference_cost": str(exact) if exact is not None else None,
            "observed_reference_cost": str(observed) if observed is not None else None,
            "unit": value.get("unit"), "quote_version": value.get("quote_version"),
            "basis": value.get("basis"), "billed_cost": None}


def cost_signature(value):
    signature = tuple(value.get(key) for key in ("unit", "quote_version", "basis"))
    return signature if all(isinstance(item, str) and item.strip() for item in signature) else None


def summary_timing(operations):
    """Reuse runner interval unions; recovery is a subset, never added twice."""
    summaries = [o for o in operations if o.get("purpose") == "context_summary" and
                 (o.get("operation_kind") == "model_calls" or
                  o.get("node_id", "").rsplit(":", 1)[-1] == "model_calls")]
    return {name: {"calls": len(rows), **interval_metrics(rows)} for name, rows in (
        ("summary", summaries), ("recovery", [o for o in summaries if o.get("recovery_of")]))}


def evidence_path(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Evidence directory is outside the report directory")
    return path


def make_slots(report, tasks, root):
    records = {r["sequence"]: r for r in report["records"]}
    queue = report["queue"]
    if len(records) != len(report["records"]) or len({q["sequence"] for q in queue}) != len(queue):
        raise ValueError("Duplicate sequence in queue or records")
    for rows in (queue, report["records"]):
        identities = {(r["task_id"], r["repetition"], r["condition"]) for r in rows}
        if len(identities) != len(rows):
            raise ValueError("Duplicate task/repetition/condition in queue or records")
    if set(records) - {q["sequence"] for q in queue}:
        raise ValueError("Result has no scheduled slot")
    slots = []
    for entry in queue:
        task = tasks[entry["task_id"]]
        if entry["condition"] not in CONDITIONS or task["complexity"] not in COMPLEXITIES:
            raise ValueError("Unknown comparison condition or complexity")
        record = records.get(entry["sequence"])
        label = f"{entry['sequence']:03d}-{entry['task_id']}-{entry['condition']}-r{entry['repetition']}"
        slot = {**entry, "split": task["split"], "complexity": task["complexity"], "recorded": record is not None}
        if record is None:
            state_path = evidence_path(root, label) / "trial-state.json"
            state = read_json(state_path) if state_path.exists() else {}
            started = bool(state.get("run_id") or state.get("stage") == "dispatch_intent")
            slot.update(state="UNRECORDED_STARTED" if started else
                        "NOT_RUN_BUDGET" if report.get("status") == "NOT_RUN_BUDGET" else "NOT_RUN",
                        run_id=state.get("run_id"), started=started)
            slots.append(slot)
            continue
        for key in ("task_id", "repetition", "condition"):
            if record[key] != entry[key]:
                raise ValueError("Recorded result does not match its scheduled slot")
        if record["split"] != task["split"] or record["complexity"] != task["complexity"]:
            raise ValueError("Recorded task metadata differs from the frozen manifest")
        original_timing = record.get("timing") or {}
        elapsed_valid = record.get("elapsed_time_valid") is not False
        timing = deepcopy(original_timing)
        if not elapsed_valid:
            # An interrupted operation's recorded end is reconciliation, not
            # completion. Neither its wall nor stage interval measures work.
            for key in ("wall_seconds", "queue_seconds", "execution_seconds"):
                timing[key] = None
            for boundary in (timing.get("model_boundaries"), *timing.get("stages", {}).values()):
                if isinstance(boundary, dict):
                    boundary.update(union_seconds=None, summed_seconds=None, complete=False)
        directory = evidence_path(root, record["evidence_directory"])
        operation_file = directory / "operations.json"
        operations = read_json(operation_file) if operation_file.exists() else None
        if operations is not None and not elapsed_valid:
            operations = [{**o, "finished_at": None} if o.get("status") != "completed" else o
                          for o in operations]
        overhead = summary_timing(operations) if operations is not None else None
        q = quality(record)
        if "resolved" in record and bool(record["resolved"]) != q["resolved"]:
            raise ValueError("Recorded resolution contradicts workflow and hidden acceptance")
        slot.update(state=record["run_status"], started=True, run_id=record["run_id"], **q,
                    implementation_id=record.get("implementation_id"), cost=cost(record),
                    wall_seconds=number(timing.get("wall_seconds", record.get("latency_seconds"))) if elapsed_valid else None,
                    end_to_end_seconds=number(record.get("end_to_end_seconds")) if elapsed_valid else None,
                    elapsed_time_valid=elapsed_valid,
                    elapsed_time_invalid_reason=record.get("elapsed_time_invalid_reason") if not elapsed_valid else None,
                    audit_original_timing=original_timing if not elapsed_valid else None,
                    audit_original_latency_seconds=record.get("latency_seconds") if not elapsed_valid else None,
                    acceptance_seconds=number(record["acceptance"].get("seconds")),
                    model_calls=record.get("model_calls"), tool_calls=record.get("tool_calls"),
                    summary_attempts=timing.get("summary_attempts"),
                    recovery_attempts=timing.get("summary_recovery_attempts"),
                    summary_failed_attempts=timing.get("summary_failed_attempts"),
                    overhead=overhead, timing=timing, evidence_directory=record["evidence_directory"],
                    error_code=record.get("run_error_code"))
        slots.append(slot)
    run_ids = [s["run_id"] for s in slots if s.get("run_id")]
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("Duplicate run_id across scheduled slots")
    return slots


def aggregate_cost(rows, unrecorded):
    statuses = Counter(r["cost"]["status"] for r in rows)
    observed = [money(r["cost"]["observed_reference_cost"]) for r in rows]
    known = [v for v in observed if v is not None]
    units = sorted({r["cost"]["unit"] for r in rows if r["cost"]["unit"]})
    signatures = {cost_signature(r["cost"]) for r in rows}
    compatible = len(signatures) == 1 and None not in signatures
    complete = bool(rows) and statuses["COMPLETE"] == len(rows) and not unrecorded and compatible
    observed_total = sum(known, Decimal(0)) if known and compatible else None
    resolved = sum(r["resolved"] for r in rows)
    return {"status": "COMPLETE" if complete else "PARTIAL" if observed_total is not None else "UNKNOWN",
            "complete_records": statuses["COMPLETE"], "partial_records": statuses["PARTIAL"],
            "unknown_records": statuses["UNKNOWN"], "unrecorded_started": unrecorded,
            "unit": units[0] if len(units) == 1 else None,
            "all_attempt_reference_cost": str(observed_total) if complete else None,
            "observed_reference_cost": str(observed_total) if observed_total is not None else None,
            "cost_per_resolved_including_failures": str(observed_total / resolved) if complete and resolved else None,
            "billed_cost": None}


def aggregate_cell(slots):
    rows = [s for s in slots if s["recorded"]]
    resolved = [r for r in rows if r["resolved"]]
    by_task = {}
    for row in rows:
        by_task.setdefault(row["task_id"], []).append(row)
    stages = sorted({name for r in rows for name in r["timing"].get("stages", {})})
    unrecorded = sum(s["state"] == "UNRECORDED_STARTED" for s in slots)
    result = {"planned": len(slots), "n": len(rows), "not_run": sum(not s["started"] for s in slots),
              "unrecorded_started": unrecorded, "represented_tasks": len(by_task),
              "workflow_completed": sum(r["workflow_completed"] for r in rows),
              "hidden_passed": sum(r["hidden_passed"] for r in rows), "resolved": len(resolved),
              "observed_resolution_rate": len(resolved) / len(rows) if rows else None,
              "task_equal_resolution_rate": mean(mean(r["resolved"] for r in task) for task in by_task.values())
                  if by_task else None,
              "groups": {str(i): {"pass": sum(r["group_scores"][str(i)] == 1 for r in rows),
                                   "known": sum(r["group_scores"][str(i)] is not None for r in rows)}
                         for i in range(1, 6)},
              "five_group_mean": mean(r["group_score"] for r in rows if r["group_score"] is not None)
                  if any(r["group_score"] is not None for r in rows) else None,
              "five_group_unknown": sum(r["group_score"] is None for r in rows),
              "typecheck": dict(Counter(r["typecheck"] for r in rows)),
              "cost": aggregate_cost(rows, unrecorded),
              "success_wall_seconds": distribution(r["wall_seconds"] for r in resolved),
              "success_end_to_end_seconds": distribution(r["end_to_end_seconds"] for r in resolved),
              "failed_wall_seconds": totals(r["wall_seconds"] for r in rows if not r["resolved"]),
              "all_attempt_wall_seconds": totals(r["wall_seconds"] for r in rows),
              "all_attempt_model_calls": totals(r["model_calls"] for r in rows),
              "all_attempt_tool_calls": totals(r["tool_calls"] for r in rows),
              "summary_calls": totals(r["summary_attempts"] for r in rows),
              "recovery_calls": totals(r["recovery_attempts"] for r in rows),
              "summary_failed_calls": totals(r["summary_failed_attempts"] for r in rows),
              "recovered_successes": sum(bool(r["recovery_attempts"]) for r in resolved),
              "acceptance_seconds": totals(r["acceptance_seconds"] for r in rows),
              "stage_model_union_seconds": {stage: totals(
                  r["timing"].get("stages", {}).get(stage, {}).get("union_seconds") for r in rows)
                  for stage in stages}}
    for kind in ("summary", "recovery"):
        result[kind + "_union_seconds"] = totals(
            r["overhead"][kind]["union_seconds"] if r["overhead"] and r["overhead"][kind]["complete"] else None
            for r in rows)
    for kind in ("model", "tool"):
        result[kind + "_union_seconds"] = totals(
            r["timing"].get(kind + "_boundaries", {}).get("union_seconds")
            if r["timing"].get(kind + "_boundaries", {}).get("complete") else None for r in rows)
    return result


def paired_comparisons(slots):
    blocks = {}
    for slot in slots:
        blocks.setdefault((slot["task_id"], slot["repetition"]), {})[slot["condition"]] = slot
    pairs = []
    for (task, repetition), block in sorted(blocks.items()):
        for baseline, treatment in COMPARISONS:
            left, right = block.get(baseline), block.get(treatment)
            pair = {"task_id": task, "repetition": repetition, "baseline": baseline, "treatment": treatment,
                    "complexity": next(iter(block.values()))["complexity"], "complete_pair": False,
                    "equal_success_wall_ratio": None, "equal_success_cost_ratio": None, "advantage": None}
            if left and right and left["recorded"] and right["recorded"]:
                pair.update(complete_pair=True, baseline_resolved=left["resolved"], treatment_resolved=right["resolved"],
                            baseline_group_score=left["group_score"], treatment_group_score=right["group_score"])
                if left["resolved"] and right["resolved"]:
                    a, b = left["wall_seconds"], right["wall_seconds"]
                    wall = b / a if a is not None and b is not None and a > 0 else None
                    a, b = (money(r["cost"]["reference_cost"]) for r in (left, right))
                    signature = cost_signature(left["cost"])
                    comparable = signature is not None and signature == cost_signature(right["cost"])
                    charge = b / a if comparable and a is not None and b is not None and a > 0 else None
                    pair.update(equal_success_wall_ratio=wall,
                                cost_basis_comparable=comparable,
                                equal_success_cost_ratio=str(charge) if charge is not None else None,
                                advantage=(wall < 1 if wall is not None else False) or
                                          (charge < 1 if charge is not None else False),
                                slower=wall > 1 if wall is not None else None,
                                more_expensive=charge > 1 if charge is not None else None)
                    pair["outcome"] = "EFFICIENCY_GAIN" if pair["advantage"] else (
                        "NO_EFFICIENCY_GAIN" if wall is not None and charge is not None else "INSUFFICIENT_METRICS")
                else:
                    pair["outcome"] = ("QUALITY_GAIN" if right["resolved"] else
                                       "QUALITY_LOSS" if left["resolved"] else "BOTH_UNRESOLVED")
            else:
                pair["outcome"] = "INCOMPLETE_PAIR"
            pairs.append(pair)
    return pairs


def pair_summary(pairs):
    both = [p for p in pairs if p.get("baseline_resolved") and p.get("treatment_resolved")]
    return {"planned_pairs": len(pairs), "complete_pairs": sum(p["complete_pair"] for p in pairs),
            "both_resolved": len(both), "outcomes": dict(Counter(p["outcome"] for p in pairs)),
            "efficiency_gains": sum(p["advantage"] is True for p in pairs),
            "gains_with_slower_time": sum(p["advantage"] is True and p.get("slower") is True for p in pairs),
            "gains_with_higher_cost": sum(p["advantage"] is True and p.get("more_expensive") is True for p in pairs),
            "equal_success_wall_ratio": distribution(p["equal_success_wall_ratio"] for p in both),
            "equal_success_cost_ratio": distribution(p["equal_success_cost_ratio"] for p in both)}


def aggregate_report(report, tasks, root):
    slots = make_slots(report, tasks, root)
    splits = {}
    for split in ("formal", "development"):
        selected = [s for s in slots if s["split"] == split]
        pairs = paired_comparisons(selected)
        names = formal_condition_names(report) if split == "formal" else {x: x for x in CONDITIONS}
        for slot in selected:
            slot["condition_display"] = names[slot["condition"]]
        for pair in pairs:
            pair["baseline_display"], pair["treatment_display"] = names[pair["baseline"]], names[pair["treatment"]]
        cells = [{"complexity": level, "condition": condition, **aggregate_cell(
            [s for s in selected if s["complexity"] == level and s["condition"] == condition]),
            "condition_display": names[condition]}
            for level in COMPLEXITIES for condition in CONDITIONS]
        splits[split] = {"planned": len(selected), "recorded": sum(s["recorded"] for s in selected),
            "condition_display_names": names, "cells": cells, "total": aggregate_cell(selected), "slots": selected, "pairs": pairs,
            "paired_by_complexity": [{"complexity": level, "baseline": baseline, "treatment": treatment,
                "baseline_display": names[baseline], "treatment_display": names[treatment],
                **pair_summary([p for p in pairs if p["complexity"] == level and
                                p["baseline"] == baseline and p["treatment"] == treatment])}
                for level in COMPLEXITIES for baseline, treatment in COMPARISONS],
            "by_task": [{"task_id": task_id, "condition": condition, "condition_display": names[condition], **aggregate_cell(
                [s for s in selected if s["task_id"] == task_id and s["condition"] == condition])}
                for task_id in sorted({s["task_id"] for s in selected}) for condition in CONDITIONS]}
    formal = splits["formal"]
    return {"schema_version": 1, "session": report["session"], "source_status": report["status"],
            "dataset_id": report.get("dataset_id"), "manifest_sha256": report.get("manifest_sha256"),
            "formal_status": "NOT_STARTED" if not formal["recorded"] else
                             "COMPLETE" if formal["recorded"] == formal["planned"] else "INCOMPLETE",
            "formal_freeze": report.get("formal_freeze"), "budget": report.get("budget"),
            "quote": {k: report.get("quote_snapshot", {}).get(k) for k in ("version", "source", "observed_at")},
            "splits": splits, "billed_cost": None,
            "inference_scope": "六个正式任务的探索性结果；重复不是独立任务；开发集不用于正式优势结论。",
            "metric_notes": ["成功要求流程 completed 且独立隐藏验收 PASS；失败和未运行格全部保留。",
                "费用复用已记录的最高档参考价，不重新抓价；PARTIAL 仅显示已知部分，实扣未知。",
                "单位成功成本包含该格所有已记录失败的费用；有缺价或未完成记录时不计算精确值。",
                "成功墙钟是提交至 Run 终态；含独立验收的端到端时间另列于 JSON。",
                "摘要包含恢复；恢复时间不能再次加到摘要总时间。阶段/模型/工具区间并集彼此可能重叠。",
                "成对效率比较只在同题同重复双方成功时进行，失败早结束不视为速度优势。",
                "需求组均分使用完整五组结果，缺失不记零；每组通过数及已知分母单独保存。",
                "全批预算包含探针和未核实预留，不等于实际扣款；此表成本覆盖已记录评测 Run。"]}


def fmt(value, digits=3):
    return "未知" if value is None else f"{float(value):.{digits}f}"


def range_text(value, digits=1):
    return "—" if not value["n"] else (
        f"{fmt(value['median'], digits)} [{fmt(value['min'], digits)}, {fmt(value['max'], digits)}]")


def render_markdown(result):
    formal = result["splits"]["formal"]
    lines = ["# M4.1 多模型公平对比", "", f"会话：`{result['session']}`。原始批次状态：`{result['source_status']}`。", "",
             "正式集尚未运行，当前不能判断正式任务上是否存在优势。" if not formal["recorded"] else
             f"正式集已记录 {formal['recorded']}/{formal['planned']}；" +
             ("以下为完整探索性结果。" if result["formal_status"] == "COMPLETE" else "尚未完成，不作最终优势结论。"), "",
             "所有费用均为中转站最高档参考单位，未乘账户组、未算缓存折扣，实扣未知。", ""]
    invalid_elapsed = [s for split in result["splits"].values() for s in split["slots"]
                       if s.get("elapsed_time_valid") is False]
    if invalid_elapsed:
        numbers = "、".join(str(s["sequence"]) for s in invalid_elapsed)
        lines += [f"序号 {numbers} 的运行跨重启中断，其墙钟和受影响的模型区间耗时未知，不用于性能比较；"
                  "原时间值保存在 JSON 中供审计。", ""]
    for split, title in (("formal", "正式集：按复杂度与条件"), ("development", "开发集：仅用于调试，不作正式结论")):
        section = result["splits"][split]
        total = section["total"]
        lines += [f"## {title}", "",
                  f"已记录 {total['n']}/{total['planned']}，已启动未入结果 {total['unrecorded_started']}，"
                  f"未运行 {total['not_run']}；双条件成功 {total['resolved']}。"
                  f"已记录 Run 的参考费用已知部分合计 {fmt(total['cost']['observed_reference_cost'], 6)}，"
                  f"完整性 {total['cost']['status']}；模型调用 {fmt(total['all_attempt_model_calls']['total'], 0)}。", "",
                  "五组均分为完整需求组平均通过比例；费用中的“已知”不是总费用。", "",
                  "| 复杂度 | 条件 | n/计划 | 流程完成 | 隐藏全过 | 双条件成功 | 五组均分 | 成本完整/部分/未知 | 总参考成本 | 单位成功成本 | 成功墙钟秒：中位 [范围] |",
                  "|---|---|---:|---:|---:|---:|---:|---|---:|---:|---| "]
        for cell in section["cells"]:
            charge = cell["cost"]
            total = fmt(charge["all_attempt_reference_cost"], 6) if charge["status"] == "COMPLETE" else (
                "已知 " + fmt(charge["observed_reference_cost"], 6) if charge["observed_reference_cost"] is not None else "未知")
            score = "未知" if cell["five_group_mean"] is None else f"{cell['five_group_mean']:.0%}"
            lines.append(f"| {LABELS[cell['complexity']]} | {cell['condition_display']} | {cell['n']}/{cell['planned']} | "
                         f"{cell['workflow_completed']} | {cell['hidden_passed']} | {cell['resolved']} | {score} | "
                         f"{charge['complete_records']}/{charge['partial_records']}/{charge['unknown_records']} | "
                         f"{total} | {fmt(charge['cost_per_resolved_including_failures'], 6)} | {range_text(cell['success_wall_seconds'])} |")
        lines += ["", "### 开销分解（含失败；恢复属于摘要的子集）", "",
                  "| 复杂度 | 条件 | 模型/工具调用 | 摘要/恢复调用 | 摘要/恢复秒 | 模型/工具区间并集秒 | 全部 Run 墙钟秒 | 独立验收秒 |",
                  "|---|---|---:|---:|---:|---:|---:|---:|"]
        for cell in section["cells"]:
            def total(key):
                value = cell[key]
                return fmt(value["total"], 1) if value["total"] is not None else (
                    "已知 " + fmt(value["observed_total"], 1) if value["observed_total"] is not None else "未知")
            lines.append(f"| {LABELS[cell['complexity']]} | {cell['condition_display']} | "
                         f"{total('all_attempt_model_calls')}/{total('all_attempt_tool_calls')} | "
                         f"{total('summary_calls')}/{total('recovery_calls')} | "
                         f"{total('summary_union_seconds')}/{total('recovery_union_seconds')} | "
                         f"{total('model_union_seconds')}/{total('tool_union_seconds')} | "
                         f"{total('all_attempt_wall_seconds')} | {total('acceptance_seconds')} |")
        lines += ["", "### 每格状态（含未运行与失败）", "",
                  "| 序号 | 任务 | 重复 | 条件 | 状态 | 双条件成功 | 错误 |",
                  "|---:|---|---:|---|---|---|---|"]
        for slot in section["slots"]:
            lines.append(f"| {slot['sequence']} | {slot['task_id']} | {slot['repetition']} | {slot['condition_display']} | "
                         f"{slot['state']} | {'是' if slot.get('resolved') else '否' if slot['recorded'] else '未运行/未入结果'} | "
                         f"{slot.get('error_code') or '—'} |")
        lines.append("")
    lines += ["## 正式集同题同重复比较", "", "比值为后者/前者，低于 1 表示后者较低。只在双方成功时比较费用/速度；质量赢/输为双条件成功变化。", "",
              "| 复杂度 | 前者 → 后者 | 已记录对/计划 | 双方成功 | 质量赢/输 | 效率改善对 | 其中更慢/更贵 | 费用比中位 [范围] | 耗时比中位 [范围] |",
              "|---|---|---:|---:|---:|---:|---:|---|---|"]
    for pair in formal["paired_by_complexity"]:
        outcomes = pair["outcomes"]
        lines.append(f"| {LABELS[pair['complexity']]} | {pair['baseline_display']} → {pair['treatment_display']} | "
                     f"{pair['complete_pairs']}/{pair['planned_pairs']} | {pair['both_resolved']} | "
                     f"{outcomes.get('QUALITY_GAIN', 0)}/{outcomes.get('QUALITY_LOSS', 0)} | {pair['efficiency_gains']} | "
                     f"{pair['gains_with_slower_time']}/{pair['gains_with_higher_cost']} | "
                     f"{range_text(pair['equal_success_cost_ratio'], 3)} | {range_text(pair['equal_success_wall_ratio'], 3)} |")
    lines += ["", "## 口径与限制", ""] + ["- " + note for note in result["metric_notes"]]
    lines += ["", result["inference_scope"], "", "逐题三次重复、五需求组分项、阶段区间及完整成对记录见同目录 `aggregate-report.json`。", ""]
    return "\n".join(lines)


def write_report(source):
    source = Path(source).resolve()
    report = read_json(source)
    _, tasks, manifest_sha = load_optimization_tasks()
    if report.get("manifest_sha256") != manifest_sha:
        raise ValueError("Report does not match the frozen task manifest")
    root = source.parent
    approved_root = Path(report["plan"]["output_root"]).resolve()
    if not root.is_relative_to(approved_root):
        raise ValueError("Report is outside the existing private output root")
    result = aggregate_report(report, tasks, root)
    json_path, md_path = root / "aggregate-report.json", root / "aggregate-report.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    for path in write_report(args.report):
        print(path)


if __name__ == "__main__":
    main()
