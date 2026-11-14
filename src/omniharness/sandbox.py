"""Constrained OpenSandbox SDK adapter; host authorization is a separate layer.

Only a trusted host supplies this configuration and a broker-created snapshot.
No Docker options, credentials, environment, images or mounts come from a model.
"""
from __future__ import annotations

import asyncio
import base64
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxApiException
from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts
from opensandbox.models.sandboxes import Host, NetworkPolicy, Volume
from opensandbox.sandbox import Sandbox
from opensandbox.transport import RetryPolicy

from .sandbox_stream import sandbox_response_hook


class SandboxBoundaryError(RuntimeError):
    """Static safe error, never a raw SDK exception containing credentials."""


class SandboxResultUnknown(SandboxBoundaryError):
    pass


def _absolute_directory(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.startswith('/') or '\\' in value or '\x00' in value:
        raise SandboxBoundaryError('Invalid sandbox directory')
    if '..' in value.split('/'):
        raise SandboxBoundaryError('Directory traversal is not allowed')
    result = PurePosixPath(value)
    if result == PurePosixPath('/'):
        raise SandboxBoundaryError('A filesystem root cannot be authorized')
    return result


@dataclass(frozen=True)
class SandboxConfig:
    endpoint: str
    api_key: str = field(repr=False)
    image: str
    snapshot_root: str
    instance_id: str
    max_command_seconds: int = 600
    max_log_bytes: int = 20 * 1024 * 1024
    preview_characters: int = 4096

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ('', '/')):
            raise SandboxBoundaryError('Invalid controller endpoint')
        if not isinstance(self.api_key, str) or len(self.api_key) < 32:
            raise SandboxBoundaryError('An explicit controller credential is required')
        if not re.fullmatch(r'(?:[^\s@]+@)?sha256:[a-f0-9]{64}', self.image):
            raise SandboxBoundaryError('The tool image must be pinned by digest or local image ID')
        _absolute_directory(self.snapshot_root)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', self.instance_id):
            raise SandboxBoundaryError('Invalid instance identity')
        for value, maximum in ((self.max_command_seconds, 600), (self.max_log_bytes, 100 * 1024 * 1024),
                               (self.preview_characters, 65536)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise SandboxBoundaryError('Invalid sandbox resource limit')

    def connection(self, *, execd_token: str | None = None) -> ConnectionConfig:
        url = urlsplit(self.endpoint)
        headers = {'Accept-Encoding': 'identity'}
        if execd_token:
            headers['X-EXECD-ACCESS-TOKEN'] = execd_token
        return ConnectionConfig(domain=url.netloc, protocol=url.scheme, api_key=self.api_key,
                                request_timeout=timedelta(seconds=30), use_server_proxy=True,
                                follow_redirects=False, disable_metrics=True, enable_tracing=False,
                                headers=headers,
                                event_hooks={'response': [sandbox_response_hook()]},
                                retry_policy=RetryPolicy.disabled(), debug=False)


@dataclass(frozen=True)
class CommandResult:
    sandbox_id: str
    command_id: str
    exit_code: int
    stdout_preview: str
    stderr_preview: str
    log_bytes: int
    preview_truncated: bool


LogSink = Callable[[str, str], Awaitable[None]]
StartedSink = Callable[[str, str], Awaitable[None]]
CreatedSink = Callable[[str], Awaitable[None]]
ReclaimedSink = Callable[[str], Awaitable[None]]


def operation_metadata(instance_id: str, run_id: str, operation_id: str) -> dict[str, str]:
    """Preserve IDs losslessly within the upstream 63-character label contract."""
    labels = {}
    for key, value in {'omni_instance': instance_id, 'omni_run': run_id,
                       'omni_operation': operation_id}.items():
        if len(value) <= 63 and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?', value):
            labels[key] = value
        else:
            encoded = base64.b32encode(value.encode('utf-8')).decode('ascii').rstrip('=').lower()
            labels[key + '_encoding'] = 'base32'
            for index, start in enumerate(range(0, len(encoded), 63)):
                labels[key if index == 0 else f'{key}_{index}'] = encoded[start:start + 63]
    return labels


async def verify_runtime_profile(sandbox, config: SandboxConfig) -> None:
    """Original Docker execd does not satisfy this contract; fail closed.

    Native execd authentication is distinct from OpenSandbox's K8s-only
    secureAccess ingress option. The patched service attests its fixed profile.
    """
    endpoint = await sandbox.get_endpoint(44772)
    connection = sandbox.connection_config
    target = f'{connection.protocol}://{endpoint.endpoint}/omni/profile'
    origin, expected = urlsplit(target), urlsplit(config.endpoint)
    if (origin.scheme, origin.netloc) != (expected.scheme, expected.netloc):
        raise SandboxBoundaryError('Runtime checks require the authenticated controller proxy')
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5,
                                 event_hooks={'response': [sandbox_response_hook()]}) as client:
        response = await client.get(target, headers=endpoint.build_request_headers(connection))
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            raise SandboxBoundaryError('Runtime profile is invalid') from None
    required = {'profile': 'omniharness-m4-execd-v1', 'workload_uid': 10001, 'workload_gid': 10001,
                'init_mode': True, 'signal_shield': True, 'cap_drop': 'active', 'seccomp': 'active'}
    if not isinstance(body, dict) or any(body.get(key) != value for key, value in required.items()):
        raise SandboxBoundaryError('The hardened runtime profile is not active')


class SandboxExecutor:
    """One disposable sandbox per operation, reusing the broker's snapshot.

    The caller owns durable concurrency claims, approval, budgets and full log
    storage. Callback failure aborts execution; a preview is never full evidence.
    """

    def __init__(self, config: SandboxConfig, *, sdk=Sandbox, profile_probe=verify_runtime_profile,
                 creation_client=None):
        self.config, self.sdk, self.profile_probe = config, sdk, profile_probe
        self.creation_client = creation_client

    def _snapshot(self, path: str) -> str:
        root, candidate = _absolute_directory(self.config.snapshot_root), _absolute_directory(path)
        # Physical identity/link checks are performed by the broker and server;
        # this independent lexical check does not pretend to resolve WSL paths.
        if root not in candidate.parents:
            raise SandboxBoundaryError('Snapshot is outside its authorized root')
        return str(candidate)

    async def execute(self, *, run_id: str, operation_id: str, snapshot_path: str,
                      command: str | list[str], log_sink: LogSink, started_sink: StartedSink,
                      created_sink: CreatedSink, reclaimed_sink: ReclaimedSink | None = None,
                      cwd: str = '.', timeout_seconds: int = 120,
                      operation_fingerprint: str | None = None, dispatch_guard=None) -> CommandResult:
        path = self._snapshot(snapshot_path)
        for identity in (run_id, operation_id):
            if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identity):
                raise SandboxBoundaryError('Invalid operation identity')
        if (not isinstance(cwd, str) or cwd.startswith('/') or '\\' in cwd or '\x00' in cwd
                or '..' in cwd.split('/')):
            raise SandboxBoundaryError('Invalid command working directory')
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int)
                or not 1 <= timeout_seconds <= self.config.max_command_seconds):
            raise SandboxBoundaryError('Command timeout is outside the configured limit')
        if isinstance(command, str):
            valid_command = bool(command.strip()) and '\x00' not in command and len(command) <= 32768
        else:
            valid_command = (isinstance(command, list) and 0 < len(command) <= 256
                             and all(isinstance(x, str) and '\x00' not in x for x in command)
                             and bool(command[0]) and sum(map(len, command)) <= 32768)
        if (not valid_command or not all(callable(x) for x in (log_sink, started_sink, created_sink))
                or reclaimed_sink is not None and not callable(reclaimed_sink)):
            raise SandboxBoundaryError('Invalid command or evidence callbacks')
        sandbox = None
        creation_request = None
        created_id = None
        command_id = None
        previews = {'stdout': '', 'stderr': ''}
        log_bytes, truncated = 0, False

        async def output(channel, event):
            nonlocal log_bytes, truncated
            text = event.text
            size = len(text.encode('utf-8'))
            if log_bytes + size > self.config.max_log_bytes:
                raise SandboxBoundaryError('Command log storage limit reached; evidence is incomplete')
            try:
                await log_sink(channel, text)
            except Exception:
                # The SDK logs callback failures before propagating them.
                # Never let filesystem paths/credentials enter that traceback.
                raise SandboxBoundaryError('Command evidence could not be saved') from None
            log_bytes += size
            remaining = max(0, self.config.preview_characters - len(previews[channel]))
            previews[channel] += text[:remaining]
            truncated |= len(text) > remaining

        async def started(event):
            nonlocal command_id
            command_id = event.id
            try:
                await started_sink(sandbox.id, command_id)
            except Exception:
                raise SandboxBoundaryError('Command identity could not be saved') from None

        async def terminate():
            if self.creation_client is not None and created_id is not None:
                receipt = await self.creation_client.reclaim(creation_request)
                if receipt['sandbox_id'] != created_id:
                    raise SandboxResultUnknown('Creation reclamation identity differs')
                if reclaimed_sink is not None:
                    await reclaimed_sink(created_id)
                return
            if sandbox is not None:
                # Killing this operation's container also terminates detached
                # grandchildren; cancelling an HTTP request alone does not.
                try:
                    await sandbox.kill()
                except SandboxApiException as exc:
                    if exc.status_code != 404:
                        raise
                # SDK kill acknowledges deletion acceptance, not completion.
                # M4-A additionally verifies the controller's 404 corresponds
                # to actual Docker process cleanup before this can be enabled.
                while True:
                    try:
                        await sandbox.get_info()
                    except SandboxApiException as exc:
                        if exc.status_code == 404:
                            # Resource reclamation is distinct from command
                            # completion, including cancellation/unknown output.
                            if reclaimed_sink is not None:
                                try:
                                    await reclaimed_sink(sandbox.id)
                                except Exception:
                                    raise SandboxBoundaryError('Sandbox reclamation could not be recorded') from None
                            return
                        raise
                    await asyncio.sleep(0.1)

        try:
            # Per-operation daemon credential, never a provider/controller Key.
            # The hardened execd profile removes it from workload environments
            # and protects the supervisor from non-root /proc access.
            execd_token = secrets.token_urlsafe(32)
            if self.creation_client is not None:
                from .sandbox_creation_api import CreationRequest
                creation_request = CreationRequest(run_id=run_id, operation_id=operation_id,
                    operation_fingerprint=operation_fingerprint, snapshot_path=path,
                    execd_token=execd_token, timeout_seconds=timeout_seconds)
                created_id = await self.creation_client.create(creation_request)
                # Save identity before connecting or dispatching a workload.
                await created_sink(created_id)
                sandbox = await self.sdk.connect(created_id,
                    connection_config=self.config.connection(execd_token=execd_token))
                if sandbox.id != created_id:
                    raise SandboxBoundaryError('Connected sandbox identity differs')
            else:
                sandbox = await self.sdk.create(
                    self.config.image, connection_config=self.config.connection(execd_token=execd_token),
                    timeout=timedelta(seconds=timeout_seconds + 120),
                    # Docker rejects K8s secureAccess. Native execd token + the
                    # mandatory profile check below protects this path instead.
                    ready_timeout=timedelta(seconds=60), secure_access=False,
                    env={'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                         'EXECD_ACCESS_TOKEN': execd_token},
                    metadata=operation_metadata(self.config.instance_id, run_id, operation_id),
                    resource={'cpu': '2', 'memory': '2Gi'},
                    network_policy=NetworkPolicy(default_action='deny', egress=[]),
                    volumes=[Volume(name='workspace', host=Host(path=path),
                                    mount_path='/workspace', read_only=False)],
                )
            try:
                if self.creation_client is None:
                    await created_sink(sandbox.id)
            except Exception:
                raise SandboxBoundaryError('Sandbox identity could not be saved before dispatch') from None
            await self.profile_probe(sandbox, self.config)
            if dispatch_guard is not None:
                await dispatch_guard()
            handlers = ExecutionHandlers(on_init=started,
                                         on_stdout=lambda e: output('stdout', e),
                                         on_stderr=lambda e: output('stderr', e),
                                         skip_accumulation=True)
            async with asyncio.timeout(timeout_seconds + 15):
                result = await sandbox.commands.run(command, opts=RunCommandOpts(
                    working_directory=str(PurePosixPath('/workspace') / cwd),
                    timeout=timedelta(seconds=timeout_seconds), uid=10001, gid=10001,
                    background=False), handlers=handlers)
            if not command_id or result.id != command_id:
                raise SandboxResultUnknown('Command identity was not confirmed')
            code = result.exit_code
            if code is None and result.error is not None:
                code = result.error.exit_code
            if code is None:
                raise SandboxResultUnknown('The command did not report a confirmed exit status')
            return CommandResult(sandbox.id, command_id, code, previews['stdout'], previews['stderr'],
                                 log_bytes, truncated)
        except asyncio.CancelledError:
            raise
        except SandboxBoundaryError:
            raise
        except Exception:
            # Even a connection error can occur after dispatch. Reconciliation
            # must use the stored IDs/metadata; never retry command creation.
            raise SandboxResultUnknown('Sandbox operation failed; reconcile before retrying') from None
        finally:
            if sandbox is not None or created_id is not None:
                cleanup = asyncio.create_task(asyncio.wait_for(terminate(), timeout=100 if created_id else 15))
                cancelled_during_cleanup = False
                try:
                    while True:
                        try:
                            await asyncio.shield(cleanup)
                            break
                        except asyncio.CancelledError:
                            if cleanup.cancelled():
                                raise SandboxResultUnknown('Sandbox cleanup was interrupted') from None
                            cancelled_during_cleanup = True
                            # A second cancellation must not close the SDK's
                            # transport while the bounded cleanup is running.
                            continue
                except Exception:
                    # Cleanup uncertainty is material, including after an
                    # otherwise successful command. Do not report success.
                    raise SandboxResultUnknown('Sandbox cleanup is unconfirmed') from None
                finally:
                    try:
                        if sandbox is not None:
                            await sandbox.close()
                    except Exception:
                        raise SandboxBoundaryError('Sandbox connection cleanup failed') from None
                if cancelled_during_cleanup:
                    raise asyncio.CancelledError
