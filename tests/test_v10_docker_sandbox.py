"""Validate the disposable local container adapter against an actual snapshot."""
import asyncio
import os
import sys

import pytest
from v10_docker_sandbox import DisposableDockerSandbox


@pytest.mark.skipif(sys.platform != "win32", reason="This adapter uses the isolated Windows/WSL test runtime")
@pytest.mark.skipif(os.environ.get("OMNI_V10_DOCKER_LIVE") != "1",
                    reason="Set OMNI_V10_DOCKER_LIVE=1 only with the isolated WSL Docker engine running")
async def test_disposable_sandbox_runs_pytest_with_complete_logs(tmp_path):
    (tmp_path / "test_example.py").write_text("def test_example():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    events, logs = [], {}
    async def created(identity):
        events.append(("created", identity))
    async def started(identity, command):
        events.append(("started", identity, command))
    async def reclaimed(identity):
        events.append(("reclaimed", identity))
    async def output(channel, value):
        logs[channel] = logs.get(channel, "") + value
    sandbox = DisposableDockerSandbox()
    result = await sandbox.execute(run_id="fixture", operation_id="a" * 64, snapshot_path=str(tmp_path),
        command=["pytest", "-q"], cwd=".", timeout_seconds=60, log_sink=output,
        created_sink=created, started_sink=started, reclaimed_sink=reclaimed)
    assert result.exit_code == 0 and "1 passed" in logs["stdout"]
    assert result.log_bytes == sum(len(value.encode("utf-8")) for value in logs.values())
    assert [event[0] for event in events] == ["created", "started", "reclaimed"]
    inspect = await asyncio.create_subprocess_exec(
        "wsl.exe", "-d", "OmniHarnessCheckAlt", "-u", "root", "--exec", "docker", "ps", "-a",
        "--filter", "name=^/v10-corr-test-" + "a" * 24 + "$", "--format", "{{.Names}}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await asyncio.wait_for(inspect.communicate(), timeout=10)
    assert inspect.returncode == 0 and not stdout.strip()
