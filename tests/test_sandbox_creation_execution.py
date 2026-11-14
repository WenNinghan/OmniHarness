import pytest
from test_m4_sandbox_adapter import FakeSandbox, config

from omniharness.sandbox import SandboxExecutor, SandboxResultUnknown


class RemoteCreation:
    def __init__(self, fake):
        self.fake, self.creates, self.reclaims = fake, 0, 0

    async def create(self, request):
        self.creates += 1
        assert request.operation_fingerprint == "f" * 64
        return self.fake.id

    async def reclaim(self, request):
        self.reclaims += 1
        self.fake.killed = True
        return {"sandbox_id": self.fake.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "connect", "guard", "create"])
async def test_worker_uses_service_and_never_falls_back_or_repeats_command(failure):
    fake = FakeSandbox()
    remote = RemoteCreation(fake)
    calls = []

    async def connect(identity, **kwargs):
        calls.append("connect")
        if failure == "connect":
            raise ConnectionError("connect lost")
        return fake

    async def direct_create(*args, **kwargs):
        raise AssertionError("must never fall back to direct creation")

    async def failed_create(request):
        remote.creates += 1
        raise SandboxResultUnknown("create response lost")

    fake.connect, fake.create = connect, direct_create
    if failure == "create":
        remote.create = failed_create

    async def created(identity):
        calls.append("created")

    async def profile(*args):
        pass

    async def guard():
        calls.append("guard")
        if failure == "guard":
            raise SandboxResultUnknown("cancelled before command")

    async def log(*args):
        pass

    async def started(*args):
        calls.append("command")

    async def reclaimed(identity):
        calls.append("reclaimed")

    task = SandboxExecutor(config(), sdk=fake, creation_client=remote, profile_probe=profile).execute(
        run_id="run-1", operation_id="op-1", operation_fingerprint="f" * 64,
        snapshot_path="/snapshots/fixture", command=["python", "-V"], log_sink=log,
        started_sink=started, created_sink=created, reclaimed_sink=reclaimed, dispatch_guard=guard)
    if failure:
        with pytest.raises(SandboxResultUnknown):
            await task
        assert "command" not in calls
    else:
        result = await task
        assert result.exit_code == 0 and calls == ["created", "connect", "guard", "command", "reclaimed"]
    assert remote.creates == 1 and remote.reclaims == (0 if failure == "create" else 1)
