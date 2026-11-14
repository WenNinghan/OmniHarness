from types import SimpleNamespace

import pytest

from omniharness.sandbox import SandboxBoundaryError, SandboxConfig, operation_metadata
from omniharness.sandbox_creation_backend import SandboxCreationBackend
from omniharness.sandbox_creation_journal import CreationBinding, CreationJournal
from omniharness.sandbox_creation_supervisor import CreationSupervisor


@pytest.mark.asyncio
async def test_supervised_backend_uses_hardened_sdk_arguments_and_saves_id(tmp_path):
    config = SandboxConfig(endpoint="http://localhost:19080", api_key="secret-" * 8,
        image="sha256:" + "a" * 64, snapshot_root="/snapshots", instance_id="test")
    binding = CreationBinding("test", "run-1", "d" * 64, "b" * 64, "c" * 64)
    journal = CreationJournal.initialize(tmp_path / "journal.sqlite", "e" * 64)
    calls = []
    closed = []

    class SDK:
        @staticmethod
        async def create(image, **kwargs):
            calls.append((image, kwargs))

            async def close():
                closed.append(True)

            return SimpleNamespace(id="sandbox-1", close=close)

    backend = SandboxCreationBackend(config, sdk=SDK)
    result = await CreationSupervisor(journal).create(binding, lambda: backend.create(
        binding, snapshot_path="/snapshots/run-1/fixture", execd_token="x" * 32, timeout_seconds=120))
    assert result.sandbox_id == "sandbox-1" and closed == [True]
    image, args = calls[0]
    assert image == config.image
    assert args["metadata"] == operation_metadata("test", "run-1", "d" * 64)
    assert args["network_policy"].default_action == "deny"
    assert args["volumes"][0].host.path == "/snapshots/run-1/fixture"
    assert args["env"]["EXECD_ACCESS_TOKEN"] == "x" * 32
    assert config.api_key not in args["env"].values()


@pytest.mark.asyncio
async def test_backend_rejects_outside_snapshot_before_dispatch():
    config = SandboxConfig(endpoint="http://localhost:19080", api_key="secret-" * 8,
        image="sha256:" + "a" * 64, snapshot_root="/snapshots", instance_id="test")
    binding = CreationBinding("test", "run-1", "d" * 64, "b" * 64, "c" * 64)
    backend = SandboxCreationBackend(config, sdk=object())
    with pytest.raises(SandboxBoundaryError):
        await backend.create(binding, snapshot_path="/outside", execd_token="x" * 32, timeout_seconds=120)
