"""One-time, explicit audit-only closure of the user's formal slot 17.

No graph execution, model requests, automatic replay, or approvals are performed.
The original 50-call reservation remains unknown.  --execute is deliberately
required and a persisted intent prevents a second cancel after interruption.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

from omniharness.evaluation_budget import BudgetLedger
from omniharness.reference_cost import calculate_reference_cost
from omniharness.sandbox import operation_metadata
from scripts.m4_evaluate import (
    EvaluationError,
    candidate_bytes,
    digest,
    load_optimization_tasks,
    optimization_fairness,
    reviewed_completed_tools,
    save,
    summarize_optimization,
    trial_metrics,
)

ROOT = Path("G:/harness/.omniharness/m4-optimization")
SESSION = "m4-opt-d65b8cffaa3345d3"
RUN = "81330633-e9d0-4977-8187-6ced52d00040"
TRIAL = "017-formal-high-report-Single-Flash-r1"
MODEL = "7c4a1d7c-1a1d-46b7-a462-7bb9aeeba920"
RESERVATION = SESSION + "-17"


def guarded_cancel(client, run_id, intent, ledger, reservation):
    """Write intent before one POST; a later invocation may only read status."""
    if intent.exists():
        ledger.unknown(reservation)
        run = client.get("/api/v1/runs/" + run_id)
        run.raise_for_status()
        if run.json().get("status") != "cancelled" or run.json().get("id") != run_id:
            raise EvaluationError("Existing cancel intent has no confirmed terminal outcome; do not repost")
        return run.json()
    save(intent, {"run_id": run_id, "action": "single_cancel", "created_at": datetime.now(timezone.utc).isoformat()})
    ledger.unknown(reservation)
    try:
        response = client.post("/api/v1/runs/" + run_id + "/cancel")
        response.raise_for_status()
    except Exception:
        # No second POST is permitted, including when the first response is lost.
        response = client.get("/api/v1/runs/" + run_id)
        response.raise_for_status()
    result = response.json()
    if result.get("status") != "cancelled" or result.get("id") != run_id:
        raise EvaluationError("Cancel terminal state not confirmed; retain full reservation")
    return result


def assert_no_resources(controller, docker, identity):
    data = controller()
    page = data.get("pagination") or {}
    if data.get("items") != [] or page.get("totalItems") != 0 or page.get("hasNextPage") is not False:
        raise EvaluationError("Controller cannot prove all sandboxes absent")
    for all_containers in (False, True):
        code, matches = docker(identity, all_containers)
        if code != 0 or matches:
            raise EvaluationError("Docker cannot prove the Run sandbox absent")


def docker_scan(identity, all_containers):
    label = operation_metadata("omni-m4-preview", identity, "audit")["omni_run"]
    args = ["wsl.exe", "-d", "OmniHarnessCheckAlt", "-u", "root", "--exec", "docker", "ps"]
    if all_containers:
        args.append("-a")
    args += ["-q", "--filter", "label=omni_run=" + label]
    result = subprocess.run(args, capture_output=True, check=False, timeout=20)
    return result.returncode, bool(result.stdout.strip())


def verify_images(plan):
    prefix = ["wsl.exe", "-d", "OmniHarnessCheckAlt", "-u", "root", "--exec", "docker"]
    for name, running in (("omni-m4-preview-worker-1", "false"), ("omni-m4-preview-api-1", "true")):
        item = subprocess.run([*prefix, "inspect", "--format", "{{.Image}} {{.State.Running}}", name],
                              capture_output=True, check=False, timeout=20)
        if item.returncode or item.stdout.decode().strip() != plan["implementation_id"] + " " + running:
            raise EvaluationError("Candidate image or worker state differs")
    item = subprocess.run([*prefix, "image", "inspect", "--format", "{{.Id}}", plan["sandbox"]["image"]],
                          capture_output=True, check=False, timeout=20)
    if item.returncode or item.stdout.decode().strip() != plan["sandbox"]["image"]:
        raise EvaluationError("Frozen tool image differs")


def get_json(client, path):
    result = client.get(path)
    result.raise_for_status()
    return result.json()


def close_formal17(root=ROOT, *, verify_only=False):
    plan = json.loads((root / "plan-hy3-glm.json").read_text(encoding="utf-8"))
    campaign = root / "runs" / SESSION
    report_path = campaign / "report.json"
    trial = campaign / TRIAL
    state_path = trial / "trial-state.json"
    audit_path = root / "formal17-independent-acceptance-20261110" / "audit.json"
    acceptance_path = audit_path.parent / "acceptance.json"
    intent = root / "formal17-cancel-intent.json"
    ledger = BudgetLedger(root / "budget.sqlite3")
    with ledger.runner_lock():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
        frozen = report.get("formal_freeze") or {}
        entry = report["queue"][16]
        if (report.get("session") != SESSION or report.get("plan") != plan or entry.get("sequence") != 17
                or entry.get("phase") != "first" or entry.get("task_id") != "formal-high-report"
                or entry.get("condition") != "Single-Flash" or entry.get("repetition") != 1
                or frozen.get("economy_model_id") != MODEL or frozen.get("implementation_id") != plan["implementation_id"]
                or frozen.get("sandbox") != plan["sandbox"] or frozen.get("quote_snapshot") != report["quote_snapshot"]
                or state.get("run_id") != RUN or state.get("reservation") != RESERVATION
                or state.get("implementation_id") != plan["implementation_id"] or audit.get("run_id") != RUN):
            raise EvaluationError("Frozen formal identity differs")
        matching = [r for r in report["records"] if r.get("sequence") == 17]
        if matching:
            if len(matching) == 1 and matching[0].get("manual_formal_close", {}).get("run_id") == RUN:
                return "already_recorded"
            raise EvaluationError("Existing formal record cannot be replaced")
        if len(report["records"]) != 16 or {r["sequence"] for r in report["records"]} != set(range(1, 17)):
            raise EvaluationError("Previous slots do not match the frozen queue")
        _, tasks, manifest = load_optimization_tasks()
        task = tasks["formal-high-report"]
        if manifest != frozen.get("manifest_sha256") or audit.get("manifest_sha256") != manifest:
            raise EvaluationError("Task manifest differs")
        workspace = Path(state["workspace_path"]).resolve()
        expected_workspace = Path("G:/OmniHarness-M4-Fixtures/optimization") / SESSION / TRIAL
        if workspace != expected_workspace.resolve() or Path(state["workspace"]["host_path"]).resolve() != workspace:
            raise EvaluationError("Workspace path differs from the exact formal trial")
        candidate = {name: candidate_bytes(workspace / name) for name in task["write_allow"]}
        final_hashes = {name: digest(value) for name, value in candidate.items()}
        groups = acceptance.get("groups") or []
        if (final_hashes != audit.get("candidate_sha256") or final_hashes != acceptance.get("candidate_sha256")
                or acceptance.get("image") != plan["sandbox"]["image"] or acceptance.get("status") != "PASS"
                or acceptance.get("groups_passed") != 5 or acceptance.get("typescript_typecheck") != "PASS"
                or acceptance.get("exit_code") != 0 or acceptance.get("groups_complete") is not True
                or len(groups) != 5 or {g.get("group") for g in groups} != set(range(1, 6))
                or any(g.get("status") != "PASS" for g in groups)
                or acceptance.get("sandbox", {}).get("reclaimed") is not True):
            raise EvaluationError("Independent acceptance does not match the current files")
        reservation = next((x for x in ledger.snapshot()["reservations"] if x["id"] == RESERVATION), None)
        if not reservation or reservation["maximum"] != 50 or reservation["state"] not in {"active", "unknown"}:
            raise EvaluationError("Original full reservation is unavailable")
        verify_images(plan)
        key = Path(plan["sandbox"]["key_file"]).read_text(encoding="utf-8").strip()
        base = plan["api_url"]
        cookie = Path(plan["session_cookie_file"]).read_text(encoding="utf-8").strip()
        with httpx.Client(base_url=base, cookies={"omniharness_session": cookie}, headers={"Origin": base,
                          "X-Omni-Request": "1"}, timeout=30, trust_env=False) as client:
            run = get_json(client, "/api/v1/runs/" + RUN)
            if (run.get("status") not in {"pending_review", "cancelled"} or run.get("model_config_id") != MODEL
                    or run.get("workspace_id") != state["workspace"]["id"]
                    or run.get("conversation_id") != state["conversation_id"]):
                raise EvaluationError("Run identity or state changed")
            if run["status"] == "pending_review" and run.get("error_code") != "AGENT_EXECUTION_REVIEW":
                raise EvaluationError("Pending review reason changed")
            if run["status"] == "cancelled" and not intent.exists():
                raise EvaluationError("Cancelled without this audit intent")
            evidence = {name: get_json(client, "/api/v1/runs/" + RUN + "/" + name)
                        for name in ("tools", "operations", "artifacts", "budget")}
            tools, operations = evidence["tools"], evidence["operations"]
            if (len(tools) != 13 or len(operations) != 17
                    or any(row.get("status") != "completed" for row in tools + operations)
                    or any(row.get("tool_name") not in {"workspace_list", "workspace_read", "workspace_search", "workspace_patch"}
                           for row in tools)):
                raise EvaluationError("Original completed operations changed")
            patches = [{"operation_id": x["operation_id"], "path": x["path"],
                        "receipt_before_sha256": x["before_sha256"], "receipt_sha256": x["after_sha256"]}
                       for x in audit["patches"]]
            review = {"completed_tool_evidence": {"run_id": RUN, "frozen_tool_policy": run["workspace"]["tool_policy"],
                                                   "patches": patches, "commands": []}}
            reviewed_completed_tools(review, run, evidence, state, task, client)
            provisional = {**entry, "split": "formal", "agent_strategy": run.get("agent_strategy"),
                           "execution_config": run.get("execution_config"),
                           "tool_policy": run["workspace"]["tool_policy"]}
            previous = [r for r in report["records"] if r.get("split") == "formal"]
            if optimization_fairness(previous + [provisional], frozen_models=frozen["models"],
                                     frozen_selection=frozen).get("status") != "PASS":
                raise EvaluationError("Formal model binding preflight failed")
            with httpx.Client(timeout=20, trust_env=False) as ctrl:
                def controller():
                    r = ctrl.get(plan["sandbox"]["endpoint"] + "/v1/sandboxes", headers={"OPEN-SANDBOX-API-KEY": key})
                    r.raise_for_status()
                    return r.json()
                assert_no_resources(controller, docker_scan, RUN)
            if verify_only:
                return "preflight_passed_no_actions"
            audit_record = {"run_id": RUN, "manifest_sha256": manifest,
                "candidate_sha256": final_hashes, "acceptance_sha256": digest(acceptance_path.read_bytes()),
                "known_tools": 13, "known_operations": 17, "pending_command_outcome": "unknown",
                "original_review_code": "AGENT_EXECUTION_REVIEW",
                "reason": "User requested cancellation after review; no graph reentry or model replay",
                "elapsed_time_valid": False, "full_reservation_retained": 50}
            closure_audit = root / "formal17-close-audit.json"
            if closure_audit.exists():
                if json.loads(closure_audit.read_text(encoding="utf-8")) != audit_record:
                    raise EvaluationError("Existing closure audit cannot be replaced")
            else:
                save(closure_audit, audit_record)
            run = guarded_cancel(client, RUN, intent, ledger, RESERVATION)
            budget = get_json(client, "/api/v1/runs/" + RUN + "/budget")
            if type(budget.get("model_calls")) is not int:
                raise EvaluationError("Model count unavailable")
            started = datetime.fromisoformat(state["submitted_at"])
            finished = datetime.fromisoformat(run["finished_at"])
            cost = calculate_reference_cost(operations, expected_calls=budget["model_calls"],
                                            snapshot=report["quote_snapshot"], at=started)
            cost["status"] = "PARTIAL" if cost.get("observed_reference_cost") is not None else "UNKNOWN"
            cost["reference_cost"] = None
            cost["unknown"].append({"reason": "pending_command_outcome_not_attested_full_reservation_retained"})
            elapsed = max(0, (finished - started).total_seconds())
            record = {**entry, "split": task["split"], "complexity": task["complexity"],
                "implementation_id": state["implementation_id"], "run_id": RUN, "run_status": "cancelled",
                "run_error_code": run.get("error_code"), "agent_strategy": run.get("agent_strategy"),
                "execution_config": run.get("execution_config"), "tool_policy": run["workspace"]["tool_policy"],
                "acceptance": acceptance, "resolved": False, "latency_seconds": elapsed,
                "elapsed_time_valid": False, "elapsed_time_invalid_reason": "cross_restart_cancel",
                "active_seconds": budget.get("active_seconds"), "model_calls": budget["model_calls"],
                "tool_calls": budget.get("tool_calls"), "usage": run.get("usage"),
                "summary_count": sum(a["kind"] == "context_summary" for a in evidence["artifacts"]),
                "cost_estimate": cost, "billed_cost": None, "initial_sha256": state["initial_sha256"],
                "final_sha256": final_hashes, "evidence_directory": TRIAL,
                "budget_reservation": "unknown_full_50_retained", "automatic_recovery": False, "replayed": False,
                "manual_formal_close": {"run_id": RUN, "audit": "formal17-close-audit.json",
                    "reason": "user_requested_cancel_pending_unattested_command"}}
            record["timing"] = trial_metrics(run, operations, tools, state["submitted_at"])
            record.update(first_pass_resolved=False, recovered_resolved=False, end_to_end_seconds=elapsed)
            formal = [r for r in report["records"] if r.get("split") == "formal"] + [record]
            fairness = optimization_fairness(formal, frozen_models=frozen["models"], frozen_selection=frozen)
            if fairness.get("status") != "PASS":
                raise EvaluationError("Formal model binding verification failed")
            result_path = trial / "result.json"
            if result_path.exists() and json.loads(result_path.read_text(encoding="utf-8")) != record:
                raise EvaluationError("Existing result cannot be replaced")
            save(trial / "operations.json", operations)
            save(trial / "acceptance.json", acceptance)
            save(result_path, record)
            report["records"].append(record)
            report["fairness"] = fairness
            report["summary"] = summarize_optimization(report["records"])
            report["budget"] = ledger.snapshot()
            report["status"] = "formal_manual_close_complete"
            report.pop("failure", None)
            save(report_path, report)
            state["stage"] = "recorded"
            save(state_path, state)
            return "recorded"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--execute", action="store_true")
    choice.add_argument("--verify", action="store_true", help="Read-only checks; never send cancel")
    args = parser.parse_args()
    if args.verify:
        print(close_formal17(verify_only=True))
        return
    if not args.execute:
        print("Audit-only closure requires explicit --execute; no operations sent.")
        return
    print(close_formal17())


if __name__ == "__main__":
    main()
