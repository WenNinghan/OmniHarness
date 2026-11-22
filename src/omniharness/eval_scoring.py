"""Deterministic read-only evaluation engine for the 20 fixed regression tasks."""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .structured_json import extract_json_payload

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS_PATH = ROOT / "examples" / "evaluation" / "tasks.json"

DATASET_SPLITS: dict[str, tuple[str, ...]] = {
    "A": (
        "research-001",
        "research-002",
        "research-003",
        "research-004",
        "python-001",
        "python-002",
        "typescript-001",
        "typescript-002",
    ),
    "B": (
        "research-005",
        "research-006",
        "research-007",
        "python-003",
        "python-004",
        "typescript-003",
    ),
    "C": (
        "research-008",
        "research-009",
        "research-010",
        "python-005",
        "typescript-004",
        "typescript-005",
    ),
}

TASK_TO_SPLIT: dict[str, str] = {
    task_id: split
    for split, task_ids in DATASET_SPLITS.items()
    for task_id in task_ids
}

_LOCATOR_RE = re.compile(r"^[A-Z0-9-]+:\d+$")
_MAX_STDIO_BYTES = 8192

REFERENCE_CODE_SOLUTIONS: dict[str, str] = {
    "python-001": """def merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result
""",
    "python-002": """def chunks(items, size):
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("size must be positive")
    return [list(items[i:i + size]) for i in range(0, len(items), size)]
""",
    "python-003": """import re

_DURATION_RE = re.compile(r"^\\s*(\\d+(?:\\.\\d+)?)(ms|s)\\s*$")


def parse_duration_ms(value):
    if not isinstance(value, str):
        raise ValueError("duration must be a string")
    match = _DURATION_RE.fullmatch(value)
    if match is None:
        raise ValueError("invalid duration format")
    number = float(match.group(1))
    unit = match.group(2)
    return number if unit == "ms" else number * 1000.0
""",
    "python-004": """def dedupe_by_id(rows):
    seen = set()
    result = []
    for row in rows:
        identity = row.get("id")
        if identity is None:
            result.append(row)
            continue
        key = (type(identity), identity)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result
""",
    "python-005": """def moving_average(values, window):
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("window must be positive")
    if window > len(values):
        return []
    return [sum(values[i:i + window]) / window for i in range(len(values) - window + 1)]
""",
    "typescript-001": (
        "export function numericSort(values: number[]): number[] {\n"
        "  return [...values].sort((a, b) => a - b);\n"
        "}\n"
    ),
    "typescript-002": """const PRICE_RE = /^(?:0|[1-9]\\d*)(?:\\.\\d{1,2})?$/;

export function totalCents(prices: string[]): number {
  let total = 0;
  for (const raw of prices) {
    if (!PRICE_RE.test(raw)) {
      throw new TypeError("Invalid price format");
    }
    const [wholePart, fracPart = ""] = raw.split(".");
    const whole = Number(wholePart);
    const frac = Number(fracPart.padEnd(2, "0"));
    if (!Number.isSafeInteger(whole) || whole > Math.floor((Number.MAX_SAFE_INTEGER - frac) / 100)) {
      throw new RangeError("Item exceeds MAX_SAFE_INTEGER");
    }
    const cents = whole * 100 + frac;
    if (total > Number.MAX_SAFE_INTEGER - cents) {
      throw new RangeError("Total exceeds MAX_SAFE_INTEGER");
    }
    total += cents;
  }
  return total;
}
""",
    "typescript-003": """export function toQuery(params: Record<string, string | string[] | null | undefined>): string {
  const parts: string[] = [];
  for (const key of Object.keys(params).sort()) {
    const value = params[key];
    if (value === null || value === undefined) {
      continue;
    }
    const encodedKey = encodeURIComponent(key);
    if (Array.isArray(value)) {
      for (const item of value) {
        parts.push(`${encodedKey}=${encodeURIComponent(item)}`);
      }
    } else {
      parts.push(`${encodedKey}=${encodeURIComponent(value)}`);
    }
  }
  return parts.join("&");
}
""",
    "typescript-004": """export type Event = { sequence: number; text: string };

export function applyEvents(current: string, lastSequence: number, events: Event[]) {
  if (!Number.isSafeInteger(lastSequence) || lastSequence < 0) {
    throw new TypeError("Invalid lastSequence");
  }
  let text = current;
  let cursor = lastSequence;
  for (const event of events) {
    if (!Number.isSafeInteger(event.sequence) || event.sequence <= 0) {
      throw new TypeError("Invalid event sequence");
    }
    if (event.sequence <= cursor) {
      continue;
    }
    if (event.sequence !== cursor + 1) {
      throw new RangeError("Missing event sequence");
    }
    text += event.text;
    cursor = event.sequence;
  }
  return { text, lastSequence: cursor };
}
""",
    "typescript-005": """export function groupBy<T>(items: T[], key: (item: T) => string): Record<string, T[]> {
  const result: Record<string, T[]> = Object.create(null);
  for (const item of items) {
    const name = key(item);
    if (!Object.hasOwn(result, name)) {
      Object.defineProperty(result, name, {
        value: [],
        enumerable: true,
        writable: true,
        configurable: true,
      });
    }
    result[name].push(item);
  }
  return result;
}
""",
}


def load_regression_dataset(path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Read tasks.json without modification and validate the fixed A/B/C partition."""
    target = path or DEFAULT_TASKS_PATH
    raw_bytes = target.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    dataset = json.loads(raw_bytes.decode("utf-8"))
    tasks = dataset.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("Evaluation dataset must contain a task list")
    task_ids = [task.get("id") for task in tasks]
    if len(task_ids) != len(set(task_ids)) or any(not isinstance(item, str) for item in task_ids):
        raise ValueError("Evaluation task IDs must be unique strings")
    split_ids = [item for group in DATASET_SPLITS.values() for item in group]
    if len(split_ids) != len(set(split_ids)):
        raise ValueError("DATASET_SPLITS must be disjoint")
    if set(split_ids) != set(task_ids):
        raise ValueError("DATASET_SPLITS must be an exact partition of dataset tasks")
    return dataset, sha256


def _skeleton_from_expected(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return 0.0
    if isinstance(value, str):
        if _LOCATOR_RE.fullmatch(value.strip()):
            return "<DOC-ID:line>"
        return "<string>"
    if isinstance(value, list):
        if not value:
            return []
        if all(isinstance(item, dict) for item in value):
            merged: dict[str, Any] = {}
            for item in value:
                for key, item_val in item.items():
                    if key not in merged or (merged[key] is None and item_val is not None):
                        merged[key] = _skeleton_from_expected(item_val)
            return [merged]
        return [_skeleton_from_expected(value[0])]
    if isinstance(value, dict):
        return {key: _skeleton_from_expected(sub) for key, sub in value.items()}
    return "<value>"


def build_research_prompt(task: dict[str, Any]) -> str:
    """Build a leak-free research prompt containing sources and structural JSON skeleton."""
    if task.get("category") != "research":
        raise ValueError("build_research_prompt requires a research task")
    task_input = task["input"]
    lines = [
        task_input["prompt"],
        "",
        "约束：",
        "- 仅使用下方所附合成资料；不得联网或补充外部事实。",
        "- 每个事实、数值与状态结论必须标注精确的 `document_id:line` 引用。",
    ]
    if task_input.get("cutoff"):
        lines.append(f"- 严格截止时间：{task_input['cutoff']}")
    lines.append("")
    lines.append("资料：")
    for doc in task_input.get("source_documents", []):
        header = f"[{doc['document_id']}] {doc.get('title', '')}"
        if doc.get("published_at"):
            header += f" (published_at={doc['published_at']})"
        lines.append(header)
        for entry in doc.get("lines", []):
            lines.append(f"  {doc['document_id']}:{entry['line']}: {entry['text']}")
    skeleton = _skeleton_from_expected(task["expected"])
    lines.append("")
    lines.append("请仅输出符合以下字段结构的单个 JSON 对象（占位符仅表示字段类型，请替换为真实推导结果）：")
    lines.append(json.dumps(skeleton, ensure_ascii=False, indent=2))
    return "\n".join(lines)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _close(actual: Any, expected: float, abs_tol: float = 1e-9) -> bool:
    return _is_num(actual) and math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=abs_tol)


def _norm_locators(items: Any) -> set[str]:
    if not isinstance(items, list):
        return set()
    return {item.strip() for item in items if isinstance(item, str) and item.strip()}


def _has_locators(items: Any, required: set[str]) -> bool:
    return isinstance(items, list) and required.issubset(_norm_locators(items))


def _str_upper(value: Any) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def _add_check(checks: list[dict[str, Any]], name: str, passed: bool, detail: str = "") -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def _extract_prose_outside_json(raw_text: str) -> str:
    try:
        payload = extract_json_payload(raw_text)
    except ValueError:
        return raw_text
    if payload == raw_text.strip():
        return ""
    return raw_text.replace(payload, "")


def grade_research_task(
    task: dict[str, Any],
    candidate_output: str | dict[str, Any],
    *,
    abs_tol: float = 1e-9,
) -> dict[str, Any]:
    """Deterministically grade any of the 10 fixed research tasks."""
    task_id = task.get("id", "")
    checks: list[dict[str, Any]] = []
    raw_text = ""
    if isinstance(candidate_output, str):
        raw_text = candidate_output
        try:
            payload = extract_json_payload(candidate_output)
            data = json.loads(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            _add_check(checks, "valid_json", False, f"Invalid JSON payload: {type(exc).__name__}")
            return {"task_id": task_id, "passed": False, "checks": checks}
    elif isinstance(candidate_output, dict):
        data = candidate_output
    else:
        _add_check(checks, "valid_json", False, "Candidate output must be str or dict")
        return {"task_id": task_id, "passed": False, "checks": checks}

    if not isinstance(data, dict):
        _add_check(checks, "json_object", False, "Candidate JSON root must be an object")
        return {"task_id": task_id, "passed": False, "checks": checks}

    if task_id == "research-001":
        _add_check(checks, "status", _str_upper(data.get("status")) == "PASS")
        unit = data.get("unit")
        _add_check(checks, "unit", isinstance(unit, str) and unit.strip().lower() == "kwh")
        items = data.get("items")
        by_node = {}
        if isinstance(items, list):
            for entry in items:
                if isinstance(entry, dict) and isinstance(entry.get("node"), str):
                    by_node[entry["node"].strip()] = entry
        expected_items = {"甲": (2.4, {"R001-A:1"}), "乙": (1.8, {"R001-A:2"}), "丙": (0.65, {"R001-A:3"})}
        items_ok = isinstance(items, list) and len(items) == 3 and set(by_node) == set(expected_items)
        if items_ok:
            for node, (val, ev) in expected_items.items():
                entry = by_node[node]
                if not (_close(entry.get("value"), val, abs_tol) and _has_locators(entry.get("evidence"), ev)):
                    items_ok = False
                    break
        _add_check(checks, "items", items_ok)
        _add_check(checks, "total", _close(data.get("total"), 4.85, abs_tol))

    elif task_id == "research-002":
        specs = (
            ("completion_rate", 90, 120, 0.75, {"R002-A:1", "R002-A:2"}),
            ("success_among_completers", 18, 90, 0.2, {"R002-A:2", "R002-A:3"}),
            ("success_among_all", 18, 120, 0.15, {"R002-A:1", "R002-A:3"}),
        )
        for field, num, den, val, ev in specs:
            entry = data.get(field)
            ok = (
                isinstance(entry, dict)
                and _close(entry.get("numerator"), num, abs_tol)
                and _close(entry.get("denominator"), den, abs_tol)
                and _close(entry.get("value"), val, abs_tol)
                and _has_locators(entry.get("evidence"), ev)
            )
            _add_check(checks, field, ok)

    elif task_id == "research-003":
        _add_check(checks, "claim_status", _str_upper(data.get("claim_status")) == "UNVERIFIED")
        _add_check(
            checks,
            "measured_new_latency_null",
            "measured_new_latency_ms" in data and data["measured_new_latency_ms"] is None,
        )
        _add_check(checks, "known_old_latency_ms", _close(data.get("known_old_latency_ms"), 200, abs_tol))
        _add_check(checks, "old_measurement_evidence", _has_locators(data.get("old_measurement_evidence"), {"R003-B:1"}))
        _add_check(checks, "claim_evidence", _has_locators(data.get("claim_evidence"), {"R003-A:1"}))
        _add_check(
            checks,
            "limitations_evidence",
            _has_locators(data.get("limitations_evidence"), {"R003-A:2", "R003-B:2"}),
        )
        other_json = json.dumps({k: v for k, v in data.items() if k != "forbidden_inferences"}, ensure_ascii=False)
        combined = f"{other_json}\n{_extract_prose_outside_json(raw_text)}"
        compact = re.sub(r"\s+", "", combined).lower()
        forbidden_hit = "100ms" in compact or "50%提速已验证" in compact
        _add_check(checks, "no_forbidden_inferences", not forbidden_hit)

    elif task_id == "research-004":
        _add_check(checks, "status", _str_upper(data.get("status")) == "DISPUTED")
        _add_check(
            checks,
            "confirmed_amount_null",
            "confirmed_amount_yuan" in data and data["confirmed_amount_yuan"] is None,
        )
        alts = data.get("alternatives")
        has_12m = False
        has_15m = False
        has_avg = False
        if isinstance(alts, list) and len(alts) >= 2:
            for item in alts:
                if not isinstance(item, dict):
                    continue
                if _close(item.get("amount_yuan"), 13_500_000, abs_tol):
                    has_avg = True
                if _close(item.get("amount_yuan"), 12_000_000, abs_tol) and _has_locators(
                    item.get("evidence"), {"R004-A:1", "R004-A:2"}
                ):
                    has_12m = True
                if _close(item.get("amount_yuan"), 15_000_000, abs_tol) and _has_locators(
                    item.get("evidence"), {"R004-B:1"}
                ):
                    has_15m = True
        _add_check(checks, "alternatives", has_12m and has_15m and not has_avg)
        res_needed = data.get("resolution_needed")
        _add_check(checks, "resolution_needed", isinstance(res_needed, str) and bool(res_needed.strip()))

    elif task_id == "research-005":
        status_cut = data.get("status_at_cutoff")
        _add_check(checks, "status_at_cutoff", isinstance(status_cut, str) and status_cut.strip().lower() == "public_beta")
        ver_cut = data.get("version_at_cutoff")
        _add_check(checks, "version_at_cutoff", str(ver_cut).strip() == "0.9" if ver_cut is not None else False)
        _add_check(checks, "v1_released_at_cutoff", data.get("v1_released_at_cutoff") is False)
        ev = _norm_locators(data.get("evidence"))
        _add_check(checks, "evidence", "R005-A:1" in ev and not any(x == "R005-B" or x.startswith("R005-B:") for x in ev))
        excl = _norm_locators(data.get("excluded_after_cutoff"))
        _add_check(checks, "excluded_after_cutoff", any(x == "R005-B" or x.startswith("R005-B:") for x in excl))

    elif task_id == "research-006":
        _add_check(checks, "median_a_ms", _close(data.get("median_a_ms"), 10, abs_tol))
        _add_check(checks, "median_b_ms", _close(data.get("median_b_ms"), 9, abs_tol))
        _add_check(checks, "reduction_fraction", _close(data.get("reduction_fraction"), 0.1, abs_tol))
        meas_ev = _norm_locators(data.get("measurement_evidence"))
        comp_ev = _norm_locators(data.get("comparability_evidence"))
        _add_check(checks, "measurement_evidence", {"R006-A:1", "R006-B:1"}.issubset(meas_ev) and "R006-A:3" not in meas_ev)
        _add_check(checks, "comparability_evidence", {"R006-A:2", "R006-B:2"}.issubset(comp_ev) and "R006-A:3" not in comp_ev)
        _add_check(checks, "irrelevant_lines", _has_locators(data.get("irrelevant_lines"), {"R006-A:3"}))

    elif task_id == "research-007":
        _add_check(checks, "combined_claim_status", _str_upper(data.get("combined_claim_status")) == "UNVERIFIED")
        _add_check(
            checks,
            "relationship_null",
            "relationship_between_entities" in data and data["relationship_between_entities"] is None,
        )
        entities = data.get("entities")
        by_id = {}
        if isinstance(entities, list):
            for entry in entities:
                if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                    by_id[entry["id"].strip()] = entry
        ok = isinstance(entities, list) and len(entities) == 2 and set(by_id) == {"DEMO-A", "DEMO-B"}
        if ok:
            a, b = by_id["DEMO-A"], by_id["DEMO-B"]
            ok = (
                a.get("name") == "星桥"
                and a.get("office") == "南区"
                and a.get("business") == "光子芯片研发"
                and _has_locators(a.get("evidence"), {"R007-A:1", "R007-A:2", "R007-A:3"})
                and b.get("name") == "星桥"
                and b.get("office") == "北区"
                and b.get("business") == "库存管理软件"
                and _has_locators(b.get("evidence"), {"R007-B:1", "R007-B:2", "R007-B:3"})
            )
        _add_check(checks, "entities", ok)

    elif task_id == "research-008":
        _add_check(checks, "unique_experiments", _is_int(data.get("unique_experiments")) and data["unique_experiments"] == 2)
        uids = data.get("unique_ids")
        _add_check(
            checks,
            "unique_ids",
            isinstance(uids, list) and len(uids) == 2 and {x.strip() for x in uids if isinstance(x, str)} == {"ST-001", "ST-002"},
        )
        _add_check(checks, "participants", _is_int(data.get("participants")) and data["participants"] == 80)
        _add_check(checks, "successes", _is_int(data.get("successes")) and data["successes"] == 73)
        _add_check(checks, "pooled_success_fraction", _close(data.get("pooled_success_fraction"), 0.9125, abs_tol))
        dups = _norm_locators(data.get("duplicate_documents"))
        _add_check(checks, "duplicate_documents", any(x == "R008-B" or x.startswith("R008-B:") for x in dups))
        ev = _norm_locators(data.get("evidence"))
        _add_check(
            checks,
            "evidence",
            {"R008-A:1", "R008-C:1", "R008-C:2"}.issubset(ev)
            and not any(x == "R008-B" or x.startswith("R008-B:") for x in ev),
        )

    elif task_id == "research-009":
        sites = data.get("sites")
        by_site = {}
        if isinstance(sites, list):
            for entry in sites:
                if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                    by_site[entry["id"].strip()] = entry
        sites_ok = isinstance(sites, list) and len(sites) == 3 and set(by_site) == {"A", "B", "C"}
        if sites_ok:
            sa, sb, sc = by_site["A"], by_site["B"], by_site["C"]
            sites_ok = (
                _close(sa.get("failure_fraction"), 0, abs_tol)
                and _str_upper(sa.get("status")) == "VERIFIED"
                and _has_locators(sa.get("evidence"), {"R009-A:1"})
                and "failure_fraction" in sb
                and sb["failure_fraction"] is None
                and _str_upper(sb.get("status")) == "UNVERIFIED"
                and _has_locators(sb.get("evidence"), {"R009-A:2"})
                and _close(sc.get("failure_fraction"), 0.1, abs_tol)
                and _str_upper(sc.get("status")) == "VERIFIED"
                and _has_locators(sc.get("evidence"), {"R009-A:3"})
            )
        _add_check(checks, "sites", sites_ok)
        _add_check(checks, "known_tests", _is_int(data.get("known_tests")) and data["known_tests"] == 30)
        _add_check(checks, "known_failures", _is_int(data.get("known_failures")) and data["known_failures"] == 2)
        _add_check(
            checks,
            "aggregate_failure_fraction",
            _close(data.get("aggregate_failure_fraction"), 2 / 30, abs_tol),
        )
        excl = _norm_locators(data.get("excluded_sites"))
        _add_check(checks, "excluded_sites", "B" in excl)

    elif task_id == "research-010":
        _add_check(
            checks,
            "universal_improvement_claim",
            _str_upper(data.get("universal_improvement_claim")) == "FAIL",
        )
        delta = data.get("delta_percentage_points")
        _add_check(
            checks,
            "delta_percentage_points",
            isinstance(delta, dict) and _close(delta.get("A"), 3, abs_tol) and _close(delta.get("B"), -4, abs_tol),
        )
        _add_check(checks, "task_c_status", _str_upper(data.get("task_c_status")) == "UNVERIFIED")
        _add_check(
            checks,
            "counterexample_evidence",
            _has_locators(data.get("counterexample_evidence"), {"R010-A:2"}),
        )
        _add_check(
            checks,
            "evidence",
            _has_locators(data.get("evidence"), {"R010-A:1", "R010-A:2", "R010-A:3"}),
        )
        allowed_summary = data.get("allowed_summary")
        _add_check(checks, "allowed_summary", isinstance(allowed_summary, str) and bool(allowed_summary.strip()))

    else:
        _add_check(checks, "known_research_task", False, f"Unsupported research task: {task_id}")

    return {"task_id": task_id, "passed": bool(checks) and all(item["passed"] for item in checks), "checks": checks}


def _truncate_text(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    encoded = raw.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_STDIO_BYTES:
        return raw
    return encoded[:_MAX_STDIO_BYTES].decode("utf-8", errors="replace") + "\n...[truncated]"


def grade_python_task(
    task: dict[str, Any],
    candidate_code: str,
    *,
    python_executable: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Execute Python candidate solution in an isolated temporary directory."""
    task_id = task.get("id", "")
    test_code = task["verification_method"]["test_code"]
    exe = python_executable or sys.executable
    with tempfile.TemporaryDirectory(prefix=f"omni-eval-{task_id}-") as tmpdir:
        work = Path(tmpdir)
        (work / "candidate.py").write_text(candidate_code, encoding="utf-8")
        (work / "test_candidate.py").write_text(test_code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [exe, "-s", "-E", "test_candidate.py"],
                cwd=work,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
            return {
                "task_id": task_id,
                "passed": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": _truncate_text(proc.stdout),
                "stderr": _truncate_text(proc.stderr),
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "task_id": task_id,
                "passed": False,
                "exit_code": None,
                "stdout": _truncate_text(exc.stdout),
                "stderr": _truncate_text(exc.stderr),
                "timed_out": True,
            }


def grade_typescript_task(
    task: dict[str, Any],
    candidate_code: str,
    *,
    repo_root: Path | None = None,
    node_executable: str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Compile and execute TypeScript candidate solution in an isolated temp directory."""
    task_id = task.get("id", "")
    test_code = task["verification_method"]["test_code"]
    root = repo_root or ROOT
    node_bin = node_executable or "node"
    tsc_js = root / "web" / "node_modules" / "typescript" / "bin" / "tsc"
    types_dir = root / "web" / "node_modules" / "@types"
    if not tsc_js.is_file():
        raise FileNotFoundError(f"TypeScript compiler entry not found at {tsc_js}")

    with tempfile.TemporaryDirectory(prefix=f"omni-eval-{task_id}-") as tmpdir:
        work = Path(tmpdir)
        (work / "package.json").write_text('{"type": "module"}\n', encoding="utf-8")
        tsconfig = {
            "compilerOptions": {
                "target": "ES2022",
                "module": "NodeNext",
                "moduleResolution": "NodeNext",
                "strict": True,
                "types": ["node"],
                "typeRoots": [str(types_dir)],
                "skipLibCheck": True,
            },
            "include": ["candidate.ts", "test_candidate.ts"],
        }
        (work / "tsconfig.json").write_text(json.dumps(tsconfig, indent=2), encoding="utf-8")
        (work / "candidate.ts").write_text(candidate_code, encoding="utf-8")
        (work / "test_candidate.ts").write_text(test_code, encoding="utf-8")

        try:
            comp = subprocess.run(
                [node_bin, str(tsc_js), "--project", "tsconfig.json"],
                cwd=work,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "task_id": task_id,
                "passed": False,
                "compile_exit_code": None,
                "run_exit_code": None,
                "stdout": _truncate_text(exc.stdout),
                "stderr": _truncate_text(exc.stderr),
                "timed_out": True,
            }
        if comp.returncode != 0:
            return {
                "task_id": task_id,
                "passed": False,
                "compile_exit_code": comp.returncode,
                "run_exit_code": None,
                "stdout": _truncate_text(comp.stdout),
                "stderr": _truncate_text(comp.stderr),
                "timed_out": False,
            }

        try:
            run_proc = subprocess.run(
                [node_bin, "test_candidate.js"],
                cwd=work,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
            return {
                "task_id": task_id,
                "passed": run_proc.returncode == 0,
                "compile_exit_code": 0,
                "run_exit_code": run_proc.returncode,
                "stdout": _truncate_text(run_proc.stdout),
                "stderr": _truncate_text(run_proc.stderr),
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "task_id": task_id,
                "passed": False,
                "compile_exit_code": 0,
                "run_exit_code": None,
                "stdout": _truncate_text(exc.stdout),
                "stderr": _truncate_text(exc.stderr),
                "timed_out": True,
            }


def grade_task(task: dict[str, Any], candidate_output: str | dict[str, Any]) -> dict[str, Any]:
    """Dispatch deterministic grading by task category and language."""
    category = task.get("category")
    if category == "research":
        return grade_research_task(task, candidate_output)
    if category == "code":
        if not isinstance(candidate_output, str):
            return {"task_id": task.get("id", ""), "passed": False, "error": "Code output must be a string"}
        language = task.get("language")
        if language == "python":
            return grade_python_task(task, candidate_output)
        if language == "typescript":
            return grade_typescript_task(task, candidate_output)
    raise ValueError(f"Unsupported task category/language for {task.get('id')}")


def _task_category(record: dict[str, Any]) -> str:
    task_id = str(record.get("task_id", ""))
    if task_id.startswith("research-"):
        return "research"
    if task_id.startswith("python-"):
        return "python"
    if task_id.startswith("typescript-"):
        return "typescript"
    cat = record.get("language") or record.get("category")
    if cat in {"research", "python", "typescript"}:
        return str(cat)
    return "unknown"


def _classify_record(record: dict[str, Any]) -> str:
    status = record.get("status")
    if status is None:
        status = record.get("run_status")
    if status in {"uncertain", "pending_review", "skipped", "cancelled", "interrupted"}:
        return "uncertain_or_skipped"
    review_status = record.get("review_status")
    is_degraded = bool(record.get("delivered_unreviewed")) or (
        isinstance(review_status, str) and bool(review_status.strip()) and review_status.upper() != "PASS"
    )
    if is_degraded:
        return "delivered_unreviewed"
    if bool(record.get("passed")) and status in {None, "completed", "resolved", "PASS"}:
        return "resolved"
    return "failed"


def _summarize_bucket(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    counts = {"resolved": 0, "delivered_unreviewed": 0, "failed": 0, "uncertain_or_skipped": 0}
    grouped: dict[tuple[str, str], list[float]] = {}
    in_tokens: list[int] = []
    out_tokens: list[int] = []
    costs: list[float] = []
    all_in_known = bool(records)
    all_out_known = bool(records)
    all_cost_known = bool(records)

    for rec in records:
        outcome = _classify_record(rec)
        counts[outcome] += 1
        key = (str(rec.get("task_id", "")), str(rec.get("condition", "default")))
        grouped.setdefault(key, []).append(1.0 if outcome == "resolved" else 0.0)

        tin = rec.get("input_tokens")
        if _is_int(tin) and tin >= 0:
            in_tokens.append(tin)
        else:
            all_in_known = False

        tout = rec.get("output_tokens")
        if _is_int(tout) and tout >= 0:
            out_tokens.append(tout)
        else:
            all_out_known = False

        cost = rec.get("cost_usd")
        if _is_num(cost) and float(cost) >= 0:
            costs.append(float(cost))
        else:
            all_cost_known = False

    group_variances: dict[str, float] = {}
    for (task_id, condition), xs in grouped.items():
        if len(xs) >= 2:
            mean_val = sum(xs) / len(xs)
            var = sum((x - mean_val) ** 2 for x in xs) / len(xs)
            group_variances[f"{task_id}::{condition}"] = var

    return {
        "total": total,
        **counts,
        "pass_rate": (counts["resolved"] / total) if total > 0 else None,
        "repeat_variance": {
            "mean": (sum(group_variances.values()) / len(group_variances)) if group_variances else None,
            "by_group": group_variances,
        },
        "total_input_tokens": sum(in_tokens) if all_in_known else (0 if not records else None),
        "total_output_tokens": sum(out_tokens) if all_out_known else (0 if not records else None),
        "cost_usd": sum(costs) if all_cost_known else None,
    }


def summarize_eval_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate evaluation records overall, by A/B/C split, and by task category."""
    by_split: dict[str, list[dict[str, Any]]] = {"A": [], "B": [], "C": []}
    by_category: dict[str, list[dict[str, Any]]] = {"research": [], "python": [], "typescript": []}

    for rec in records:
        task_id = str(rec.get("task_id", ""))
        split = rec.get("split") or TASK_TO_SPLIT.get(task_id)
        if split in by_split:
            by_split[split].append(rec)
        category = _task_category(rec)
        if category in by_category:
            by_category[category].append(rec)

    return {
        "overall": _summarize_bucket(records),
        "by_split": {split: _summarize_bucket(items) for split, items in by_split.items()},
        "by_category": {cat: _summarize_bucket(items) for cat, items in by_category.items()},
    }
