"""Archive an explicitly selected, independently audited formal failure.

--verify performs the same live, read-only checks without recording anything.
--execute atomically records the existing evidence and never changes the ledger.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx

from omniharness.evaluation_budget import BudgetLedger
from omniharness.reference_cost import calculate_reference_cost
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
    verify_formal_runtime,
)
from scripts.m4_formal17_close import assert_no_resources, docker_scan, get_json

ROOT = Path("G:/harness/.omniharness/m4-optimization")
SESSION = "m4-opt-d65b8cffaa3345d3"
def validate_terminal(run, operations, tools, reservation, *, run_id, uncertain_id, model_calls, tool_calls):
    if run.get("id") != run_id or run.get("status") != "cancelled" or not run.get("finished_at"):
        raise EvaluationError("Original Run is not the confirmed cancelled formal Run")
    if not reservation or reservation.get("state") != "unknown" or reservation.get("maximum") != 50:
        raise EvaluationError("Original unknown 50-call reservation must remain intact")
    uncertain = [x for x in operations if x.get("status") != "completed"]
    if (len(operations) != model_calls + tool_calls or len(uncertain) != 1
            or uncertain[0].get("operation_id") != uncertain_id
            or uncertain[0].get("status") != "uncertain" or uncertain[0].get("operation_kind") != "model_calls"
            or len(tools) != tool_calls or any(x.get("status") != "completed" or not x.get("reclaimed_at")
                                       or x.get("slot_claimed") for x in tools)):
        raise EvaluationError("Operation evidence differs from the reviewed failure")


def validate_existing_result(path, record):
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != record:
        raise EvaluationError("Existing result cannot be replaced")


def validate_snapshot_audit(audit, commands):
    expected = {c["snapshot_id"] for c in commands}
    if not commands:
        if audit.get("snapshots", []) or audit.get("known_db_snapshot_ids", []):
            raise EvaluationError("Snapshot evidence contradicts the absence of commands")
        metadata = audit.get("snapshot_verification")
        if metadata is not None and metadata.get("count") != 0:
            raise EvaluationError("Snapshot count contradicts the absence of commands")
    elif "snapshots" in audit:
        if (audit.get("snapshot_ids_match_database") is not True
                or {x.get("id") for x in audit["snapshots"]} != expected
                or any(x.get("manifest_matches") is not True or x.get("linux_present") is not True
                       for x in audit["snapshots"])):
            raise EvaluationError("Audited snapshot identities differ")
    else:
        metadata = audit.get("snapshot_verification", {})
        if (not all(metadata.get(k) is True for k in ("command_snapshot_ids_match_database",
                    "native_manifests_identity_match", "linux_snapshot_directories_exist"))
                or metadata.get("count") != len(commands)
                or set(audit.get("known_db_snapshot_ids") or []) != expected):
            raise EvaluationError("Audited snapshot verification is incomplete")


def audited_elapsed_valid(audit):
    """Wall time is comparable only when the independent audit attests continuity."""
    return audit.get("elapsed_time_valid") is True


def validate_acceptance(acceptance, hashes, image, language):
    groups = acceptance.get("groups") or []
    passed = sum(x.get("status") == "PASS" for x in groups)
    typed = acceptance.get("typescript_typecheck") == ("PASS" if language == "typescript" else "not_applicable")
    computed_pass = passed == 5 and acceptance.get("exit_code") == 0 and typed
    if (hashes != acceptance.get("candidate_sha256") or acceptance.get("status") not in {"PASS", "FAIL"}
            or acceptance.get("image") != image or type(acceptance.get("exit_code")) is not int
            or acceptance.get("groups_complete") is not True or not 1 <= passed <= 5
            or acceptance.get("groups_passed") != passed or len(groups) != 5
            or {x.get("group") for x in groups} != set(range(1, 6))
            or any(x.get("status") not in {"PASS", "FAIL"} for x in groups)
            or acceptance.get("typescript_typecheck") not in (
                {"PASS", "FAIL"} if language == "typescript" else {"not_applicable"})
            or (acceptance.get("status") == "PASS") != computed_pass
            or acceptance.get("sandbox", {}).get("reclaimed") is not True):
        raise EvaluationError("Independent acceptance does not match current files")


def archive(sequence, root=ROOT, *, verify_only=False):
    if type(sequence) is not int or not 16 <= sequence <= 105:
        raise EvaluationError("Explicit formal sequence must be between 16 and 105")
    campaign = root / "runs" / SESSION
    report_path = campaign / "report.json"
    reservation_id = f"{SESSION}-{sequence}"
    ledger = BudgetLedger(root / "budget.sqlite3")
    with ledger.runner_lock():
        plan = json.loads((root / "plan-hy3-glm.json").read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        entry = report["queue"][sequence - 1]
        if entry.get("sequence") != sequence or entry.get("phase") not in {"first", "repeats"}:
            raise EvaluationError("Sequence does not identify a frozen formal slot")
        label = f"{sequence:03d}-{entry['task_id']}-{entry['condition']}-r{entry['repetition']}"
        trial = campaign / label
        state_path = trial / "trial-state.json"
        acceptance_path = trial / "independent-reaudit" / "acceptance-reaudit.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        audit = json.loads((root / f"review-{sequence:03d}-static-audit.json").read_text(encoding="utf-8"))
        acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
        frozen = report.get("formal_freeze") or {}
        run_id = state.get("run_id")
        unresolved = audit.get("noncompleted_operations") or []
        if (len(unresolved) != 1 or unresolved[0].get("status") != "uncertain"
                or unresolved[0].get("operation_kind") != "model_calls"):
            raise EvaluationError("Audit must identify exactly one unknown model operation")
        uncertain_id = unresolved[0].get("operation_id")
        if (report.get("session") != SESSION or report.get("plan") != plan or not run_id or not uncertain_id
                or frozen.get("implementation_id") != plan["implementation_id"] or frozen.get("sandbox") != plan["sandbox"]
                or frozen.get("quote_snapshot") != report.get("quote_snapshot")
                or frozen.get("strong_model_id") != plan["strong_model_id"]
                or frozen.get("economy_model_id") != plan["economy_model_id"]
                or frozen.get("models") != report.get("models")
                or state.get("reservation") != reservation_id or state.get("implementation_id") != plan["implementation_id"]
                or audit.get("run_id") != run_id or audit.get("terminal_status") != "cancelled"
                or audit.get("schema_version") != 1 or not audit.get("audited_at")
                or any(audit.get("actions_sent", {}).get(k) is not False for k in ("model", "command", "cancel", "resume"))):
            raise EvaluationError("Frozen trial identity differs")
        matches = [row for row in report["records"] if row.get("sequence") == sequence]
        if matches:
            row = matches[0]
            reservation = next((r for r in ledger.snapshot()["reservations"] if r["id"] == reservation_id), None)
            path = trial / "result.json"
            if (len(matches) == 1 and row.get("manual_formal_close", {}).get("run_id") == run_id
                    and row.get("run_status") == "cancelled" and row.get("resolved") is False
                    and reservation and reservation.get("state") == "unknown" and reservation.get("maximum") == 50
                    and path.exists() and json.loads(path.read_text(encoding="utf-8")) == row):
                return "already_recorded"
            raise EvaluationError("Existing record cannot be replaced")
        if len(report["records"]) != sequence - 1 or {r["sequence"] for r in report["records"]} != set(range(1, sequence)):
            raise EvaluationError("Previous slots do not match the frozen queue")
        _, tasks, manifest = load_optimization_tasks()
        task = tasks[entry["task_id"]]
        if task.get("split") != "formal":
            raise EvaluationError("Task is not formal")
        if manifest != frozen.get("manifest_sha256"):
            raise EvaluationError("Frozen manifest differs")
        workspace = Path(state["workspace_path"]).resolve()
        if (workspace != (Path("G:/OmniHarness-M4-Fixtures/optimization") / SESSION / label).resolve()
                or Path(state["workspace"]["host_path"]).resolve() != workspace):
            raise EvaluationError("Workspace path differs from the exact formal trial")
        files = {name: candidate_bytes(workspace / name) for name in task["write_allow"]}
        hashes = {name: digest(value) for name, value in files.items()}
        baseline = {name: digest(candidate_bytes(Path("examples/evaluation/optimization-v1") / task["id"] /
                                                 "workspace" / name)) for name in task["write_allow"]}
        if state.get("initial_sha256") != baseline:
            raise EvaluationError("Original workspace baseline differs from frozen task")
        validate_acceptance(acceptance, hashes, plan["sandbox"]["image"], task["language"])
        reservation = next((r for r in ledger.snapshot()["reservations"] if r["id"] == reservation_id), None)
        verify_formal_runtime(plan, frozen)
        base = plan["api_url"]
        cookie = Path(plan["session_cookie_file"]).read_text(encoding="utf-8").strip()
        key = Path(plan["sandbox"]["key_file"]).read_text(encoding="utf-8").strip()
        with httpx.Client(base_url=base, cookies={"omniharness_session": cookie}, headers={"Origin": base,
                          "X-Omni-Request": "1"}, timeout=30, trust_env=False) as client:
            run = get_json(client, "/api/v1/runs/" + run_id)
            evidence = {name: get_json(client, "/api/v1/runs/" + run_id + "/" + name)
                        for name in ("tools", "operations", "artifacts", "budget")}
            tools, operations = evidence["tools"], evidence["operations"]
            budget = evidence["budget"]
            model_calls, tool_calls = budget.get("model_calls"), budget.get("tool_calls")
            if (type(model_calls) is not int or type(tool_calls) is not int or model_calls < 1 or tool_calls < 0
                    or audit.get("budget", {}).get("model_calls") != model_calls
                    or audit.get("budget", {}).get("tool_calls") != tool_calls
                    or audit.get("budget", {}).get("reservation", {}).get("id") != reservation_id
                    or audit.get("budget", {}).get("reservation", {}).get("state") != "unknown"
                    or audit.get("budget", {}).get("reservation", {}).get("maximum") != 50
                    or audit.get("budget", {}).get("reservation", {}).get("actual_calls") is not None):
                raise EvaluationError("Audited budget differs")
            validate_terminal(run, operations, tools, reservation, run_id=run_id, uncertain_id=uncertain_id,
                              model_calls=model_calls, tool_calls=tool_calls)
            if (run.get("workspace_id") != state["workspace"]["id"] or run.get("conversation_id") != state["conversation_id"]
                    or audit.get("tools_all_completed_reclaimed") is not True or audit.get("tools_count") != tool_calls
                    or set(audit.get("tool_types") or []) != {t.get("tool_name") for t in tools}
                    or audit.get("final_files_match") != {name: True for name in task["write_allow"]}
                    or not isinstance(audit.get("patches"), list) or not isinstance(audit.get("commands"), list)):
                raise EvaluationError("Static audit or Run identity differs")
            frozen_checks = audit.get("frozen_checks") or {}
            required_patch = {"completed_and_reclaimed", "artifacts_valid", "path_known", "expected_previous_matches",
                              "receipt_before_matches", "receipt_after_matches", "receipt_identity_matches"}
            required_command = {"completed_and_reclaimed", "artifacts_valid", "ids_match", "exit_code_recorded",
                                "controller_not_found", "docker_absent"}
            if (not frozen_checks or any(value is not True for value in frozen_checks.values())
                    or any(set(x.get("checks") or {}) != required_patch or
                           any(v is not True for v in x["checks"].values()) for x in audit["patches"])
                    or any(set(x.get("checks") or {}) != required_command or
                           any(v is not True for v in x["checks"].values()) for x in audit["commands"])):
                raise EvaluationError("Original patch or snapshot audit is incomplete")
            patches = []
            for t in tools:
                if t["tool_name"] == "workspace_patch":
                    p, receipt = t["parameters"], t["result"]
                    patches.append({"operation_id": t["id"], "path": p["path"],
                                    "receipt_before_sha256": receipt["before_sha256"], "receipt_sha256": receipt["sha256"]})
            if Counter(p["path"] for p in patches) != Counter(p["path"] for p in audit["patches"]):
                raise EvaluationError("Patch identities differ from the audit")
            commands = []
            checked = {}
            with httpx.Client(timeout=20, trust_env=False) as controller:
                def list_controller():
                    response = controller.get(plan["sandbox"]["endpoint"] + "/v1/sandboxes",
                        headers={"OPEN-SANDBOX-API-KEY": key})
                    response.raise_for_status()
                    return response.json()
                assert_no_resources(list_controller, docker_scan, run_id)
                for t in [x for x in tools if x["tool_name"] == "run_command"]:
                    sid = t["sandbox_id"]
                    response = controller.get(plan["sandbox"]["endpoint"] + "/v1/sandboxes/" + sid,
                        headers={"OPEN-SANDBOX-API-KEY": key})
                    command = subprocess.run(["wsl.exe", "-d", "OmniHarnessCheckAlt", "-u", "root", "--exec",
                        "docker", "ps", "-aq", "--filter", "name=^/sandbox-" + sid + "$"], capture_output=True,
                        check=False, timeout=20)
                    checked[sid] = bool(command.stdout.strip())
                    if response.status_code != 404 or command.returncode or checked[sid]:
                        raise EvaluationError("A command sandbox could not be proven reclaimed")
                    commands.append({"operation_id": t["id"], "sandbox_id": sid, "command_id": t["command_id"],
                        "snapshot_id": t["snapshot_id"], "exit_code": t["result"]["exit_code"],
                        "reclaimed_at": t["reclaimed_at"], "slot_claimed": False, "controller_status": 404})
            if {c["operation_id"] for c in commands} != {c["operation_id"] for c in audit["commands"]}:
                raise EvaluationError("Command identities differ from the audit")
            audit_commands = {c["operation_id"]: c for c in audit["commands"]}
            if any(any(audit_commands[c["operation_id"]].get(k) != c[k] for k in ("sandbox_id", "snapshot_id", "exit_code"))
                   for c in commands):
                raise EvaluationError("Command receipts differ from the independently audited IDs")
            validate_snapshot_audit(audit, commands)
            review = {"completed_tool_evidence": {"run_id": run_id, "frozen_tool_policy": run["workspace"]["tool_policy"],
                "patches": patches, "commands": commands, "docker_listing_exit_code": 0, "docker_sandbox_matches": checked}}
            reviewed_completed_tools(review, run, evidence, state, task, client)
            start, end = datetime.fromisoformat(state["submitted_at"]), datetime.fromisoformat(run["finished_at"])
            if end < start:
                raise EvaluationError("Original terminal time is invalid")
            cost = calculate_reference_cost(operations, expected_calls=model_calls, snapshot=report["quote_snapshot"], at=start)
            if cost.get("status") not in {"PARTIAL", "UNKNOWN"} or cost.get("reference_cost") is not None:
                raise EvaluationError("Unknown model usage cannot be reported complete")
            elapsed = (end - start).total_seconds()
            elapsed_valid = audited_elapsed_valid(audit)
            record = {**entry, "split": task["split"], "complexity": task["complexity"],
                "implementation_id": state["implementation_id"], "run_id": run_id, "run_status": "cancelled",
                "run_error_code": run.get("error_code"), "agent_strategy": run.get("agent_strategy"),
                "execution_config": run.get("execution_config"), "tool_policy": run["workspace"]["tool_policy"],
                "acceptance": acceptance, "resolved": False, "latency_seconds": elapsed, "elapsed_time_valid": elapsed_valid,
                "active_seconds": budget.get("active_seconds"), "model_calls": model_calls, "tool_calls": tool_calls,
                "usage": run.get("usage"), "summary_count": sum(x["kind"] == "context_summary" for x in evidence["artifacts"]),
                "cost_estimate": cost, "billed_cost": None, "initial_sha256": state["initial_sha256"],
                "final_sha256": hashes, "evidence_directory": label, "budget_reservation": "unknown_full_50_retained",
                "automatic_recovery": False, "replayed": False, "first_pass_resolved": False,
                "recovered_resolved": False, "end_to_end_seconds": None,
                "manual_formal_close": {"run_id": run_id, "audit": f"review-{sequence:03d}-static-audit.json",
                                         "reason": "unknown_model_result_no_replay"}}
            if not elapsed_valid:
                record["elapsed_time_invalid_reason"] = "continuity_not_attested_in_independent_audit"
            record["timing"] = trial_metrics(run, operations, tools, state["submitted_at"])
            formal = [r for r in report["records"] if r.get("split") == "formal"] + [record]
            fairness = optimization_fairness(formal, frozen_models=frozen["models"], frozen_selection=frozen)
            if fairness.get("status") != "PASS":
                raise EvaluationError("Formal binding verification failed")
            if verify_only:
                return "preflight_passed_no_actions"
            result_path = trial / "result.json"
            validate_existing_result(result_path, record)
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--sequence", type=int, required=True)
    args = parser.parse_args()
    if not args.verify and not args.execute:
        print("Pass --verify or --execute; no action taken.")
        return
    print(archive(args.sequence, verify_only=args.verify))


if __name__ == "__main__":
    main()
