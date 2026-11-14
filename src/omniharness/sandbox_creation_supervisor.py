"""One-shot dispatch inside the independently hosted creation service.

Caller cancellation does not cancel an already dispatched create. A process
crash or lost upstream response remains unknown in the durable journal. This
module neither authenticates requests nor reclaims resources or tool slots.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .sandbox_creation_journal import CreationBinding, CreationJournal, CreationRecord


class CreationAlreadyDispatched(RuntimeError):
    """An existing record must be inspected, never automatically replayed."""


class CreationSupervisor:
    def __init__(self, journal: CreationJournal):
        self.journal = journal
        self._tasks: set[asyncio.Task] = set()
        self._claims: set[CreationBinding] = set()

    def claim(self, binding: CreationBinding) -> bool:
        """Called under authorization locks; only this process can consume it.

        A durable DISPATCHED row is evidence of uncertainty, never a capability
        to dispatch again after a restart or a lost authorization response.
        """
        if not self.journal.claim_dispatch(binding):
            return False
        self._claims.add(binding)
        return True

    async def create(self, binding: CreationBinding,
                     create_once: Callable[[], Awaitable[str]]) -> CreationRecord:
        if not callable(create_once):
            raise TypeError("create_once must be callable")
        if not self.claim(binding):
            raise CreationAlreadyDispatched("Creation exists and cannot be replayed")
        return await self.create_claimed(binding, create_once)

    async def create_claimed(self, binding: CreationBinding,
                             create_once: Callable[[], Awaitable[str]]) -> CreationRecord:
        """Dispatch a claim committed by the trusted authorization boundary."""
        if binding not in self._claims:
            raise CreationAlreadyDispatched("No fresh in-process dispatch capability")
        # Consume before any I/O/await. A failed check is not retry permission.
        self._claims.remove(binding)
        record = self.journal.lookup(binding)
        if record is None or record.state != "DISPATCHED" or not callable(create_once):
            raise CreationAlreadyDispatched("Creation claim is unavailable")

        async def dispatch():
            sandbox_id = await create_once()
            return self.journal.record_created(binding, sandbox_id)

        task = asyncio.create_task(dispatch())
        self._tasks.add(task)

        def done(completed):
            self._tasks.discard(completed)
            # A disconnected caller cannot consume an upstream exception.
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def drain(self):
        """Wait for current tasks; a hung upstream intentionally remains pending."""
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
