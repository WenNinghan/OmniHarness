"""Opt-in frozen REST evaluation; candidates only run in isolated sandboxes.

Default CLI mode describes a plan without network, model calls or file writes.
Schema 1 retains the original A/B/C wiring smoke. Schema 2 runs the approved
development/formal five-condition design with one durable shared request budget.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import shutil
import stat
import subprocess
import time
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from statistics import median
from uuid import uuid4

import httpx

from omniharness.context_policy import context_budget, parse_context_configuration
from omniharness.evaluation_budget import AUTHORIZATION, BudgetLedger, EvaluationBudgetExhausted
from omniharness.providers import NATIVE_AGENT_STREAM, agent_streaming_enabled
from omniharness.reference_cost import calculate_reference_cost, load_reference_prices
from omniharness.sandbox import SandboxConfig, SandboxExecutor

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("think", "plan", "dispatch", "execute", "review")
TASKS = ("python-001", "typescript-001")
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
STOPPED = {"paused", "awaiting_approval", "pending_review"}
REFERENCE_SOLUTIONS = {
    "python-001": """def merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result
""",
    "typescript-001": "export function numericSort(values: number[]): number[] { return [...values].sort((a, b) => a - b); }\n",
}


class EvaluationError(RuntimeError):
    """Static operational diagnosis, without credentials or arbitrary response bodies."""


def save(path: Path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            # Observed Windows replace contention: preserve the previous file,
            # retry only the local rename briefly, never replay a paid request.
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_tasks():
    dataset = json.loads((ROOT / "examples/evaluation/tasks.json").read_text(encoding="utf-8"))
    selected = {task["id"]: task for task in dataset["tasks"] if task["id"] in TASKS}
    if set(selected) != set(TASKS):
        raise EvaluationError("The two fixed evaluation fixtures are unavailable")
    return dataset, selected


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise EvaluationError("Evaluation plan schema must be 1")
    if (set(plan.get("stage_models", {})) != set(STAGES) or not plan.get("baseline_model_id")
            or len(set(plan["stage_models"].values())) < 2):
        raise EvaluationError("C needs five explicit stage bindings and at least two different model configurations")
    if any(not isinstance(item, str) or not item for item in [plan["baseline_model_id"], *plan["stage_models"].values()]):
        raise EvaluationError("Model configuration IDs must be nonempty strings")
    url = httpx.URL(plan.get("api_url", ""))
    if (url.scheme not in {"http", "https"} or url.host not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.query or url.path not in {"", "/"}):
        raise EvaluationError("The paired evaluation API must be an explicit loopback origin")
    for field in ("workspace_root", "output_root", "session_cookie_file"):
        if not isinstance(plan.get(field), str) or not Path(plan[field]).is_absolute():
            raise EvaluationError("Evaluation storage and session files require absolute local paths")
    work, output = Path(plan["workspace_root"]).resolve(), Path(plan["output_root"]).resolve()
    if work == output or work in output.parents or output in work.parents:
        raise EvaluationError("Result/acceptance storage must be outside registered Agent workspaces")
    sandbox = plan.get("sandbox", {})
    if (not isinstance(sandbox, dict) or not isinstance(sandbox.get("key_file"), str)
            or not Path(sandbox["key_file"]).is_absolute()):
        raise EvaluationError("Independent acceptance requires an explicit local controller credential file")
    # Validate without reading a credential or contacting the controller.
    SandboxConfig(endpoint=sandbox.get("endpoint", ""), api_key="validation-only-" * 3,
                  image=sandbox.get("image", ""), snapshot_root=sandbox.get("snapshot_root", ""),
                  instance_id="m4-evaluation")
    return plan


def schedule(repetitions: int, seed: int):
    if type(repetitions) is not int or not 1 <= repetitions <= 30:
        raise EvaluationError("Repetitions must be between 1 and 30")
    result = [(task, condition, repeat) for repeat in range(1, repetitions + 1)
              for task in TASKS for condition in ("A", "B", "C")]
    random.Random(seed).shuffle(result)
    return result


def prompt(task):
    return "\n".join([task["input"]["prompt"], *task["input"]["constraints"],
        "项目中的 README 说明了接口和公开 smoke 命令。请直接修改授权文件并用真实工具验证；最终报告修改与实际测试结果。",
        "独立验收由宿主在任务结束后运行，公开 smoke 通过不代表全部验收通过。不要请求联网、安装依赖或修改权限。"])


def model_preflight(plan, rows):
    models = {row["id"]: row for row in rows}
    selected = {plan["baseline_model_id"], *plan["stage_models"].values()}
    fairness = None
    for identity in selected:
        row = models.get(identity)
        if (not row or not row.get("enabled") or row.get("adapter_type") == "mock"
                or (row.get("capabilities", {}).get("tools") or {}).get("status") != "supported"):
            raise EvaluationError("Every selected model requires enabled live configuration and tested tool support")
        policy, limits = parse_context_configuration(row.get("context_policy"))
        effective = context_budget(policy, limits, output_tokens=row["max_output_tokens"])
        signature = {"policy": asdict(policy), "input_limit": effective.input_limit,
                     "max_output_tokens": row["max_output_tokens"]}
        if fairness is not None and signature != fairness:
            raise EvaluationError("Selected models differ in context policy, effective input or output cap")
        fairness = signature
    return {identity: models[identity] for identity in selected}, fairness


def submission(plan, models, condition, task, workspace_id, request_id):
    assignment = plan["stage_models"] if condition == "C" else {stage: plan["baseline_model_id"] for stage in STAGES}
    return {
        "content": prompt(task), "model_config_id": plan["baseline_model_id"],
        "workflow_type": "agent", "agent_strategy": "single" if condition == "A" else "staged",
        "workspace_id": workspace_id, "client_request_id": request_id,
        "stage_overrides": [{"stage": stage, "model_config_id": assignment[stage],
            "model_version": models[assignment[stage]]["version"],
            "provider_version": models[assignment[stage]]["provider_version"]} for stage in STAGES],
    }


def candidate_bytes(path: Path):
    information = path.lstat()
    if (not stat.S_ISREG(information.st_mode) or information.st_nlink != 1
            or getattr(information, "st_file_attributes", 0) & 0x400 or information.st_size > 256 * 1024):
        raise EvaluationError("Candidate is not a bounded ordinary file")
    return path.read_bytes()


def acceptance_files(task, candidate: bytes, marker: str):
    code = task["verification_method"]["test_code"]
    if task["language"] == "python":
        test = code + f'\nprint({marker!r})\n'
        return {"candidate.py": candidate.decode("utf-8"), "acceptance.py": test}, ["python", "-B", "acceptance.py"]
    test = code.replace('"./candidate.js"', '"./candidate.ts"') + f'\nconsole.log({json.dumps(marker)});\n'
    return {"candidate.ts": candidate.decode("utf-8"), "acceptance.ts": test}, ["node", "--experimental-strip-types", "acceptance.ts"]


# Fixed host-side file transfer only. Never executes a candidate on the host.
SNAPSHOT_WRITER = r'''
import json, os, pathlib, re, sys
root = pathlib.Path(sys.argv[1])
name = sys.argv[2]
if not root.is_absolute() or '..' in root.parts or not re.fullmatch(r'evaluation-[a-f0-9]{32}', name):
    raise SystemExit(2)
if not root.is_dir() or root.resolve() != root or os.getuid() == 10001:
    raise SystemExit(3)
target = root / name
target.mkdir(mode=0o755, exist_ok=False)
files = json.load(sys.stdin)
allowed = {'candidate.py','candidate.ts','acceptance.py','acceptance.ts'}
if not isinstance(files, dict) or not files or set(files) - allowed:
    raise SystemExit(4)
for filename, value in files.items():
    data = value.encode('utf-8')
    if len(data) > 512 * 1024:
        raise SystemExit(5)
    with open(target / filename, 'xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(target / filename, 0o444)
os.chmod(target, 0o555)
'''


class SandboxAcceptance:
    def __init__(self, settings: dict):
        self.settings = settings
        self.config = SandboxConfig(endpoint=settings["endpoint"],
            api_key=Path(settings["key_file"]).read_text(encoding="utf-8").strip(), image=settings["image"],
            snapshot_root=settings["snapshot_root"], instance_id="m4-evaluation", max_log_bytes=2 * 1024 * 1024)
        self.executor = SandboxExecutor(self.config)

    def _transfer(self, name, files):
        args = ["python3", "-c", SNAPSHOT_WRITER, self.config.snapshot_root, name]
        if self.settings.get("wsl_distro"):
            args = ["wsl.exe", "-d", self.settings["wsl_distro"], "-u", "root", "--exec", *args]
        result = subprocess.run(args, input=json.dumps(files).encode(), capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise EvaluationError("Independent acceptance snapshot could not be prepared")

    async def evaluate(self, task, candidate: bytes, output: Path):
        marker = "OMNI_ACCEPTANCE_COMPLETE_" + uuid4().hex
        files, command = acceptance_files(task, candidate, marker)
        name = "evaluation-" + uuid4().hex
        await asyncio.to_thread(self._transfer, name, files)
        lines, identities = [], {}

        async def log(channel, value):
            lines.append({"channel": channel, "text": value})

        async def created(sandbox_id):
            identities["sandbox_id"] = sandbox_id
            save(output / "acceptance-dispatch.json", identities)

        async def started(sandbox_id, command_id):
            identities.update(sandbox_id=sandbox_id, command_id=command_id)
            save(output / "acceptance-dispatch.json", identities)

        async def reclaimed(sandbox_id):
            identities["reclaimed"] = True
            save(output / "acceptance-dispatch.json", identities)

        started_at = time.monotonic()
        try:
            result = await self.executor.execute(run_id=name, operation_id="independent-acceptance",
                snapshot_path=str(PurePosixPath(self.config.snapshot_root) / name), command=command,
                log_sink=log, started_sink=started, created_sink=created, reclaimed_sink=reclaimed,
                timeout_seconds=30)
            completed = marker in "".join(item["text"] for item in lines if item["channel"] == "stdout").splitlines()
            return {"status": "PASS" if result.exit_code == 0 and completed else "FAIL",
                    "exit_code": result.exit_code, "completion_marker_seen": completed,
                    "seconds": time.monotonic() - started_at, "sandbox": identities,
                    "image": self.config.image, "candidate_sha256": digest(candidate),
                    "test_sha256": digest(task["verification_method"]["test_code"].encode()),
                    "typescript_typecheck": "UNVERIFIED" if task["language"] == "typescript" else "not_applicable"}
        finally:
            save(output / "acceptance-logs.json", lines)


class EvaluationRunner:
    def __init__(self, plan, client, acceptance, *, poll_seconds=1, timeout_seconds=1900):
        self.plan, self.client, self.acceptance = plan, client, acceptance
        self.poll_seconds, self.timeout_seconds = poll_seconds, timeout_seconds

    def api(self, method, path, **kwargs):
        response = self.client.request(method, "/api/v1" + path, **kwargs)
        if response.status_code >= 400:
            raise EvaluationError(f"Evaluation API {method} failed with HTTP {response.status_code}")
        return response.json()

    async def wait(self, run_id):
        started, blocked_reason = time.monotonic(), None
        while True:
            run = self.api("GET", f"/runs/{run_id}")
            if run["status"] in TERMINAL:
                return run, blocked_reason
            if run["status"] in STOPPED or time.monotonic() - started > self.timeout_seconds:
                blocked_reason = run["status"] if run["status"] in STOPPED else "evaluation_timeout"
                # Only cancel the Run created by this trial. Never approve an operation.
                self.api("POST", f"/runs/{run_id}/cancel")
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    run = self.api("GET", f"/runs/{run_id}")
                    if run["status"] in TERMINAL:
                        return run, blocked_reason
                    await asyncio.sleep(self.poll_seconds)
                raise EvaluationError("Evaluation Run cancellation did not settle; stop before the next trial")
            await asyncio.sleep(self.poll_seconds)

    def records(self, run, conversation_id, output):
        result = {name: self.api("GET", f"/runs/{run['id']}/{name}") for name in
                  ("tools", "approvals", "artifacts", "budget", "agent-runs", "operations")}
        result["messages"] = self.api("GET", f"/conversations/{conversation_id}/messages")
        result["run"] = run
        events = []
        # Terminal SSE finishes after all persisted pages. Retain event IDs for audits.
        with self.client.stream("GET", f"/api/v1/runs/{run['id']}/events") as response:
            if response.status_code != 200:
                raise EvaluationError("Evaluation event evidence is unavailable")
            for line in response.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        result["events"] = events
        for name, value in result.items():
            save(output / (name + ".json"), value)
        return result

    async def trial(self, task, condition, repeat, sequence, session, models, root):
        label = f"{sequence:03d}-{task['id']}-{condition}-r{repeat}"
        directory = root / label
        directory.mkdir()
        workspace = Path(self.plan["workspace_root"]) / session / label
        source = ROOT / "examples/evaluation/m4" / task["language"]
        shutil.copytree(source, workspace)  # New directory every trial, never reset user work.
        initial = candidate_bytes(workspace / task["input"]["filename"])
        initial_hash = digest(initial)
        if initial != task["input"]["source_code"].encode():
            raise EvaluationError("Public example diverges from the fixed versioned dataset")
        grant = self.api("POST", "/workspaces", json={"name": label, "host_path": str(workspace.resolve()),
            "read_allow": ["."], "write_allow": [task["input"]["filename"]], "command_approval": False})
        if not grant.get("ready"):
            raise EvaluationError("Evaluation workspace mount was not verified")
        conversation = self.api("POST", "/conversations", json={"title": "M4 evaluation " + label})
        body = submission(self.plan, models, condition, task, grant["id"], session + "-" + str(sequence))
        save(directory / "input.json", {"task_id": task["id"], "condition": condition, "repeat": repeat,
             "submission": body, "workspace": grant, "initial_sha256": initial_hash})
        started = time.monotonic()
        queued = self.api("POST", f"/conversations/{conversation['id']}/messages", json=body)
        save(directory / "created-run.json", queued)
        try:
            run, blocked = await self.wait(queued["run_id"])
        except BaseException:
            # An interrupted evaluation must not silently leave its paid Run
            # dispatching work. No retry of Run creation or approval follows.
            try:
                self.api("POST", f"/runs/{queued['run_id']}/cancel")
            except Exception:
                save(directory / "cancellation-unconfirmed.json", {"run_id": queued["run_id"], "status": "UNKNOWN"})
            raise
        elapsed = time.monotonic() - started
        evidence = self.records(run, conversation["id"], directory)
        final = candidate_bytes(workspace / task["input"]["filename"])
        (directory / task["input"]["filename"]).write_bytes(final)
        if blocked:
            acceptance = {"status": "BLOCKED", "reason": blocked}
        else:
            try:
                acceptance = await self.acceptance.evaluate(task, final, directory)
            except Exception as exc:
                acceptance = {"status": "BLOCKED", "reason": type(exc).__name__}
        save(directory / "acceptance.json", acceptance)
        budget, artifacts, tools = evidence["budget"], evidence["artifacts"], evidence["tools"]
        record = {"task_id": task["id"], "condition": condition, "repetition": repeat, "sequence": sequence,
            "run_id": run["id"], "run_status": run["status"], "workspace_id": grant["id"],
            "run_error_code": run.get("error_code"),
            "agent_strategy": run.get("agent_strategy"), "execution_config": run.get("execution_config"),
            "tool_policy": (run.get("workspace") or {}).get("tool_policy"),
            "initial_sha256": initial_hash, "final_sha256": digest(final), "acceptance": acceptance,
            "latency_seconds": elapsed, "active_seconds": budget.get("active_seconds"),
            "model_calls": budget.get("model_calls"), "tool_calls": budget.get("tool_calls"),
            "summary_count": sum(a["kind"] == "context_summary" for a in artifacts),
            "usage": run.get("usage"), "cost_estimate": None, "billed_cost": None,
            "approval_count": len(evidence["approvals"]),
            "unknown_tool_results": sum(item["status"] == "uncertain" for item in tools),
            "repair_iterations": budget.get("revisions"), "valid_review_findings": None,
            "review_false_positives": None, "leakage_check": "UNVERIFIED",
            "changed": initial_hash != digest(final), "evidence_directory": label}
        save(directory / "result.json", record)
        return record


def fairness_check(records):
    signatures = []
    for record in records:
        config = record.get("execution_config") or {}
        model_data = config.get("models") or {}
        model_signatures = []
        for model in model_data.values():
            policy, limits = parse_context_configuration(model.get("context_policy"))
            cap = model.get("max_output_tokens")
            if not isinstance(cap, int):
                return {"status": "UNVERIFIED", "reason": "Frozen model output limit is unavailable"}
            model_signatures.append({"policy": asdict(policy), "input": context_budget(policy, limits, output_tokens=cap).input_limit,
                                     "output": cap})
        if model_signatures and any(item != model_signatures[0] for item in model_signatures):
            return {"status": "FAIL", "reason": "Frozen model context/output policies differ"}
        if record.get("agent_strategy") != ("single" if record["condition"] == "A" else "staged"):
            return {"status": "FAIL", "reason": "Actual execution strategy does not match the comparison condition"}
        signatures.append({"budget": config.get("budget"), "sandbox": config.get("sandbox"),
                           "tool_policy": record.get("tool_policy"),
                           "context": model_signatures[0] if model_signatures else None,
                           "present": bool(config.get("budget") and config.get("sandbox") and model_data and record.get("tool_policy"))})
    if not signatures or any(not item["present"] for item in signatures):
        return {"status": "UNVERIFIED", "reason": "Frozen execution metadata is incomplete"}
    equal = all(item == signatures[0] for item in signatures)
    return {"status": "PASS" if equal else "FAIL", "reason": "Frozen budgets and sandbox profiles compared"}


async def run(plan, *, repetitions=1, seed=43, client=None, acceptance=None):
    validate_plan(plan)
    dataset, tasks = load_tasks()
    queue = schedule(repetitions, seed)
    session = "m4-eval-" + uuid4().hex[:16]
    root = Path(plan["output_root"]) / session
    root.mkdir(parents=True, exist_ok=False)
    own_client = client is None
    if own_client:
        cookie = Path(plan["session_cookie_file"]).read_text(encoding="utf-8").strip()
        client = httpx.Client(base_url=plan["api_url"], cookies={"omniharness_session": cookie},
            headers={"Origin": plan["api_url"], "X-Omni-Request": "1"}, timeout=30, trust_env=False,
            follow_redirects=False)
    manifest = {"schema_version": 1, "session": session, "dataset_id": dataset["dataset_id"],
        "dataset_version": dataset["dataset_version"], "seed": seed, "repetitions": repetitions,
        "started_at": datetime.now(timezone.utc).isoformat(), "status": "running", "records": [],
        "purpose": "wiring_smoke" if repetitions == 1 else "repeated_small_task_evaluation",
        "quality_advantage_claim": None, "cost_estimate": None, "billed_cost": None}
    save(root / "report.json", manifest)
    try:
        runner = EvaluationRunner(plan, client, acceptance or SandboxAcceptance(plan["sandbox"]))
        models, policy = model_preflight(plan, runner.api("GET", "/model-configs"))
        save(root / "models.json", models)
        manifest["context_fairness"] = policy
        calibration = []
        for task_id, task in tasks.items():
            for label, candidate, expected in (("known-defect", task["input"]["source_code"], "FAIL"),
                                               ("known-reference", REFERENCE_SOLUTIONS[task_id], "PASS")):
                directory = root / ("calibration-" + task_id + "-" + label)
                directory.mkdir()
                result = await runner.acceptance.evaluate(task, candidate.encode(), directory)
                calibration.append({"task_id": task_id, "fixture": label, "expected": expected, "actual": result})
                save(root / "calibration.json", calibration)
                if result.get("status") != expected:
                    raise EvaluationError("Independent acceptance failed its positive/negative calibration; no task Run was submitted")
        manifest["acceptance_calibration"] = "PASS"
        for index, (task_id, condition, repeat) in enumerate(queue, start=1):
            record = await runner.trial(tasks[task_id], condition, repeat, index, session, models, root)
            manifest["records"].append(record)
            manifest["fairness"] = fairness_check(manifest["records"])
            save(root / "report.json", manifest)
            if record["run_status"] != "completed" or record["acceptance"]["status"] == "BLOCKED":
                raise EvaluationError("A trial did not complete its runtime/acceptance path; no following Run was submitted")
            if manifest["fairness"]["status"] != "PASS":
                raise EvaluationError("Frozen runtime comparability was not confirmed; no following Run was submitted")
        manifest["status"] = "completed"
        manifest["fairness"] = fairness_check(manifest["records"])
    except BaseException as exc:
        manifest["status"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "blocked"
        manifest["failure"] = type(exc).__name__
        # Static EvaluationError strings are safe; arbitrary HTTP/OS/SDK text is not.
        if isinstance(exc, EvaluationError):
            manifest["failure_message"] = str(exc)
        raise
    finally:
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(root / "report.json", manifest)
        if own_client:
            client.close()
    return root / "report.json"


OPTIMIZATION_ROOT = ROOT / "examples/evaluation/optimization-v1"
CONDITIONS = ("Single-Pro", "Single-Flash", "Full-Pro", "Compact-Pro", "Compact-Mixed")
STRATEGIES = {"Single-Pro": "single", "Single-Flash": "single", "Full-Pro": "staged",
              "Compact-Pro": "compact", "Compact-Mixed": "compact"}


def load_optimization_tasks():
    raw = (OPTIMIZATION_ROOT / "manifest.json").read_bytes()
    expected = (OPTIMIZATION_ROOT / "manifest.sha256").read_text(encoding="utf-8").split()[0]
    if digest(raw) != expected:
        raise EvaluationError("Frozen evaluation manifest changed")
    manifest = json.loads(raw)
    tasks = {task["id"]: task for task in manifest["tasks"]}
    if len(tasks) != 9 or sum(task["split"] == "formal" for task in tasks.values()) != 6:
        raise EvaluationError("Expected three development and six formal tasks")
    for task in tasks.values():
        for relative, sha in task["files_sha256"].items():
            path = OPTIMIZATION_ROOT / task["id"] / relative
            if digest(candidate_bytes(path)) != sha:
                raise EvaluationError("A frozen task file changed")
    return manifest, tasks, expected


def optimization_schedule(seed=43):
    _, tasks, _ = load_optimization_tasks()
    rng, queue = random.Random(seed), []
    # Every task/repetition is a contiguous five-condition comparison block.
    for phase, split, repeats in (("development", "development", (1,)),
                                  ("first", "formal", (1,)), ("repeats", "formal", (2, 3))):
        blocks = [(task["id"], repeat) for repeat in repeats for task in tasks.values() if task["split"] == split]
        rng.shuffle(blocks)
        order = list(CONDITIONS)
        rng.shuffle(order)
        for block_index, (identity, repeat) in enumerate(blocks):
            rotation = block_index % len(order)
            for condition in order[rotation:] + order[:rotation]:
                queue.append({"sequence": len(queue) + 1, "phase": phase, "task_id": identity,
                              "repetition": repeat, "condition": condition})
    return queue


def validate_optimization_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != 2:
        raise EvaluationError("Optimization evaluation requires schema 2")
    strong, economy = plan.get("strong_model_id"), plan.get("economy_model_id")
    if not strong or not economy or strong == economy:
        raise EvaluationError("Two explicit distinct model configurations are required")
    compatibility = {**plan, "schema_version": 1, "baseline_model_id": strong,
                     "stage_models": {stage: economy if stage == "execute" else strong for stage in STAGES}}
    validate_plan(compatibility)
    ledger = ROOT / ".omniharness/m4-optimization/budget.sqlite3"
    if Path(plan.get("budget_ledger", "")).resolve() != ledger.resolve() or plan.get("authorization_id") != AUTHORIZATION:
        raise EvaluationError("Use the one approved campaign ledger and authorization")
    if not isinstance(plan.get("implementation_id"), str) or not plan["implementation_id"]:
        raise EvaluationError("An immutable candidate implementation ID is required")
    return plan


def optimization_models(plan, rows):
    selected, signature = model_preflight({"baseline_model_id": plan["strong_model_id"],
        "stage_models": {stage: plan["economy_model_id"] if stage == "execute" else plan["strong_model_id"]
                         for stage in STAGES}}, rows)
    if signature["policy"]["version"] != "context-v2" or signature["max_output_tokens"] != 100000:
        raise EvaluationError("All five conditions require the same context-v2 and 100K output cap")
    return selected, signature


def validate_formal_gate(report, queue, through, freeze_formal):
    """Require the approved development -> first -> repeats sequence."""
    if through == "development":
        return
    records = report.get("records", [])
    by_sequence = {row.get("sequence"): row for row in records}
    if len(by_sequence) != len(records):
        raise EvaluationError("Duplicate recorded sequence prevents formal evaluation")
    for expected in queue[:15]:
        row = by_sequence.get(expected["sequence"])
        if (not row or row.get("split") != "development"
                or any(row.get(key) != expected[key] for key in ("phase", "task_id", "condition", "repetition"))):
            raise EvaluationError("Formal evaluation requires all 15 matching development records")
    frozen = report.get("formal_freeze")
    if not frozen and not freeze_formal:
        raise EvaluationError("Formal evaluation requires explicit development freeze (--freeze-formal)")
    if through != "all":
        return
    if not frozen:
        raise EvaluationError("Repeated formal trials require the completed frozen first 30")
    first = []
    for expected in queue[15:45]:
        row = by_sequence.get(expected["sequence"])
        if (not row or row.get("split") != "formal"
                or any(row.get(key) != expected[key] for key in ("phase", "task_id", "condition", "repetition"))
                or row.get("implementation_id") != frozen.get("implementation_id")
                or (row.get("execution_config", {}).get("sandbox") or {}).get("image")
                != frozen.get("sandbox", {}).get("image")):
            raise EvaluationError("Repeated formal trials require all 30 matching frozen first-round records")
        first.append(row)
    if (report.get("fairness", {}).get("status") != "PASS"
            or optimization_fairness(first, frozen_models=frozen.get("models"), frozen_selection=frozen)["status"] != "PASS"
            or report.get("models") != frozen.get("models")
            or report.get("quote_snapshot") != frozen.get("quote_snapshot")
            or report.get("manifest_sha256") != frozen.get("manifest_sha256")):
        raise EvaluationError("Repeated formal trials require passing unchanged frozen first-round metadata")


def verify_formal_runtime(plan, frozen):
    """Read-only identity check for the explicitly configured local WSL candidate."""
    if (plan.get("api_url") != "http://127.0.0.1:18792"
            or plan.get("sandbox", {}).get("wsl_distro") != "OmniHarnessCheckAlt"
            or frozen.get("implementation_id") != plan.get("implementation_id")
            or frozen.get("sandbox") != plan.get("sandbox")):
        raise EvaluationError("Formal candidate profile does not match its frozen identity")
    image = frozen["implementation_id"]
    sandbox = frozen["sandbox"]["image"]
    prefix = ["wsl.exe", "-d", "OmniHarnessCheckAlt", "-u", "root", "--exec", "docker"]
    template = '{{.Name}}|{{.State.Running}}|{{.Image}}|{{index .Config.Labels "com.docker.compose.project"}}|{{json (index .NetworkSettings.Ports "8787/tcp")}}'
    names = ["omni-m4-preview-api-1", "omni-m4-preview-worker-1"]
    try:
        containers = subprocess.run(prefix + ["inspect", "--format", template, *names], capture_output=True,
                                    check=True, timeout=15).stdout.decode("utf-8").splitlines()
        image_id = subprocess.run(prefix + ["image", "inspect", sandbox, "--format", "{{.Id}}"],
                                  capture_output=True, check=True, timeout=15).stdout.decode("utf-8").strip()
        rows = [line.split("|", 4) for line in containers]
        if len(rows) != 2 or any(len(row) != 5 for row in rows):
            raise ValueError()
        by_name = {row[0].lstrip("/"): row for row in rows}
        if set(by_name) != set(names) or image_id != sandbox:
            raise ValueError()
        for row in rows:
            if row[1:4] != ["true", image, "omni-m4-preview"]:
                raise ValueError()
        ports = json.loads(by_name[names[0]][4])
        if not isinstance(ports, list) or {"HostIp": "127.0.0.1", "HostPort": "18792"} not in ports:
            raise ValueError()
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, UnicodeError, TypeError):
        raise EvaluationError("Formal candidate container, port or sandbox image does not match the freeze") from None


def optimization_submission(plan, models, condition, task, workspace_id, request_id):
    strong, economy = plan["strong_model_id"], plan["economy_model_id"]
    assignment = condition_assignment(condition, strong, economy)
    return {"content": task["prompt"] + "\n项目 README 给出接口与公开 smoke。直接修改授权文件并用真实工具验证。"
            "宿主在终态独立验收，公开 smoke 不代表所有要求通过；不要联网、安装依赖或修改权限。",
            "model_config_id": economy if condition == "Single-Flash" else strong,
            "workflow_type": "agent", "agent_strategy": STRATEGIES[condition],
            "workspace_id": workspace_id, "client_request_id": request_id,
            "stage_overrides": [{"stage": stage, "model_config_id": assignment[stage],
                                 "model_version": models[assignment[stage]]["version"],
                                 "provider_version": models[assignment[stage]]["provider_version"]} for stage in STAGES]}


def condition_assignment(condition, strong, economy):
    if condition not in CONDITIONS or not strong or not economy or strong == economy:
        raise EvaluationError("Formal comparison needs two distinct frozen model identities")
    return {stage: economy if condition == "Single-Flash" or (condition == "Compact-Mixed" and stage == "execute")
            else strong for stage in STAGES}


OPTIMIZATION_SNAPSHOT_WRITER = r'''
import json, os, pathlib, re, sys
root = pathlib.Path(sys.argv[1]); name = sys.argv[2]
if not root.is_absolute() or '..' in root.parts or not re.fullmatch(r'evaluation-[a-f0-9]{32}',name):raise SystemExit(2)
if not root.is_dir() or root.resolve()!=root or os.getuid()==10001:raise SystemExit(3)
files=json.load(sys.stdin)
if not isinstance(files,dict) or not 1<=len(files)<=60:raise SystemExit(4)
total=0
for relative,value in files.items():
    path=pathlib.PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or chr(92) in relative or any(not re.fullmatch(r'[A-Za-z0-9_.-]+',part) for part in path.parts):raise SystemExit(5)
    if not isinstance(value,str):raise SystemExit(6)
    total+=len(value.encode('utf-8'))
if total>4*1024*1024:raise SystemExit(7)
target=root/name;target.mkdir(mode=0o755,exist_ok=False)
for relative,value in files.items():
    path=target/relative;path.parent.mkdir(parents=True,exist_ok=True)
    with open(path,'xb') as stream:stream.write(value.encode('utf-8'));stream.flush();os.fsync(stream.fileno())
    os.chmod(path,0o444)
for directory,_,_ in os.walk(target,topdown=False):os.chmod(directory,0o555)
'''


class OptimizationAcceptance(SandboxAcceptance):
    def _transfer(self, name, files):
        args = ["python3", "-c", OPTIMIZATION_SNAPSHOT_WRITER, self.config.snapshot_root, name]
        if self.settings.get("wsl_distro"):
            args = ["wsl.exe", "-d", self.settings["wsl_distro"], "-u", "root", "--exec", *args]
        result = subprocess.run(args, input=json.dumps(files).encode(), capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise EvaluationError("Independent multi-file acceptance snapshot could not be prepared")

    async def evaluate(self, task, candidate: dict[str, bytes], output: Path):
        if set(candidate) != set(task["write_allow"]):
            raise EvaluationError("Candidate file manifest differs from the frozen task")
        source = OPTIMIZATION_ROOT / task["id"]
        files = {name: candidate_bytes(source / "workspace" / name).decode("utf-8")
                 for name in task["workspace_files"] if name not in candidate}
        files.update({name: value.decode("utf-8") for name, value in candidate.items()})
        files[task["acceptance_file"]] = candidate_bytes(source / "hidden" / task["acceptance_file"]).decode("utf-8")
        if task["language"] == "typescript":
            files["acceptance_driver.py"] = ("import subprocess,sys,json\n"
                "r=subprocess.run(['tsc','--noEmit','--project','tsconfig.json'],capture_output=True,text=True)\n"
                "print('OMNI_TYPECHECK '+json.dumps({'status':'PASS' if r.returncode==0 else 'FAIL'}),flush=True)\n"
                "print(r.stdout,end='');print(r.stderr,end='',file=sys.stderr)\n"
                "s=subprocess.run(['node','--experimental-strip-types','acceptance.ts'])\n"
                "raise SystemExit(0 if r.returncode==0 and s.returncode==0 else 1)\n")
            command = ["python", "-B", "acceptance_driver.py"]
        else:
            command = ["python", "-B", "acceptance.py"]
        name, logs, identities = "evaluation-" + uuid4().hex, [], {}
        start = time.monotonic()
        await asyncio.to_thread(self._transfer, name, files)

        async def log(channel, text):
            logs.append({"channel": channel, "text": text})

        async def created(identity):
            identities["sandbox_id"] = identity
            save(output / "acceptance-dispatch.json", identities)

        async def started(identity, command):
            identities.update(sandbox_id=identity, command_id=command)
            save(output / "acceptance-dispatch.json", identities)

        async def reclaimed(identity):
            identities["reclaimed"] = True
            save(output / "acceptance-dispatch.json", identities)

        try:
            result = await self.executor.execute(run_id=name, operation_id="independent-acceptance",
                snapshot_path=str(PurePosixPath(self.config.snapshot_root) / name), command=command,
                log_sink=log, created_sink=created, started_sink=started, reclaimed_sink=reclaimed,
                timeout_seconds=60)
            lines = "".join(item["text"] for item in logs if item["channel"] == "stdout").splitlines()
            groups = [json.loads(line.removeprefix("OMNI_GROUP ")) for line in lines if line.startswith("OMNI_GROUP ")]
            typecheck = [json.loads(line.removeprefix("OMNI_TYPECHECK ")) for line in lines if line.startswith("OMNI_TYPECHECK ")]
            complete = len(groups) == 5 and {row.get("group") for row in groups} == set(range(1, 6))
            typed = task["language"] != "typescript" or typecheck == [{"status": "PASS"}]
            passed = result.exit_code == 0 and complete and all(row["status"] == "PASS" for row in groups)
            passed = passed and typed and "OMNI_ACCEPTANCE_FINISHED" in lines and identities.get("reclaimed") is True
            return {"status": "PASS" if passed else "FAIL", "exit_code": result.exit_code,
                    "groups": groups, "groups_complete": complete, "groups_passed": sum(g["status"] == "PASS" for g in groups),
                    "typescript_typecheck": "PASS" if task["language"] == "typescript" and typed else
                        "FAIL" if task["language"] == "typescript" else "not_applicable",
                    "seconds": time.monotonic() - start, "sandbox": identities, "image": self.config.image,
                    "candidate_sha256": {key: digest(value) for key, value in candidate.items()}}
        finally:
            save(output / "acceptance-logs.json", logs)


async def calibrate_optimization(acceptance, output):
    manifest, tasks, frozen = load_optimization_tasks()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for task in tasks.values():
        for section, expected in (("workspace", "FAIL"), ("reference", "PASS")):
            directory = output / (task["id"] + "-" + section)
            directory.mkdir(exist_ok=True)
            candidate = {name: candidate_bytes(OPTIMIZATION_ROOT / task["id"] / section / name) for name in task["write_allow"]}
            result = await acceptance.evaluate(task, candidate, directory)
            results.append({"task_id": task["id"], "variant": section, "expected": expected, "actual": result})
            save(output / "calibration.json", {"manifest_sha256": frozen, "results": results})
            if result["status"] != expected or not result.get("groups_complete"):
                raise EvaluationError("A fixed reference/defect failed calibration; no paid request is permitted")
    report = {"status": "PASS", "manifest_sha256": frozen, "dataset_id": manifest["dataset_id"],
              "image": acceptance.config.image, "results": results, "model_calls_sent": 0}
    save(output / "calibration.json", report)
    return report


def optimization_fairness(records, *, frozen_models=None, frozen_selection=None):
    # Existing immutable metadata comparison is reused; only the expected
    # strategy label is normalized after validating the five real conditions.
    normalized = []
    for record in records:
        if record.get("agent_strategy") != STRATEGIES.get(record["condition"]):
            return {"status": "FAIL", "reason": "Actual strategy differs from assigned condition"}
        config = record.get("execution_config") or {}
        if config.get("workflow_version") != "stage-agent-v4":
            return {"status": "FAIL", "reason": "The actual graph is not stage-agent-v4"}
        budget = config.get("budget") or {}
        if any(budget.get(key) != value for key, value in {
                "max_model_calls": 50, "max_tool_calls": 100, "max_active_seconds": 1800}.items()):
            return {"status": "FAIL", "reason": "The frozen execution ceiling differs from the approved comparison"}
        actual_models = config.get("models") or {}
        if record.get("split") == "formal":
            if not frozen_selection or set(actual_models) != set(STAGES):
                return {"status": "UNVERIFIED", "reason": "Formal stage model assignments are incomplete"}
            try:
                expected_models = condition_assignment(record["condition"], frozen_selection.get("strong_model_id"),
                                                       frozen_selection.get("economy_model_id"))
            except EvaluationError:
                return {"status": "UNVERIFIED", "reason": "Formal selected models are unavailable"}
            for stage, expected_id in expected_models.items():
                actual, binding = actual_models.get(stage), (frozen_models or {}).get(expected_id)
                if (not isinstance(actual, dict) or not binding or type(actual.get("version")) is not int
                        or type(binding.get("version")) is not int):
                    return {"status": "UNVERIFIED", "reason": "Formal stage model version is unavailable"}
                if actual.get("id") != expected_id or actual["version"] != binding.get("version"):
                    return {"status": "FAIL", "reason": "Formal stage model ID or version differs from its condition"}
        for model in actual_models.values():
            policy, _ = parse_context_configuration(model.get("context_policy"))
            if policy.version != "context-v2" or model.get("max_output_tokens") != 100000:
                return {"status": "FAIL", "reason": "A frozen model differs from context-v2/100K"}
            if record.get("split") == "formal":
                binding = (frozen_models or {}).get(model.get("id"))
                if not binding or not model.get("transport"):
                    return {"status": "UNVERIFIED", "reason": "Formal model transport evidence is incomplete"}
                expected = (NATIVE_AGENT_STREAM if agent_streaming_enabled(binding, config["workflow_version"])
                            else "adapter-default")
                if model["transport"] != expected:
                    return {"status": "FAIL", "reason": "Actual model transport differs from its formal frozen capability"}
        copy = {**record, "condition": "A" if record["agent_strategy"] == "single" else "B"}
        copy["agent_strategy"] = "single" if copy["condition"] == "A" else "staged"
        normalized.append(copy)
    return fairness_check(normalized)


def interval_metrics(rows):
    intervals, missing = [], 0
    for row in rows:
        try:
            begin, end = (datetime.fromisoformat(row[key].replace("Z", "+00:00"))
                          for key in ("started_at", "finished_at"))
            if begin.tzinfo is None or end < begin:
                raise ValueError()
            intervals.append((begin.timestamp(), end.timestamp()))
        except (KeyError, TypeError, ValueError, AttributeError):
            missing += 1
    union, edge = 0.0, None
    for begin, end in sorted(intervals):
        union += max(0.0, end - max(begin, edge if edge is not None else begin))
        edge = max(edge if edge is not None else end, end)
    return {"union_seconds": union, "summed_seconds": sum(end - begin for begin, end in intervals),
            "intervals": len(intervals), "missing_intervals": missing, "complete": missing == 0}


def trial_metrics(run, operations, tools, submitted_at):
    model_ops = [o for o in operations if o.get("operation_kind") == "model_calls"
                 or o.get("node_id", "").rsplit(":", 1)[-1] == "model_calls"]
    summaries = [o for o in model_ops if o.get("purpose") == "context_summary"]
    recoveries = [o for o in summaries if o.get("recovery_of")]
    stage_names = {o.get("node_id", "").split(":")[1] for o in model_ops if ":" in o.get("node_id", "")}
    started, finished = run.get("started_at"), run.get("finished_at")
    wall = interval_metrics([{"started_at": submitted_at, "finished_at": finished}])
    queue = interval_metrics([{"started_at": submitted_at, "finished_at": started}])
    execution = interval_metrics([{"started_at": started, "finished_at": finished}])
    return {"wall_seconds": wall["union_seconds"] if wall["complete"] else None,
            "queue_seconds": queue["union_seconds"] if queue["complete"] else None,
            "execution_seconds": execution["union_seconds"] if execution["complete"] else None,
            "model_boundaries": interval_metrics(model_ops), "tool_boundaries": interval_metrics(tools),
            "stages": {stage: interval_metrics([o for o in model_ops if o.get("node_id", "").startswith("agent:" + stage + ":")])
                       for stage in sorted(stage_names)},
            "summary_attempts": len(summaries), "summary_recovery_attempts": len(recoveries),
            "summary_failed_attempts": (sum(o["output_status"] != "complete" for o in summaries)
                                        if all(o.get("output_status") is not None for o in summaries) else None),
            "recovery_used": bool(recoveries)}


def summarize_optimization(records):
    formal = [r for r in records if r["split"] == "formal"]
    result = {}
    for condition in CONDITIONS:
        selected = [r for r in formal if r["condition"] == condition]
        resolved = [r for r in selected if r["resolved"]]
        timed_resolved = [r for r in resolved if r.get("elapsed_time_valid", True)]
        failed = [r for r in selected if not r["resolved"]]
        costs = [(r.get("cost_estimate") or {}).get("reference_cost") for r in selected]
        total_cost = sum((Decimal(value) for value in costs if value is not None), Decimal(0))
        complete_cost = bool(selected) and all(value is not None for value in costs)
        by_task = {}
        for record in selected:
            by_task.setdefault(record["task_id"], []).append(record["resolved"])
        result[condition] = {"planned": 18, "attempted": len(selected), "not_run": 18 - len(selected),
            "resolved": len(resolved), "workflow_completed": sum(r["run_status"] == "completed" for r in selected),
            "code_passed": sum(r["acceptance"]["status"] == "PASS" for r in selected),
            "observed_resolution_rate": len(resolved) / len(selected) if selected else None,
            "task_equal_resolution_rate": sum(sum(v) / len(v) for v in by_task.values()) / len(by_task) if by_task else None,
            "represented_tasks": len(by_task),
            "success_median_wall_seconds": median(r["latency_seconds"] for r in timed_resolved) if timed_resolved else None,
            "failed_wall_seconds": (sum(r["latency_seconds"] for r in failed)
                                    if all(r.get("elapsed_time_valid", True) for r in failed) else None),
            "failed_wall_seconds_complete": all(r.get("elapsed_time_valid", True) for r in failed),
            "all_attempt_model_calls": sum(r["model_calls"] for r in selected),
            "all_attempt_reference_cost": str(total_cost) if complete_cost else None,
            "observed_reference_cost": str(total_cost) if any(c is not None for c in costs) else None,
            "cost_per_resolved_including_failed_attempts": str(total_cost / len(resolved)) if complete_cost and resolved else None,
            "first_pass_resolved": sum(not r.get("timing", {}).get("recovery_used", False) for r in resolved),
            "recovered_resolved": sum(r.get("timing", {}).get("recovery_used", False) for r in resolved),
            "billed_cost": None}
    pairs = []
    for task, repeat in sorted({(r["task_id"], r["repetition"]) for r in formal}):
        block = {r["condition"]: r for r in formal if r["task_id"] == task and r["repetition"] == repeat}
        for baseline, treatment in (("Single-Pro", "Compact-Mixed"), ("Single-Flash", "Compact-Mixed"),
                                    ("Full-Pro", "Compact-Pro"), ("Compact-Pro", "Compact-Mixed")):
            left, right = block.get(baseline), block.get(treatment)
            pair = {"task_id": task, "repetition": repeat, "baseline": baseline, "treatment": treatment,
                    "complete_pair": left is not None and right is not None}
            if left and right:
                pair.update(baseline_resolved=left["resolved"], treatment_resolved=right["resolved"])
                both = left["resolved"] and right["resolved"]
                pair["equal_success_wall_ratio"] = (right["latency_seconds"] / left["latency_seconds"]
                    if both and left.get("elapsed_time_valid", True) and right.get("elapsed_time_valid", True)
                    and left["latency_seconds"] > 0 else None)
                a, b = ((r.get("cost_estimate") or {}).get("reference_cost") for r in (left, right))
                pair["equal_success_cost_ratio"] = str(Decimal(b) / Decimal(a)) if both and a and b and Decimal(a) > 0 else None
            pairs.append(pair)
    return {"formal_only": True, "conditions": result, "paired_task_repetitions": pairs,
            "claim": None, "inference_scope": "Exploratory six-task sample; repeats are not independent tasks"}


def development_review_document(value, report, through):
    if value is None:
        return None
    if (through != "development" or report.get("formal_freeze")
            or any(r.get("split") == "formal" for r in report["records"])):
        raise EvaluationError("Development review is forbidden after the formal freeze")
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("authorization_id") != AUTHORIZATION or value.get("session") != report["session"]
            or not all(isinstance(value.get(k), str) and value[k].strip() for k in ("operator", "reason", "reviewed_at"))
            or type(value.get("adopt_current_models", False)) is not bool):
        raise EvaluationError("Development review requires explicit campaign, session, operator and reason")
    try:
        if datetime.fromisoformat(value["reviewed_at"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError()
    except (TypeError, ValueError):
        raise EvaluationError("Development review time must include its timezone") from None
    reviews = value.get("runs", [])
    if not isinstance(reviews, list) or len(reviews) > 1:
        raise EvaluationError("Review one explicitly identified development Run at a time")
    for row in reviews:
        if (not isinstance(row, dict) or not isinstance(row.get("run_id"), str) or not row["run_id"]
                or row.get("terminal_status") not in {"cancelled", "failed"}
                or not isinstance(row.get("operation_ids"), list) or not row["operation_ids"]
                or any(not isinstance(i, str) or not i for i in row["operation_ids"])
                or len(set(row["operation_ids"])) != len(row["operation_ids"])):
            raise EvaluationError("Review must identify exact unknown model operations and a failed terminal Run")
    revision = value.get("implementation_revision")
    if revision is not None and (not isinstance(revision, dict) or set(revision) != {"before", "after"}
            or not all(isinstance(v, str) and v.strip() for v in revision.values())
            or revision["before"] == revision["after"]):
        raise EvaluationError("Implementation review must name distinct before and after builds")
    if not reviews and not value.get("adopt_current_models") and revision is None:
        raise EvaluationError("Development review has no explicit action")
    return {**value, "fingerprint": digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())}


def reviewed_completed_tools(review, run, evidence, state, task, client):
    """Match completed local effects to receipts and explicit resource-reclamation evidence."""
    tools = evidence["tools"]
    readonly = {"workspace_read", "workspace_list", "workspace_search", "artifact_read"}
    if any(t.get("status") != "completed" or t.get("tool_name") not in
           readonly | {"workspace_patch", "run_command"} or t.get("slot_held")
           or t.get("slot_claimed") for t in tools):
        raise EvaluationError("Development review requires completed known tools without held slots")
    controlled = [t for t in tools if t["tool_name"] not in readonly]
    expected = dict(state["initial_sha256"])
    if controlled:
        audit = review.get("completed_tool_evidence", {})
        policy = (run.get("workspace") or {}).get("tool_policy", {})
        if (audit.get("run_id") != run["id"] or policy.get("network") != "deny"
                or policy.get("snapshot_writeback") is not False
                or audit.get("frozen_tool_policy") != policy):
            raise EvaluationError("Completed effects require the exact isolated tool-policy audit")
        patches, commands = audit.get("patches", []), audit.get("commands", [])
        for name, rows in (("workspace_patch", patches), ("run_command", commands)):
            ids = [r.get("operation_id") for r in rows]
            if len(ids) != len(set(ids)) or set(ids) != {t["id"] for t in controlled if t["tool_name"] == name}:
                raise EvaluationError("Completed-effect audit must match every operation exactly")
        by_id = {a["id"]: a for a in evidence["artifacts"]}
        for tool in sorted(controlled, key=lambda t: (t.get("finished_at", ""), t["id"])):
            result, parameters = tool.get("result", {}), tool.get("parameters", {})
            if (tool.get("run_id") != run["id"] or tool.get("tool_version") != "builtin-workspace-v1"
                    or not tool.get("finished_at") or not tool.get("reclaimed_at")):
                raise EvaluationError("Completed-effect receipt or reclamation is incomplete")
            if tool["tool_name"] == "workspace_patch":
                name = parameters.get("path")
                row = next(p for p in patches if p["operation_id"] == tool["id"])
                content = parameters.get("content")
                after = digest(content.encode()) if isinstance(content, str) else None
                if (name not in expected or parameters.get("expected_sha256") != expected[name]
                        or result.get("before_sha256") != expected[name] or result.get("sha256") != after
                        or result.get("path") != name or result.get("state") != "completed"
                        or result.get("run_id") != run["id"] or result.get("recovery_id") != tool["id"]
                        or result.get("workspace_id") != state["workspace"]["id"]
                        or row.get("path") != name or row.get("receipt_before_sha256") != expected[name]
                        or row.get("receipt_sha256") != after):
                    raise EvaluationError("Patch receipt does not match the original authorized file version")
                expected[name] = after
                kind = "tool_result"
            else:
                row = next(c for c in commands if c["operation_id"] == tool["id"])
                ids = ("sandbox_id", "command_id", "snapshot_id")
                if (any(not tool.get(k) or tool[k] != result.get(k) or tool[k] != row.get(k) for k in ids)
                        or type(result.get("exit_code")) is not int or row.get("exit_code") != result["exit_code"]
                        or row.get("reclaimed_at") != tool["reclaimed_at"] or row.get("slot_claimed") is not False
                        or row.get("controller_status") != 404 or audit.get("docker_listing_exit_code") != 0
                        or audit.get("docker_sandbox_matches", {}).get(tool["sandbox_id"]) is not False):
                    raise EvaluationError("Command receipt lacks matching reclaimed resource evidence")
                kind = "command_log"
            refs = result.get("artifacts", [])
            if not refs or not any(a.get("kind") == kind for a in refs):
                raise EvaluationError("Completed effects require their original artifact evidence")
            for ref in refs:
                artifact = by_id.get(ref.get("id"))
                if (not artifact or any(artifact.get(k) != ref.get(k) for k in
                        ("id", "run_id", "operation_id", "sha256", "size_bytes", "kind"))
                        or artifact.get("run_id") != run["id"] or artifact.get("operation_id") != tool["id"]):
                    raise EvaluationError("Completed-effect artifacts do not match their operation")
                response = client.get(f"/api/v1/artifacts/{artifact['id']}/content")
                if (response.status_code != 200 or digest(response.content) != artifact["sha256"]
                        or len(response.content) != artifact["size_bytes"]
                        or response.headers.get("X-Artifact-SHA256") != artifact["sha256"]):
                    raise EvaluationError("Completed-effect artifact integrity check failed")
    workspace = Path(state["workspace_path"])
    if {name: digest(candidate_bytes(workspace / name)) for name in task["write_allow"]} != expected:
        raise EvaluationError("Workspace does not match the original baseline and completed patch receipts")


def reviewed_model_failure(review, run, entry, task, evidence, state, ledger, client):
    """Audit-only retirement of a terminal development model failure; never replay."""
    if (entry["phase"] != "development" or task["split"] != "development"
            or run["id"] != review["run_id"] or state.get("run_id") != run["id"]
            or run["status"] != review["terminal_status"] or not run.get("finished_at")
            or run.get("conversation_id") != state.get("conversation_id")
            or not state.get("submission")):
        raise EvaluationError("Review does not match the original terminal development Run")
    ops = evidence["operations"]
    unknown = [o for o in ops if o.get("status") != "completed"]
    if (not unknown or {o.get("operation_id") for o in unknown} != set(review["operation_ids"])
            or any(o.get("status") != "uncertain" or not (
                o.get("operation_kind") == "model_calls"
                or o.get("node_id", "").rsplit(":", 1)[-1] == "model_calls") for o in unknown)):
        raise EvaluationError("Review may retire only the exact uncertain model operations, never unknown tools")
    reservation = next((r for r in ledger.snapshot()["reservations"] if r["id"] == state["reservation"]), None)
    if not reservation or reservation["state"] != "unknown" or reservation["maximum"] != 50:
        raise EvaluationError("Reviewed failure must retain its original full unknown reservation")
    reviewed_completed_tools(review, run, evidence, state, task, client)


class OptimizationRunner(EvaluationRunner):
    async def trial_v2(self, entry, task, session, models, root, ledger, quote, review=None):
        label = f"{entry['sequence']:03d}-{task['id']}-{entry['condition']}-r{entry['repetition']}"
        directory = root / label
        directory.mkdir(exist_ok=True)
        state_path = directory / "trial-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        reservation = session + "-" + str(entry["sequence"])
        if (directory / "result.json").exists():
            return json.loads((directory / "result.json").read_text(encoding="utf-8"))
        if review and (not all(state.get(k) for k in ("run_id", "workspace", "workspace_path", "conversation_id", "submission"))
                       or state.get("reservation") != reservation):
            raise EvaluationError("Review requires an existing dispatched trial; it cannot create a Run")
        if not state:
            workspace = Path(self.plan["workspace_root"]) / session / label
            shutil.copytree(OPTIMIZATION_ROOT / task["id"] / "workspace", workspace)
            state = {"workspace_path": str(workspace), "stage": "prepared", "reservation": reservation,
                     "implementation_id": self.plan["implementation_id"],
                     "initial_sha256": {name: digest(candidate_bytes(workspace / name)) for name in task["write_allow"]}}
            save(state_path, state)
        if "workspace" not in state:
            grant = self.api("POST", "/workspaces", json={"name": label, "host_path": state["workspace_path"],
                 "read_allow": ["."], "write_allow": task["write_allow"], "command_approval": False})
            if not grant.get("ready"):
                raise EvaluationError("Evaluation workspace mount was not verified")
            state["workspace"] = grant
            save(state_path, state)
        if "conversation_id" not in state:
            state["conversation_id"] = self.api("POST", "/conversations", json={"title": "M4.1 " + label})["id"]
            save(state_path, state)
        body = (state["submission"] if review else optimization_submission(
            self.plan, models, entry["condition"], task, state["workspace"]["id"], reservation))
        if "submission" in state and state["submission"] != body:
            raise EvaluationError("A paid trial cannot resume with different request parameters")
        state["submission"] = body
        save(state_path, state)
        if "run_id" not in state:
            ledger.reserve(reservation, 50, "evaluation_trial", metadata={"session": session, "sequence": entry["sequence"]})
            if not ledger.start(reservation):
                # No replay after a crash between dispatch and saving its ID.
                # Every trial owns one isolated conversation, so read-only lookup
                # can recover the server's existing Run without another POST.
                existing = [r for r in self.api("GET", "/runs") if r["conversation_id"] == state["conversation_id"]]
                if len(existing) != 1:
                    ledger.unknown(reservation)
                    raise EvaluationError("Dispatch outcome is unknown; do not resubmit the paid trial")
                messages = self.api("GET", f"/conversations/{state['conversation_id']}/messages")
                if not any(m.get("role") == "user" and m.get("content") == body["content"] for m in messages):
                    ledger.unknown(reservation)
                    raise EvaluationError("Recovered Run does not match the persisted task")
                state["run_id"] = existing[0]["id"]
            else:
                state.update(stage="dispatch_intent", submitted_at=datetime.now(timezone.utc).isoformat())
                save(state_path, state)
                try:
                    state["run_id"] = self.api("POST", f"/conversations/{state['conversation_id']}/messages", json=body)["run_id"]
                except BaseException:
                    ledger.unknown(reservation)
                    raise
            state["stage"] = "submitted"
            save(state_path, state)
        try:
            if review:
                run, blocked = self.api("GET", f"/runs/{state['run_id']}"), None
                if run["status"] != review["terminal_status"] or run["status"] not in {"cancelled", "failed"}:
                    raise EvaluationError("Review requires the exact terminal Run; no control command was sent")
            else:
                run, blocked = await self.wait(state["run_id"])
        except BaseException:
            if review:
                raise
            # Keep the full reservation until terminal budget evidence exists.
            try:
                self.api("POST", f"/runs/{state['run_id']}/cancel")
            finally:
                ledger.unknown(reservation)
            raise
        evidence = self.records(run, state["conversation_id"], directory)
        tools, ops, budget = evidence["tools"], evidence["operations"], evidence["budget"]
        uncertain = (run["status"] == "pending_review" or any(t.get("status") == "uncertain" or t.get("slot_held") for t in tools)
                     or any(o.get("status") in {"uncertain", "pending_review", "started", "running"} for o in ops))
        if review:
            reviewed_model_failure(review, run, entry, task, evidence, state, ledger, self.client)
        elif uncertain:
            ledger.unknown(reservation)
            raise EvaluationError("Uncertain operations require review; the evaluation batch stopped")
        calls = budget.get("model_calls")
        if type(calls) is not int:
            ledger.unknown(reservation)
            raise EvaluationError("Terminal Run usage is unavailable; allowance is retained")
        if not review:
            ledger.finish(reservation, calls)
        workspace = Path(state["workspace_path"])
        final = {name: candidate_bytes(workspace / name) for name in task["write_allow"]}
        for name, data in final.items():
            path = directory / "candidate" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if blocked:
            raise EvaluationError("Approval, pause or evaluation timeout interrupted an unattended trial")
        acceptance = await self.acceptance.evaluate(task, final, directory)
        save(directory / "acceptance.json", acceptance)
        started = datetime.fromisoformat(state["submitted_at"])
        finish = datetime.fromisoformat(run["finished_at"]) if run.get("finished_at") else datetime.now(timezone.utc)
        record = {**entry, "repetition": entry["repetition"], "split": task["split"], "complexity": task["complexity"],
            "implementation_id": state.get("implementation_id", self.plan["implementation_id"]),
            "run_id": run["id"], "run_status": run["status"], "run_error_code": run.get("error_code"),
            "agent_strategy": run.get("agent_strategy"), "execution_config": run.get("execution_config"),
            "tool_policy": (run.get("workspace") or {}).get("tool_policy"), "acceptance": acceptance,
            "resolved": run["status"] == "completed" and acceptance["status"] == "PASS",
            "latency_seconds": max(0, (finish - started).total_seconds()), "active_seconds": budget.get("active_seconds"),
            "model_calls": calls, "tool_calls": budget.get("tool_calls"), "usage": run.get("usage"),
            "summary_count": sum(a["kind"] == "context_summary" for a in evidence["artifacts"]),
            "cost_estimate": calculate_reference_cost(ops, expected_calls=calls, snapshot=quote, at=started),
            "billed_cost": None, "initial_sha256": state["initial_sha256"],
            "final_sha256": {key: digest(value) for key, value in final.items()}, "evidence_directory": label}
        if review:
            record.update(manual_development_review=review, budget_reservation="unknown_full_50_retained",
                          resolved=False, automatic_recovery=False, replayed=False)
        record["timing"] = trial_metrics(run, ops, tools, state["submitted_at"])
        record["first_pass_resolved"] = record["resolved"] and not record["timing"]["recovery_used"]
        record["recovered_resolved"] = record["resolved"] and record["timing"]["recovery_used"]
        record["end_to_end_seconds"] = max(0, (datetime.now(timezone.utc) - started).total_seconds())
        save(directory / "result.json", record)
        state["stage"] = "recorded"
        save(state_path, state)
        return record


async def run_optimization(plan, **kwargs):
    validate_optimization_plan(plan)
    with BudgetLedger(plan["budget_ledger"]).runner_lock():
        return await _run_optimization(plan, **kwargs)


async def _run_optimization(plan, *, through="all", seed=43, resume=None, freeze_formal=False,
                            development_review=None, review_only=False, client=None, acceptance=None):
    validate_optimization_plan(plan)
    manifest, tasks, frozen = load_optimization_tasks()
    queue = optimization_schedule(seed)
    ceiling = {"development": 15, "first": 45, "all": 105}[through]
    ledger = BudgetLedger(plan["budget_ledger"])
    own_client = client is None
    if own_client:
        cookie = Path(plan["session_cookie_file"]).read_text(encoding="utf-8").strip()
        client = httpx.Client(base_url=plan["api_url"], cookies={"omniharness_session": cookie},
            headers={"Origin": plan["api_url"], "X-Omni-Request": "1"}, timeout=30, trust_env=False, follow_redirects=False)
    root = Path(resume).resolve() if resume else Path(plan["output_root"]) / ("m4-opt-" + uuid4().hex[:16])
    if resume and (not root.is_relative_to(Path(plan["output_root"]).resolve()) or not (root / "report.json").is_file()):
        if own_client:
            client.close()
        raise EvaluationError("Resume requires an existing report beneath the approved output root")
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {
        "schema_version": 2, "session": root.name, "dataset_id": manifest["dataset_id"], "manifest_sha256": frozen,
        "seed": seed, "queue": queue, "plan": plan, "records": [], "status": "preflight",
        "quote_snapshot": load_reference_prices(), "quality_advantage_claim": None, "billed_cost": None}
    if report["queue"] != queue or report["manifest_sha256"] != frozen:
        raise EvaluationError("Resume must preserve the frozen plan, seed and task manifest")
    review_document = development_review_document(development_review, report, through)
    if (review_document or review_only) and not resume:
        raise EvaluationError("Development review requires an existing report")
    if review_only and not review_document:
        raise EvaluationError("Review-only requires an explicit development review file")
    reviews = {r["run_id"]: {**r, "operator": review_document["operator"], "reason": review_document["reason"],
               "reviewed_at": review_document["reviewed_at"], "fingerprint": review_document["fingerprint"]}
               for r in (review_document or {}).get("runs", [])}
    known_run_ids = {json.loads(p.read_text(encoding="utf-8")).get("run_id") for p in root.glob("*/trial-state.json")}
    if set(reviews) - known_run_ids:
        raise EvaluationError("The reviewed Run is not an existing trial in this campaign")
    for record in report["records"]:
        if record["run_id"] in reviews and record.get("manual_development_review") != reviews[record["run_id"]]:
            raise EvaluationError("A recorded review cannot be replaced with different audit evidence")
    development_done = len([r for r in report["records"] if r["split"] == "development"]) == 15
    validate_formal_gate(report, queue, through, freeze_formal)
    transition_pending = through != "development" and not report.get("formal_freeze")
    preflight_committed = not transition_pending
    if transition_pending:
        # Preserve the exact last development state before attempting any new
        # formal provider/configuration preflight. No file is written until it passes.
        report["development_plan"] = deepcopy(report["plan"])
        report["development_models"] = deepcopy(report.get("models"))
        report["development_quote_snapshot"] = deepcopy(report.get("quote_snapshot"))
    if report["plan"] != plan:
        revision = (review_document or {}).get("implementation_revision")
        if revision:
            changed = {k for k in set(report["plan"]) | set(plan) if report["plan"].get(k) != plan.get(k)}
            if (changed != {"implementation_id"} or revision != {
                    "before": report["plan"]["implementation_id"], "after": plan["implementation_id"]}
                    or known_run_ids - {r["run_id"] for r in report["records"]}):
                raise EvaluationError("Development build revision requires exact builds and all dispatched slots recorded")
            report.setdefault("development_implementation_revisions", []).append({
                **revision, "operator": review_document["operator"], "reason": review_document["reason"],
                "reviewed_at": review_document["reviewed_at"], "review_fingerprint": review_document["fingerprint"],
                "after_recorded_sequence": max((r["sequence"] for r in report["records"]), default=0)})
            report["plan"] = plan
        elif not freeze_formal or not development_done or report.get("formal_freeze"):
            raise EvaluationError("Only the explicit development-to-formal gate can change implementation/configuration")
        else:
            allowed = {"implementation_id", "strong_model_id", "economy_model_id", "sandbox"}
            if any(report["plan"].get(k) != plan.get(k) for k in (set(report["plan"]) | set(plan)) - allowed):
                raise EvaluationError("The formal gate cannot change API, campaign, storage or task authorization")
            old_image = report["plan"]["sandbox"]["image"]
            report["plan"] = plan
            report.pop("models", None)
            if old_image != plan["sandbox"]["image"]:
                report.pop("calibration", None)
            report["quote_snapshot"] = load_reference_prices()
    if preflight_committed:
        save(report_path, report)
    try:
        runner = OptimizationRunner(plan, client, acceptance or OptimizationAcceptance(plan["sandbox"]))
        own_conversations = {json.loads(p.read_text(encoding="utf-8")).get("conversation_id")
                             for p in root.glob("*/trial-state.json")}
        if any(r["status"] not in TERMINAL and r["conversation_id"] not in own_conversations
               for r in runner.api("GET", "/runs")):
            raise EvaluationError("Other unfinished Runs must settle before this comparison")
        models, signature = optimization_models(plan, runner.api("GET", "/model-configs"))
        frozen_formal = report.get("formal_freeze")
        if frozen_formal and (frozen_formal.get("models") != models
                or frozen_formal.get("implementation_id") != plan["implementation_id"]
                or frozen_formal.get("sandbox") != plan["sandbox"]
                or frozen_formal.get("quote_snapshot") != report["quote_snapshot"]
                or frozen_formal.get("strong_model_id") != plan["strong_model_id"]
                or frozen_formal.get("economy_model_id") != plan["economy_model_id"]
                or frozen_formal.get("manifest_sha256") != frozen):
            raise EvaluationError("Formal frozen model, implementation, sandbox, quote or task metadata changed")
        if "models" in report and report["models"] != models:
            if review_document and review_document.get("adopt_current_models"):
                report.setdefault("development_model_revisions", []).append({
                    "before": report["models"], "after": models, "operator": review_document["operator"],
                    "reason": review_document["reason"], "reviewed_at": review_document["reviewed_at"],
                    "review_fingerprint": review_document["fingerprint"],
                    "after_recorded_sequence": max((r["sequence"] for r in report["records"]), default=0)})
            elif not freeze_formal or not development_done or report.get("formal_freeze"):
                raise EvaluationError("Resume requires the same frozen model configurations")
            else:
                report["development_models"] = report["models"]
        report.update(models=models, context_fairness=signature, status="running")
        first_runtime_verified = False
        if transition_pending:
            verify_formal_runtime(plan, {"implementation_id": plan["implementation_id"], "sandbox": plan["sandbox"]})
            current_models, current_signature = optimization_models(plan, runner.api("GET", "/model-configs"))
            if current_models != models or current_signature != signature:
                raise EvaluationError("Formal frozen model configuration or capabilities changed before dispatch")
            report["formal_freeze"] = {"implementation_id": plan["implementation_id"], "models": models,
                "strong_model_id": plan["strong_model_id"], "economy_model_id": plan["economy_model_id"],
                "context_fairness": signature, "manifest_sha256": frozen, "quote_snapshot": report["quote_snapshot"],
                "sandbox": plan["sandbox"], "seed": seed, "at": datetime.now(timezone.utc).isoformat()}
            first_runtime_verified = True
            preflight_committed = True
            save(report_path, report)
        if review_document:
            audits = report.setdefault("development_reviews", [])
            if not any(r["fingerprint"] == review_document["fingerprint"] for r in audits):
                audits.append(review_document)
            save(report_path, report)
        if report.get("calibration", {}).get("status") != "PASS":
            report["calibration"] = await calibrate_optimization(runner.acceptance, root / "calibration")
            save(report_path, report)
        for entry in queue[:ceiling]:
            if any(r["sequence"] == entry["sequence"] for r in report["records"]):
                continue
            label = f"{entry['sequence']:03d}-{entry['task_id']}-{entry['condition']}-r{entry['repetition']}"
            state_path = root / label / "trial-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            review = reviews.get(state.get("run_id"))
            if review_only and not review:
                continue
            if entry["phase"] != "development":
                if not first_runtime_verified:
                    verify_formal_runtime(plan, report.get("formal_freeze") or {
                        "implementation_id": plan["implementation_id"], "sandbox": plan["sandbox"]})
                first_runtime_verified = False
                current_models, current_signature = optimization_models(plan, runner.api("GET", "/model-configs"))
                if current_models != models or current_signature != signature:
                    raise EvaluationError("Formal frozen model configuration or capabilities changed before dispatch")
            if entry["phase"] != "development" and not report.get("formal_freeze"):
                if len([r for r in report["records"] if r["split"] == "development"]) != 15:
                    raise EvaluationError("Formal work requires the complete separate development block")
                report["formal_freeze"] = {"implementation_id": plan["implementation_id"], "models": models,
                    "strong_model_id": plan["strong_model_id"], "economy_model_id": plan["economy_model_id"],
                    "context_fairness": signature, "manifest_sha256": frozen, "quote_snapshot": report["quote_snapshot"],
                    "sandbox": plan["sandbox"], "seed": seed, "at": datetime.now(timezone.utc).isoformat()}
                save(report_path, report)
            record = await runner.trial_v2(entry, tasks[entry["task_id"]], root.name, models, root, ledger,
                                          report["quote_snapshot"], review=review)
            report["records"].append(record)
            report["fairness"] = optimization_fairness(
                [r for r in report["records"] if r["split"] == record["split"]],
                frozen_models=(report.get("formal_freeze") or {}).get("models"),
                frozen_selection=report.get("formal_freeze"))
            report["summary"] = summarize_optimization(report["records"])
            report["budget"] = ledger.snapshot()
            save(report_path, report)
            if entry["phase"] != "development":
                verify_formal_runtime(plan, report["formal_freeze"])
            if report["fairness"]["status"] != "PASS":
                raise EvaluationError("Frozen comparison metadata differs; no following trial was submitted")
        report["status"] = ("development_review_complete" if review_only else
                            "completed" if len(report["records"]) == 105 else "phase_complete")
        report.pop("failure", None)
    except EvaluationBudgetExhausted as exc:
        report["status"] = "NOT_RUN_BUDGET"
        report["remaining"] = [row for row in queue if row["sequence"] not in {r["sequence"] for r in report["records"]}]
        report["failure"] = str(exc)
    except BaseException as exc:
        report["status"] = "blocked"
        report["failure"] = str(exc) if isinstance(exc, EvaluationError) else type(exc).__name__
        raise
    finally:
        if preflight_committed:
            report["budget"] = ledger.snapshot()
            save(report_path, report)
        if own_client:
            client.close()
    return report_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Explicitly submit the live evaluation Runs")
    parser.add_argument("--repetitions", type=int, default=1, help="Legacy schema 1 only; schema 2 fixes three formal repetitions")
    parser.add_argument("--seed", type=int, default=43)
    parser.add_argument("--through", choices=("development", "first", "all"), default="development")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--freeze-formal", action="store_true", help="One explicit freeze after all 15 development trials")
    parser.add_argument("--development-review", type=Path, help="Explicit terminal model-failure/model-revision audit before formal freeze")
    parser.add_argument("--review-only", action="store_true", help="Record reviewed development failures and run acceptance, without any model request")
    parser.add_argument("--calibrate-only", action="store_true", help="Real isolated acceptance only; zero model/API Runs")
    args = parser.parse_args()
    raw = json.loads(args.plan.read_text(encoding="utf-8"))
    if raw.get("schema_version") == 2:
        if args.repetitions != 1:
            raise EvaluationError("Schema 2 fixes its approved 105-Run schedule; do not override repetitions")
        plan = validate_optimization_plan(raw)
        if args.review_only and (not args.development_review or not args.resume or args.execute):
            raise EvaluationError("--review-only requires --development-review and --resume, without --execute")
        development_review = (json.loads(args.development_review.read_text(encoding="utf-8"))
                              if args.development_review else None)
        if args.calibrate_only:
            target = Path(plan["output_root"]) / ("calibration-" + uuid4().hex[:12])
            asyncio.run(calibrate_optimization(OptimizationAcceptance(plan["sandbox"]), target))
            print(str(target / "calibration.json"))
            return 0
        if not args.execute and not args.review_only:
            print(json.dumps({"mode": "describe_only", "run_count": 105, "development": 15, "formal": 90,
                              "through": args.through, "ceiling": 1500, "model_calls_sent": 0,
                              "schedule": optimization_schedule(args.seed)}, ensure_ascii=False, indent=2))
            return 0
        result = asyncio.run(run_optimization(plan, through=args.through, seed=args.seed, resume=args.resume,
                                             freeze_formal=args.freeze_formal, development_review=development_review,
                                             review_only=args.review_only))
        print(result)
        return 0
    if args.calibrate_only or args.resume or args.development_review or args.review_only:
        raise EvaluationError("These options require the new frozen evaluation schema")
    plan = validate_plan(raw)
    planned = schedule(args.repetitions, args.seed)
    if not args.execute:
        print(json.dumps({"mode": "describe_only", "api_url": plan["api_url"], "run_count": len(planned),
                          "baseline_model_id": plan["baseline_model_id"], "stage_models": plan["stage_models"],
                          "schedule": planned, "model_calls_sent": 0}, ensure_ascii=False, indent=2))
        return 0
    try:
        report = asyncio.run(run(plan, repetitions=args.repetitions, seed=args.seed))
    except Exception as exc:
        print("Evaluation stopped: " + type(exc).__name__ + ". Inspect the local report; no retry was sent.")
        return 1
    print(str(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
