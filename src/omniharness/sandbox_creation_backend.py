"""Candidate real-SDK creation backend, not wired to production execution.

Authorization of workspace snapshots and authentication of callers are
responsibilities of a future service boundary, not supplied by this module.
"""
from __future__ import annotations

import re
from datetime import timedelta

from opensandbox.models.sandboxes import Host, NetworkPolicy, Volume
from opensandbox.sandbox import Sandbox

from .sandbox import SandboxBoundaryError, SandboxConfig, SandboxExecutor, operation_metadata
from .sandbox_creation_journal import CreationBinding


class SandboxCreationBackend:
    def __init__(self, config: SandboxConfig, *, sdk=Sandbox):
        self.config = config
        self.sdk = sdk

    async def create(self, binding: CreationBinding, *, snapshot_path: str,
                     execd_token: str, timeout_seconds: int) -> str:
        if binding.instance_id != self.config.instance_id:
            raise SandboxBoundaryError("Creation instance differs from service")
        if (not isinstance(execd_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", execd_token)
                or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int)
                or not 1 <= timeout_seconds <= self.config.max_command_seconds):
            raise SandboxBoundaryError("Invalid creation parameters")
        path = SandboxExecutor(self.config)._snapshot(snapshot_path)
        sandbox = await self.sdk.create(
            self.config.image, connection_config=self.config.connection(execd_token=execd_token),
            timeout=timedelta(seconds=timeout_seconds + 120), ready_timeout=timedelta(seconds=60),
            secure_access=False,
            env={"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "EXECD_ACCESS_TOKEN": execd_token},
            metadata=operation_metadata(binding.instance_id, binding.run_id, binding.operation_id),
            resource={"cpu": "2", "memory": "2Gi"},
            network_policy=NetworkPolicy(default_action="deny", egress=[]),
            volumes=[Volume(name="workspace", host=Host(path=path),
                            mount_path="/workspace", read_only=False)],
        )
        try:
            return sandbox.id
        finally:
            # Closing the client is not deletion and does not prove reclamation.
            await sandbox.close()
