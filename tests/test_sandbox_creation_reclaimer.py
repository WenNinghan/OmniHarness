import asyncio
from types import SimpleNamespace

import pytest
from opensandbox.exceptions import SandboxApiException

from omniharness.sandbox import operation_metadata
from omniharness.sandbox_creation_journal import CreationBinding, CreationRecord
from omniharness.sandbox_creation_reclaimer import ReclamationUnknown, reclaim_created


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ok", "metadata", "controller_error", "engine_present", "already_missing"])
async def test_reclaimer_requires_exact_ownership_and_independent_engine_proof(case):
    binding = CreationBinding("test", "run-1", "a" * 64, "b" * 64, "c" * 64)
    record = CreationRecord(binding, "CREATED", "sandbox-1")

    class Manager:
        killed = False

        async def get_sandbox_info(self, identity):
            assert identity == "sandbox-1"
            if case == "already_missing" or self.killed:
                raise SandboxApiException("not found", status_code=404)
            metadata = operation_metadata("test", "run-1", "a" * 64)
            if case == "metadata":
                metadata["omni_run"] = "other"
            return SimpleNamespace(id=identity, metadata=metadata)

        async def kill_sandbox(self, identity):
            if case == "controller_error":
                raise SandboxApiException("unavailable", status_code=503)
            self.killed = True

    checks = []

    async def verify(value):
        checks.append(value)
        return case != "engine_present"

    manager = Manager()
    if case in {"ok", "already_missing"}:
        proof = await reclaim_created(record, manager, verify, attempts=1, interval=0)
        assert proof["outcome"] == "unknown" and not proof["replayed"]
        assert manager.killed == (case == "ok") and checks == [record]
        assert proof["resource_reclaimed"] is True
    else:
        with pytest.raises(ReclamationUnknown):
            await reclaim_created(record, manager, verify, attempts=1, interval=0)
        if case in {"metadata", "controller_error"}:
            assert checks == []
        if case == "metadata":
            assert not manager.killed


@pytest.mark.asyncio
async def test_reclamation_waits_for_engine_cleanup_after_controller_404():
    binding = CreationBinding("test", "run-1", "a" * 64, "b" * 64, "c" * 64)
    record = CreationRecord(binding, "CREATED", "sandbox-1")
    checks = []

    class Manager:
        async def get_sandbox_info(self, identity):
            raise SandboxApiException("not found", status_code=404)

        async def kill_sandbox(self, identity):
            pytest.fail("A missing sandbox must not be killed/recreated")

    async def verify(value):
        checks.append(value)
        return len(checks) == 3

    proof = await reclaim_created(record, Manager(), verify, attempts=3, interval=0)
    assert proof["resource_reclaimed"] is True and proof["outcome"] == "unknown"
    assert checks == [record] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["never_absent", "verifier_error", "cancelled"])
async def test_engine_wait_is_bounded_and_does_not_hide_uncertainty_or_cancellation(case):
    record = CreationRecord(CreationBinding("test", "run-1", "a" * 64, "b" * 64, "c" * 64),
                            "CREATED", "sandbox-1")
    checks = []

    class Manager:
        async def get_sandbox_info(self, identity):
            raise SandboxApiException("not found", status_code=404)

    async def verify(value):
        checks.append(value)
        if case == "verifier_error":
            raise OSError("engine unavailable")
        if case == "cancelled":
            raise asyncio.CancelledError()
        return False

    with pytest.raises(asyncio.CancelledError if case == "cancelled" else ReclamationUnknown):
        await reclaim_created(record, Manager(), verify, attempts=3, interval=0)
    assert len(checks) == (3 if case == "never_absent" else 1)
