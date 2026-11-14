"""Disposable no-network Docker sandbox used only by v10 integration tests."""
from __future__ import annotations

import asyncio
import base64
import json
import re

from omniharness.sandbox import CommandResult, SandboxBoundaryError

_IMAGE = "sha256:b37700184c262593a5535bbb8196b7d28cb07514ec67c9168aca230291aa76ad"


class DisposableDockerSandbox:
    """Run only the fixture's pytest argv against a read-only snapshot."""

    def __init__(self):
        self.calls = []

    async def execute(self, *, run_id, operation_id, snapshot_path, command, cwd, timeout_seconds,
                      log_sink, created_sink, started_sink, reclaimed_sink):
        if (command != ["pytest", "-q"] or cwd != "." or not re.fullmatch(r"[a-f0-9]{64}", operation_id)
                or not isinstance(snapshot_path, str) or not 1 <= timeout_seconds <= 120):
            raise SandboxBoundaryError("Test sandbox request is outside fixture scope")
        convert = await asyncio.create_subprocess_exec(
            "wsl.exe", "-d", "OmniHarnessCheckAlt", "-u", "root", "--exec", "wslpath", "-a", snapshot_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        raw_path, _ = await asyncio.wait_for(convert.communicate(), timeout=10)
        if convert.returncode != 0:
            raise SandboxBoundaryError("Test snapshot path is unavailable")
        path = raw_path.decode("utf-8", errors="strict").strip()
        if not path.startswith("/mnt/") or "\x00" in path or "\n" in path or "," in path:
            raise SandboxBoundaryError("Test snapshot path is invalid")
        name = "v10-corr-test-" + operation_id[:24]
        self.calls.append({"run_id": run_id, "operation_id": operation_id, "command": list(command),
                           "snapshot_path": snapshot_path, "container": name})
        await created_sink(name)
        await started_sink(name, name)
        # WSL itself can prepend locale-encoded diagnostics to its stderr.
        # Capture the Docker child's bytes inside WSL so those diagnostics are
        # never confused with the project's actual test output.
        capture = ("import base64,json,subprocess,sys; p=subprocess.run(sys.argv[1:],capture_output=True); "
                   "print(json.dumps({'exit':p.returncode,'stdout':base64.b64encode(p.stdout).decode('ascii'),"
                   "'stderr':base64.b64encode(p.stderr).decode('ascii')}))")
        process = await asyncio.create_subprocess_exec(
            "wsl.exe", "-d", "OmniHarnessCheckAlt", "-u", "root", "--exec", "python3", "-c", capture,
            "docker", "run", "--rm",
            "--name", name, "--network", "none", "--read-only", "--memory", "512m", "--cpus", "1",
            "--pids-limit", "128", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--mount", f"type=bind,src={path},dst=/workspace,readonly", "--workdir", "/workspace",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTEST_ADDOPTS=-p no:cacheprovider",
            _IMAGE, *command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            wrapper_out, _wrapper_err = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except BaseException:
            cleanup = await asyncio.create_subprocess_exec(
                "wsl.exe", "-d", "OmniHarnessCheckAlt", "-u", "root", "--exec", "docker", "rm", "-f", name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await cleanup.wait()
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        try:
            result = json.loads(wrapper_out.decode("utf-8", errors="strict"))
            if process.returncode != 0 or type(result.get("exit")) is not int:
                raise ValueError
            stdout = base64.b64decode(result["stdout"], validate=True)
            stderr = base64.b64decode(result["stderr"], validate=True)
            out, err = stdout.decode("utf-8", errors="strict"), stderr.decode("utf-8", errors="strict")
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise SandboxBoundaryError("Test sandbox result is unavailable") from None
        if out:
            await log_sink("stdout", out)
        if err:
            await log_sink("stderr", err)
        await reclaimed_sink(name)
        return CommandResult(name, name, result["exit"], out[:4000], err[:4000], len(stdout) + len(stderr), False)
