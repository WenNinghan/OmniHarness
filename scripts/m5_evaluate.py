"""Opt-in G4 20-task evaluation runner and offline deterministic scorer.

Modes:
  --plan        Read-only dataset inventory and A/B/C split plan (default).
  --self-test   Offline 30-check verification (10 starter failures + 20 reference passes).
  --score-file  Offline scoring of candidate outputs from a JSONL file.
  --run-live    Opt-in loopback API evaluation with BudgetLedger and resume support.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from omniharness.eval_scoring import (  # noqa: E402
    DATASET_SPLITS,
    REFERENCE_CODE_SOLUTIONS,
    TASK_TO_SPLIT,
    build_research_prompt,
    grade_task,
    load_regression_dataset,
    summarize_eval_records,
)
from omniharness.evaluation_budget import AUTHORIZATION, BudgetLedger  # noqa: E402

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
STOPPED_STATUSES = {"paused", "awaiting_approval", "awaiting_user", "pending_review"}
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")


def extract_code_candidate(text: str) -> str:
    """Extract a single unambiguous fenced code block if present, else return raw text."""
    stripped = text.strip()
    if not stripped:
        return ""
    lines = stripped.splitlines()
    open_marker: str | None = None
    has_fence = False
    buffer: list[str] = []
    blocks: list[str] = []
    for line in lines:
        if open_marker is None:
            match = _FENCE_OPEN_RE.match(line)
            if match is None:
                continue
            marker, rest = match.group(1), match.group(2)
            if marker[0] == "`" and "`" in rest:
                continue
            has_fence = True
            open_marker = marker
            buffer = []
        else:
            trimmed = line.lstrip(" ").rstrip(" \t")
            if len(line) - len(line.lstrip(" ")) <= 3 and len(trimmed) >= len(open_marker) and set(trimmed) == {open_marker[0]}:
                blocks.append("\n".join(buffer))
                open_marker = None
                buffer = []
            else:
                buffer.append(line)
    if open_marker is not None:
        return ""
    if not has_fence:
        return text
    if len(blocks) == 1:
        return blocks[0] + "\n"
    return ""


def build_code_prompt(task: dict[str, Any]) -> str:
    task_input = task["input"]
    constraints = "\n".join(f"- {c}" for c in task_input.get("constraints", []))
    lang = task.get("language", "")
    return "\n".join(
        [
            task_input["prompt"],
            "",
            f"目标文件名：{task_input['filename']}（{lang}）",
            "约束：",
            constraints,
            "",
            "初始代码：",
            f"```{lang}",
            task_input["source_code"].rstrip(),
            "```",
            "",
            f"请仅输出修复后的完整 `{task_input['filename']}` 源码。",
        ]
    )


def run_plan() -> dict[str, Any]:
    dataset, sha256 = load_regression_dataset()
    return {
        "mode": "plan",
        "dataset_id": dataset.get("dataset_id"),
        "dataset_version": dataset.get("dataset_version"),
        "dataset_sha256": sha256,
        "total_tasks": len(dataset["tasks"]),
        "splits": {split: list(ids) for split, ids in DATASET_SPLITS.items()},
    }


def run_self_test() -> dict[str, Any]:
    dataset, sha256 = load_regression_dataset()
    by_id = {task["id"]: task for task in dataset["tasks"]}
    starter_checks: list[dict[str, Any]] = []
    reference_checks: list[dict[str, Any]] = []

    for task_id, task in by_id.items():
        if task["category"] == "code":
            starter = grade_task(task, task["input"]["source_code"])
            starter_failed = not starter["passed"]
            starter_checks.append(
                {
                    "task_id": task_id,
                    "expected_fail": True,
                    "actual_passed": starter["passed"],
                    "ok": starter_failed,
                }
            )
            ref_code = REFERENCE_CODE_SOLUTIONS[task_id]
            ref_res = grade_task(task, ref_code)
            reference_checks.append(
                {
                    "task_id": task_id,
                    "category": task["language"],
                    "expected_pass": True,
                    "actual_passed": ref_res["passed"],
                    "ok": bool(ref_res["passed"]),
                }
            )
        else:
            ref_res = grade_task(task, json.dumps(task["expected"], ensure_ascii=False))
            reference_checks.append(
                {
                    "task_id": task_id,
                    "category": "research",
                    "expected_pass": True,
                    "actual_passed": ref_res["passed"],
                    "ok": bool(ref_res["passed"]),
                    "checks": ref_res.get("checks", []),
                }
            )

    starter_failures_verified = sum(1 for item in starter_checks if item["ok"])
    reference_passes_verified = sum(1 for item in reference_checks if item["ok"])
    all_ok = starter_failures_verified == 10 and reference_passes_verified == 20
    return {
        "mode": "self-test",
        "dataset_sha256": sha256,
        "starter_failures_verified": starter_failures_verified,
        "reference_passes_verified": reference_passes_verified,
        "total_checks": len(starter_checks) + len(reference_checks),
        "passed": all_ok,
        "starter_checks": starter_checks,
        "reference_checks": reference_checks,
    }


def _require_holdout_authorization(split_or_task_ids: set[str], allow_holdout_c: bool, frozen_candidate_id: str | None) -> None:
    if "C" in split_or_task_ids or any(TASK_TO_SPLIT.get(tid) == "C" for tid in split_or_task_ids):
        if not allow_holdout_c or not (frozen_candidate_id and frozen_candidate_id.strip()):
            raise ValueError(
                "Holdout split C requires both --allow-holdout-c and a non-empty --frozen-candidate-id."
            )


def score_jsonl_file(
    score_file: Path,
    *,
    allow_holdout_c: bool = False,
    frozen_candidate_id: str | None = None,
) -> dict[str, Any]:
    dataset, sha256 = load_regression_dataset()
    by_id = {task["id"]: task for task in dataset["tasks"]}
    raw_lines = [line for line in score_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    entries = [json.loads(line) for line in raw_lines]
    task_ids = {str(entry.get("task_id", "")) for entry in entries}
    _require_holdout_authorization(task_ids, allow_holdout_c, frozen_candidate_id)

    scored: list[dict[str, Any]] = []
    for entry in entries:
        task_id = entry["task_id"]
        if task_id not in by_id:
            raise ValueError(f"Unknown task_id in score file: {task_id}")
        task = by_id[task_id]
        status = entry.get("status", "completed")
        if status in {"uncertain", "pending_review", "skipped", "cancelled", "interrupted"}:
            graded = {"task_id": task_id, "passed": False, "checks": []}
        else:
            output = entry.get("output", "")
            if task["category"] == "code" and isinstance(output, str):
                output = extract_code_candidate(output)
            graded = grade_task(task, output)
        scored.append(
            {
                "task_id": task_id,
                "split": TASK_TO_SPLIT[task_id],
                "category": task["category"] if task["category"] == "research" else task["language"],
                "condition": entry.get("condition", "default"),
                "repeat": entry.get("repeat", 1),
                "status": status,
                "review_status": entry.get("review_status"),
                "delivered_unreviewed": entry.get("delivered_unreviewed", False),
                "passed": bool(graded["passed"]),
                "checks": graded.get("checks"),
                "input_tokens": entry.get("input_tokens"),
                "output_tokens": entry.get("output_tokens"),
                "cost_usd": None if entry.get("cost_usd") is None else entry["cost_usd"],
            }
        )

    return {
        "mode": "score-file",
        "dataset_id": dataset.get("dataset_id"),
        "dataset_version": dataset.get("dataset_version"),
        "dataset_sha256": sha256,
        "frozen_candidate_id": frozen_candidate_id,
        "summary": summarize_eval_records(scored),
        "records": scored,
    }


def _validate_loopback_origin(url_str: str) -> str:
    parsed = urlsplit(url_str)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("--api-url must be an explicit loopback origin without credentials or query")
    return url_str.rstrip("/")


def _load_session_cookie(path: Path) -> str:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError("Session cookie file is empty")
    if "=" in raw:
        return raw.split("=", 1)[1].strip()
    return raw


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_live(args: argparse.Namespace) -> dict[str, Any]:
    if args.authorization != AUTHORIZATION:
        raise ValueError(f"--authorization must equal {AUTHORIZATION!r}")
    if not args.split or args.split not in DATASET_SPLITS:
        raise ValueError("--split must be one of A, B, C")
    _require_holdout_authorization({args.split}, args.allow_holdout_c, args.frozen_candidate_id)
    for flag in ("api_url", "session_cookie_file", "model_id", "output_dir", "max_requests"):
        if getattr(args, flag, None) in (None, ""):
            raise ValueError(f"--{flag.replace('_', '-')} is required with --run-live")

    api_origin = _validate_loopback_origin(args.api_url)
    cookie_val = _load_session_cookie(Path(args.session_cookie_file))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "results.jsonl"

    dataset, sha256 = load_regression_dataset()
    by_id = {task["id"]: task for task in dataset["tasks"]}
    task_ids = list(DATASET_SPLITS[args.split])
    existing_records: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int]] = set()
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            existing_records.append(rec)
            seen_keys.add((str(rec["task_id"]), int(rec["repeat"])))

    ledger = BudgetLedger(
        output_dir / "budget.sqlite3",
        authorization_id=args.authorization,
        limit=args.max_requests,
    )
    headers = {"Origin": api_origin}
    cookies = {"omni_session": cookie_val}

    with ledger.runner_lock():
        with httpx.Client(base_url=api_origin, headers=headers, cookies=cookies, timeout=30.0, trust_env=False) as client:
            for repeat in range(1, args.repetitions + 1):
                for task_id in task_ids:
                    if (task_id, repeat) in seen_keys:
                        continue
                    task = by_id[task_id]
                    res_id = f"m5:{args.split}:{task_id}:r{repeat}"
                    ledger.reserve(res_id, 1, "m5_evaluate", metadata={"task_id": task_id, "repeat": repeat})
                    ledger.start(res_id)

                    prompt_text = (
                        build_research_prompt(task)
                        if task["category"] == "research"
                        else build_code_prompt(task)
                    )
                    run_id: str | None = None
                    conv_id: str | None = None
                    status = "uncertain"
                    output_text = ""
                    input_tokens: int | None = None
                    output_tokens: int | None = None
                    review_status: str | None = None
                    try:
                        conv_resp = client.post("/api/v1/conversations", json={"title": f"M5 {task_id} r{repeat}"})
                        conv_resp.raise_for_status()
                        conv_id = conv_resp.json()["id"]
                        msg_resp = client.post(
                            f"/api/v1/conversations/{conv_id}/messages",
                            json={
                                "content": prompt_text,
                                "model_config_id": args.model_id,
                                "client_request_id": f"m5-{uuid4()}",
                                "workflow_type": "chat",
                            },
                        )
                        msg_resp.raise_for_status()
                        run_id = msg_resp.json()["run"]["id"]
                        deadline = time.monotonic() + args.run_timeout_seconds
                        while time.monotonic() < deadline:
                            run_resp = client.get(f"/api/v1/runs/{run_id}")
                            run_resp.raise_for_status()
                            run_data = run_resp.json()
                            cur_status = run_data.get("status")
                            if cur_status in TERMINAL_STATUSES:
                                status = cur_status
                                review_status = run_data.get("review_status")
                                usage = run_data.get("usage") or {}
                                input_tokens = usage.get("input_tokens")
                                output_tokens = usage.get("output_tokens")
                                break
                            if cur_status in STOPPED_STATUSES:
                                status = "uncertain"
                                break
                            time.sleep(args.poll_seconds)
                        if status == "completed" and conv_id:
                            msgs = client.get(f"/api/v1/conversations/{conv_id}/messages")
                            msgs.raise_for_status()
                            assistants = [
                                m for m in msgs.json()
                                if m.get("run_id") == run_id and m.get("role") == "assistant"
                            ]
                            if assistants:
                                output_text = assistants[-1].get("content") or ""
                    except httpx.HTTPError:
                        status = "uncertain"

                    if status in TERMINAL_STATUSES:
                        ledger.finish(res_id, 1)
                    else:
                        ledger.unknown(res_id)

                    if status == "completed":
                        candidate = (
                            extract_code_candidate(output_text)
                            if task["category"] == "code"
                            else output_text
                        )
                        graded = grade_task(task, candidate)
                        passed = bool(graded["passed"])
                        checks = graded.get("checks")
                    else:
                        passed = False
                        checks = []

                    record = {
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                        "dataset_sha256": sha256,
                        "frozen_candidate_id": args.frozen_candidate_id,
                        "task_id": task_id,
                        "split": args.split,
                        "category": task["category"] if task["category"] == "research" else task["language"],
                        "condition": args.model_id,
                        "repeat": repeat,
                        "run_id": run_id,
                        "status": status,
                        "review_status": review_status,
                        "passed": passed,
                        "checks": checks,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cost_usd": None,
                    }
                    _append_jsonl(results_path, record)
                    existing_records.append(record)
                    seen_keys.add((task_id, repeat))

    summary_payload = {
        "mode": "run-live",
        "dataset_id": dataset.get("dataset_id"),
        "dataset_version": dataset.get("dataset_version"),
        "dataset_sha256": sha256,
        "split": args.split,
        "frozen_candidate_id": args.frozen_candidate_id,
        "budget": ledger.snapshot(),
        "summary": summarize_eval_records(existing_records),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return summary_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OmniHarness G4 20-task evaluation runner & offline scorer")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Print dataset split plan without network or file writes")
    mode.add_argument("--self-test", action="store_true", help="Run offline 30-check grading verification")
    mode.add_argument("--score-file", type=Path, help="Score saved candidate outputs from JSONL")
    mode.add_argument("--run-live", action="store_true", help="Run live evaluation against loopback API")

    parser.add_argument("--output", type=Path, help="Optional JSON output path for --score-file")
    parser.add_argument("--allow-holdout-c", action="store_true", help="Explicitly unlock split C for frozen evaluation")
    parser.add_argument("--frozen-candidate-id", type=str, default=None, help="Frozen candidate identifier")

    parser.add_argument("--api-url", type=str, default=None)
    parser.add_argument("--session-cookie-file", type=str, default=None)
    parser.add_argument("--model-id", type=str, default=None)
    parser.add_argument("--split", choices=["A", "B", "C"], default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--max-requests", type=int, default=None)
    parser.add_argument("--authorization", type=str, default=None)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--run-timeout-seconds", type=float, default=300.0)

    args = parser.parse_args(argv)
    try:
        if args.self_test:
            result = run_self_test()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["passed"] else 1
        if args.score_file is not None:
            result = score_jsonl_file(
                args.score_file,
                allow_holdout_c=args.allow_holdout_c,
                frozen_candidate_id=args.frozen_candidate_id,
            )
            encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
            if args.output is not None:
                args.output.write_text(encoded, encoding="utf-8")
            print(encoded)
            return 0
        if args.run_live:
            result = run_live(args)
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        result = run_plan()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
