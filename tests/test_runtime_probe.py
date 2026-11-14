from pathlib import Path

import pytest

from scripts.runtime_probe import run_runtime_probe


@pytest.mark.asyncio
async def test_real_runtime_tools_parallelism_and_disk_resume(tmp_path: Path):
    report = await run_runtime_probe(tmp_path)
    assert report["passed"] is True
    assert report["live_provider_verified"] is False
    assert report["mode"] == "offline-deterministic-model"
    assert report["parallel_subagents"]["max_parallel"] == 2
    assert report["deep_agents_tool_loop"]["completed_tools"] == ["coder", "researcher"]
    assert report["sqlite_interrupt_resume"] == {
        "passed": True,
        "disk_database": True,
        "fresh_connection": True,
        "result": 42,
    }
