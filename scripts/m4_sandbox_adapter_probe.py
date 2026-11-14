"""Exercise the actual application adapter against the isolated hardened runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid

from m4_sandbox_setup import SNAPSHOTS, STATE, credential, docker, save, wsl

from omniharness.sandbox import SandboxConfig, SandboxExecutor


async def check(ready_only=False):
    deployed = json.loads((STATE / "deployment.json").read_text(encoding="utf-8"))
    snapshot = SNAPSHOTS + "/adapter-" + uuid.uuid4().hex
    await asyncio.to_thread(wsl, "install", "-d", "-m", "755", "-o", "10001", "-g", "10001", snapshot)
    config = SandboxConfig(
        endpoint=deployed["endpoint"],
        api_key=credential(),
        image=deployed["tool_image"],
        snapshot_root=SNAPSHOTS,
        instance_id="m4a-real-probe",
    )
    executor = SandboxExecutor(config)
    report = {"status": "BLOCKED", "timestamp": time.time(), "checks": {}}
    calls = []
    logs = []
    current = {}
    started = asyncio.Event()

    async def created_sink(sandbox_id):
        current["sandbox_id"] = sandbox_id
        calls.append("created")

    async def started_sink(sandbox_id, command_id):
        current["command_id"] = command_id
        calls.append("started")
        started.set()

    async def log_sink(channel, text):
        logs.append((channel, text))

    try:
        code = "import os,subprocess; print('python-uid='+str(os.getuid())); subprocess.run(['node','-e',\"console.log('node-uid='+process.getuid())\"],check=True); print('中文\\n\\nend',end='')"
        result = await executor.execute(
            run_id="m4a",
            # Actual ToolOperation IDs are 64-character SHA256 hex strings.
            operation_id="a" * 64,
            snapshot_path=snapshot,
            command=["python", "-c", code],
            log_sink=log_sink,
            started_sink=started_sink,
            created_sink=created_sink,
            timeout_seconds=20,
        )
        out = "".join(text for channel, text in logs if channel == "stdout")
        remaining = await asyncio.to_thread(
            docker, "ps", "-aq", "--filter", "name=^/sandbox-" + result.sandbox_id + "$"
        )
        report["checks"]["python_node"] = {
            "pass": result.exit_code == 0
            and out == "python-uid=10001\nnode-uid=10001\n中文\n\nend"
            and calls[:2] == ["created", "started"]
            and not remaining,
            "exit_code": result.exit_code,
            "stdout": out,
            "stderr": result.stderr_preview,
            "callbacks": calls[:2],
            "container_removed": not remaining,
        }
        if ready_only:
            report["status"] = "PASS" if report["checks"]["python_node"]["pass"] else "BLOCKED"
            save("ready-report.json", report)
            return report
        started.clear()
        current.clear()
        code = "import subprocess,time; from pathlib import Path; subprocess.Popen(['sleep','80']); Path('/workspace/cancel-started').write_text('started');time.sleep(80)"
        task = asyncio.create_task(
            executor.execute(
                run_id="m4a",
                operation_id="b" * 64,
                snapshot_path=snapshot,
                command=["python", "-c", code],
                log_sink=log_sink,
                started_sink=started_sink,
                created_sink=created_sink,
                timeout_seconds=90,
            )
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=60)
            for _ in range(20):
                marker = await asyncio.to_thread(wsl, "cat", snapshot + "/cancel-started", check=False)
                if marker == "started":
                    break
                await asyncio.sleep(0.1)
            if marker != "started":
                raise RuntimeError("Cancellation fixture did not start")
            info = json.loads(await asyncio.to_thread(docker, "inspect", "sandbox-" + current["sandbox_id"]))[
                0
            ]
            pid = info["State"]["Pid"]
            before = time.monotonic()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise RuntimeError("Cancelled operation did not propagate cancellation")
            remaining = await asyncio.to_thread(
                docker, "ps", "-aq", "--filter", "name=^/sandbox-" + current["sandbox_id"] + "$"
            )
            sidecar = await asyncio.to_thread(
                docker, "ps", "-aq", "--filter", "name=^/sandbox-egress-" + current["sandbox_id"] + "$"
            )
            pid_present = await asyncio.to_thread(
                wsl, "sh", "-c", "if [ -e /proc/" + str(pid) + "/stat ]; then echo present; fi"
            )
            report["checks"]["cancel_tree"] = {
                "pass": not remaining and not sidecar and not pid_present,
                "container_removed": not remaining,
                "sidecar_removed": not sidecar,
                "original_pid_removed": not pid_present,
                "seconds": round(time.monotonic() - before, 3),
            }
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        report["status"] = "PASS" if all(c["pass"] for c in report["checks"].values()) else "BLOCKED"
    except Exception as exc:
        report["error"] = {
            "type": type(exc).__name__,
            "message": str(exc).replace(credential(), "[REDACTED]")[:800],
        }
    save("ready-report.json" if ready_only else "adapter-report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-only", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(check(args.ready_only))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(report["status"] != "PASS")
