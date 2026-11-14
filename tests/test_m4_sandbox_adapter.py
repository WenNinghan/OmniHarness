import asyncio
import base64
import re
from types import SimpleNamespace

import httpx
import pytest
from opensandbox.exceptions import SandboxApiException
from opensandbox.models.sandboxes import SandboxEndpoint

from omniharness.sandbox import (
    SandboxBoundaryError,
    SandboxConfig,
    SandboxExecutor,
    SandboxResultUnknown,
    operation_metadata,
    verify_runtime_profile,
)


def config(**kwargs):
    return SandboxConfig(**({'endpoint': 'http://127.0.0.1:19080', 'api_key': 'test-only-' * 5,
                             'image': 'sha256:' + 'a' * 64, 'snapshot_root': '/snapshots',
                             'instance_id': 'test'} | kwargs))


class FakeSandbox:
    def __init__(self, output=None, *, exit_code=0, error=None, wait=False):
        self.id = 'sandbox-1'
        self.commands = self
        self.output = output if output is not None else ['one\n', '\n', 'two']
        self.exit_code, self.error, self.wait = exit_code, error, wait
        self.killed = self.closed = False
        self.started = asyncio.Event()

    async def create(self, *args, **kwargs):
        self.create_options = kwargs
        return self

    async def run(self, command, *, opts, handlers):
        self.opts, self.command = opts, command
        assert handlers.skip_accumulation
        await handlers.on_init(SimpleNamespace(id='cmd-1'))
        self.started.set()
        if self.wait:
            await asyncio.Event().wait()
        for text in self.output:
            await handlers.on_stdout(SimpleNamespace(text=text))
        return SimpleNamespace(id='cmd-1', exit_code=self.exit_code, error=self.error)

    async def kill(self):
        self.killed = True

    async def get_info(self):
        if self.killed:
            raise SandboxApiException('deleted', status_code=404)
        return SimpleNamespace()

    async def close(self):
        self.closed = True


async def execute(fake, *, configuration=None, **kwargs):
    logs, starts, created = [], [], []

    async def log(channel, text):
        logs.append((channel, text))

    async def start(sandbox, command):
        assert sandbox in created
        starts.append((sandbox, command))

    async def create(sandbox):
        created.append(sandbox)

    async def profile(sandbox, _config):
        assert sandbox.id in created
        sandbox.profile_checked = True

    result = await SandboxExecutor(configuration or config(), sdk=fake, profile_probe=profile).execute(
        **({'run_id': 'run-1', 'operation_id': 'op-1', 'snapshot_path': '/snapshots/run-1/version-1',
            'command': ['python', '-V'], 'log_sink': log, 'started_sink': start,
            'created_sink': create} | kwargs))
    return result, logs, starts


def test_config_requires_pinned_image_and_explicit_credentials():
    for overrides in ({'image': 'python:latest'}, {'api_key': ''}, {'endpoint': 'http://user:pass@localhost'},
                      {'snapshot_root': '/'}, {'max_command_seconds': 0}):
        with pytest.raises(SandboxBoundaryError):
            config(**overrides)
    value = config()
    assert value.api_key not in repr(value)
    connection = value.connection()
    assert connection.disable_metrics and not connection.enable_tracing
    assert connection.use_server_proxy and not connection.follow_redirects
    assert connection.retry_policy == type(connection.retry_policy).disabled()


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/snapshots', '/snapshots-other/run', '/snapshots/run/../../secrets',
                                  'G:\\project', '/secrets/key'])
async def test_snapshot_scope_rejected_before_create(path):
    fake = FakeSandbox()
    with pytest.raises(SandboxBoundaryError):
        await execute(fake, snapshot_path=path)
    assert not hasattr(fake, 'create_options')


def test_metadata_preserves_full_durable_ids_with_upstream_label_limits():
    values = {'omni_instance': 'i' * 80, 'omni_run': '_' + 'r' * 98 + '_', 'omni_operation': 'a' * 64}
    labels = operation_metadata(*values.values())
    for value in labels.values():
        assert len(value) <= 63
        assert re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?', value)
    for key, value in values.items():
        encoded = ''.join(labels[key if index == 0 else f'{key}_{index}']
                          for index in range(4) if (key if index == 0 else f'{key}_{index}') in labels)
        decoded = base64.b32decode(encoded.upper() + '=' * ((-len(encoded)) % 8)).decode()
        assert decoded == value


async def test_real_operation_length_is_encoded_before_sdk_dispatch():
    fake = FakeSandbox()
    await execute(fake, operation_id='f' * 64)
    assert fake.create_options['metadata']['omni_operation_encoding'] == 'base32'
    assert all(len(value) <= 63 for value in fake.create_options['metadata'].values())


@pytest.mark.asyncio
async def test_command_preserves_newlines_and_enforces_runtime_contract():
    fake = FakeSandbox()
    result, logs, starts = await execute(fake)
    assert result.stdout_preview == 'one\n\ntwo'
    assert ''.join(t for _, t in logs) == result.stdout_preview
    assert starts == [('sandbox-1', 'cmd-1')]
    assert fake.killed and fake.closed
    assert fake.opts.uid == fake.opts.gid == 10001
    assert fake.opts.timeout.total_seconds() == 120
    assert fake.create_options['secure_access'] is False
    assert fake.profile_checked
    token = fake.create_options['env']['EXECD_ACCESS_TOKEN']
    assert len(token) >= 32
    assert fake.create_options['connection_config'].headers['X-EXECD-ACCESS-TOKEN'] == token
    assert fake.create_options['network_policy'].default_action == 'deny'
    assert fake.create_options['volumes'][0].host.path == '/snapshots/run-1/version-1'
    assert 'extensions' not in fake.create_options
    assert all('KEY' not in key for key in fake.create_options['env'])
    assert config().api_key not in fake.create_options['env'].values()


@pytest.mark.asyncio
async def test_preview_bounded_but_sink_gets_all_text():
    fake = FakeSandbox(['中文\n', 'long output'])
    result, logs, _ = await execute(fake, configuration=config(preview_characters=3))
    assert result.stdout_preview == '中文\n' and result.preview_truncated
    assert len(logs) == 2


@pytest.mark.asyncio
async def test_storage_limit_terminates_command_not_fake_success():
    fake = FakeSandbox(['large output'])
    with pytest.raises(SandboxBoundaryError, match='incomplete'):
        await execute(fake, configuration=config(max_log_bytes=2))
    assert fake.killed and fake.closed


@pytest.mark.asyncio
async def test_unknown_exit_is_not_zero():
    fake = FakeSandbox(exit_code=None)
    with pytest.raises(SandboxResultUnknown, match='exit status'):
        await execute(fake)
    assert fake.killed


@pytest.mark.asyncio
async def test_cancel_kills_operation_container():
    fake = FakeSandbox(wait=True)
    reclaimed = []

    async def record_reclaimed(identity):
        assert fake.killed and not fake.closed
        reclaimed.append(identity)

    task = asyncio.create_task(execute(fake, reclaimed_sink=record_reclaimed))
    await fake.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.killed and fake.closed
    assert reclaimed == ['sandbox-1']


@pytest.mark.asyncio
async def test_cancel_during_creation_does_not_prove_remote_creation_stopped():
    """A controller can finish creation after the client task was cancelled."""
    fake = FakeSandbox()
    request_received, finish_creation = asyncio.Event(), asyncio.Event()
    server_created = asyncio.Event()
    server_tasks = []
    reclaimed = []

    async def server_create():
        request_received.set()
        await finish_creation.wait()
        server_created.set()

    async def create(*args, **kwargs):
        server_tasks.append(asyncio.create_task(server_create()))
        await request_received.wait()
        await asyncio.Event().wait()  # Lost or cancelled client response.

    async def record_reclaimed(identity):
        reclaimed.append(identity)

    fake.create = create
    task = asyncio.create_task(execute(fake, reclaimed_sink=record_reclaimed))
    await request_received.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    finish_creation.set()
    await asyncio.gather(*server_tasks)
    assert server_created.is_set()
    assert not fake.killed and not fake.closed and not reclaimed
    assert not hasattr(fake, 'command')


@pytest.mark.asyncio
async def test_reclamation_receipt_failure_prevents_success():
    fake = FakeSandbox()

    async def fail(_identity):
        raise OSError('private database detail')

    with pytest.raises(SandboxResultUnknown, match='cleanup is unconfirmed'):
        await execute(fake, reclaimed_sink=fail)
    assert fake.killed and fake.closed


@pytest.mark.asyncio
async def test_callback_failure_never_reports_success():
    fake = FakeSandbox()

    async def fail(*args):
        raise OSError('private storage path')

    with pytest.raises(SandboxBoundaryError) as error:
        await execute(fake, started_sink=fail)
    assert 'private storage path' not in str(error.value)
    assert fake.killed and fake.closed


@pytest.mark.asyncio
async def test_sandbox_identity_persisted_before_dispatch():
    fake = FakeSandbox()

    async def fail(*args):
        raise OSError('private identity path')

    with pytest.raises(SandboxBoundaryError, match='before dispatch'):
        await execute(fake, created_sink=fail)
    assert not hasattr(fake, 'command')
    assert fake.killed and fake.closed


@pytest.mark.asyncio
async def test_cleanup_waits_for_confirmed_delete_even_after_second_cancel():
    fake = FakeSandbox(wait=True)
    accepted, removed = asyncio.Event(), asyncio.Event()

    async def kill():
        fake.killed = True
        accepted.set()

    async def get_info():
        await removed.wait()
        raise SandboxApiException('deleted', status_code=404)

    fake.kill, fake.get_info = kill, get_info
    task = asyncio.create_task(execute(fake))
    await fake.started.wait()
    task.cancel()
    await accepted.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not fake.closed
    removed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('profile,expected_success', [
    ({'profile': 'omniharness-m4-execd-v1', 'workload_uid': 10001, 'workload_gid': 10001,
      'init_mode': True, 'signal_shield': True, 'cap_drop': 'active', 'seccomp': 'active'}, True),
    ({'profile': 'omniharness-m4-execd-v1', 'workload_uid': 0}, False),
    ({'profile': 'upstream-unpatched'}, False),
])
async def test_runtime_profile_rejects_unhardened_service(monkeypatch, profile, expected_success):
    connection = config().connection(execd_token='independent-daemon-token')

    async def endpoint(_port):
        return SandboxEndpoint(endpoint='127.0.0.1:19080/v1/sandboxes/test/proxy/44772')

    def respond(request):
        assert request.headers['X-EXECD-ACCESS-TOKEN'] == 'independent-daemon-token'
        assert request.headers['OPEN-SANDBOX-API-KEY'] == config().api_key
        return httpx.Response(200, json=profile)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    sandbox = SimpleNamespace(get_endpoint=endpoint, connection_config=connection)
    if expected_success:
        await verify_runtime_profile(sandbox, config())
    else:
        with pytest.raises(SandboxBoundaryError, match='not active'):
            await verify_runtime_profile(sandbox, config())


@pytest.mark.asyncio
async def test_runtime_profile_never_sends_credentials_to_direct_endpoint():
    async def endpoint(_port):
        return SandboxEndpoint(endpoint='172.17.0.15:44772')

    sandbox = SimpleNamespace(get_endpoint=endpoint, connection_config=config().connection())
    with pytest.raises(SandboxBoundaryError, match='controller proxy'):
        await verify_runtime_profile(sandbox, config())
