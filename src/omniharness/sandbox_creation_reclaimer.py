"""Candidate bound reclamation; no application slot is released here.

The caller must supply a trusted engine verifier and fence it against any new
creation. Controller 404 alone is not sufficient evidence of Docker cleanup.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from opensandbox.exceptions import SandboxApiException

from .sandbox import operation_metadata
from .sandbox_creation_journal import CreationRecord


class ReclamationUnknown(RuntimeError):
    pass


async def reclaim_created(record: CreationRecord, manager,
                          verify_engine_absence: Callable[[CreationRecord], Awaitable[bool]],
                          *, attempts: int = 20, interval: float = 0.1):
    """Reclaim only a journal-bound live sandbox; never infer command outcome."""
    if (record.state != "CREATED" or not record.sandbox_id or not callable(verify_engine_absence)
            or type(attempts) is not int or not 1 <= attempts <= 100
            or type(interval) not in (int, float) or not 0 <= interval <= 1):
        raise ReclamationUnknown("Reclamation binding is unavailable")
    try:
        try:
            info = await manager.get_sandbox_info(record.sandbox_id)
        except SandboxApiException as exc:
            if exc.status_code != 404:
                raise
            info = None
        if info is not None:
            metadata = info.metadata if isinstance(info.metadata, dict) else {}
            expected = operation_metadata(record.binding.instance_id, record.binding.run_id, record.binding.operation_id)
            if info.id != record.sandbox_id or any(metadata.get(key) != value for key, value in expected.items()):
                raise ReclamationUnknown("Sandbox metadata differs")
            try:
                await manager.kill_sandbox(record.sandbox_id)
            except SandboxApiException as exc:
                if exc.status_code != 404:
                    raise
        for attempt in range(attempts):
            try:
                await manager.get_sandbox_info(record.sandbox_id)
            except SandboxApiException as exc:
                if exc.status_code != 404:
                    raise
                if await verify_engine_absence(record) is True:
                    return {"kind": "bound_resource_reclamation", "sandbox_id": record.sandbox_id,
                            "resource_reclaimed": True, "outcome": "unknown", "replayed": False}
            if attempt + 1 < attempts:
                await asyncio.sleep(interval)
    except ReclamationUnknown:
        raise
    except Exception:
        raise ReclamationUnknown("Reclamation could not be verified") from None
    raise ReclamationUnknown("Controller and engine reclamation are not confirmed")
