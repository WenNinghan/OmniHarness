"""Run-local depth and concurrency leases for nested agent delegation.

This module is an integration building block, not a task dispatcher, sandbox,
database budget, or ownership fence.  A host must use one scheduler per Run,
invoke ``ensure_active`` before every model/ordinary-tool boundary, and await
``aclose`` before abandoning a graph or starting another Run.  In particular,
some LangGraph cancellation paths return before nested tool tasks have ended.
"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, TypeVar


class DelegationError(RuntimeError):
    """A static error without user input, tool arguments, or credentials."""


class DelegationDepthExceeded(DelegationError):
    pass


class DelegationDrainTimeout(DelegationError):
    pass


_current: contextvars.ContextVar[_Lease | None] = contextvars.ContextVar("omni_delegation_lease", default=None)
_T = TypeVar("_T")


@dataclass(eq=False)
class _Lease:
    scheduler: HierarchicalDelegation
    depth: int
    held: bool = True
    children: int = 0
    closed: bool = False

    def __post_init__(self):
        self._ready = asyncio.Event()
        self._ready.set()
        self._ensure_lock = asyncio.Lock()

    def suspend(self) -> None:
        if self.closed:
            raise DelegationError("Delegation lease is closed")
        self.children += 1
        self._ready.clear()
        if self.children == 1 and self.held:
            self.held = False
            self.scheduler._release()

    def child_finished(self) -> None:
        if self.children <= 0:
            raise DelegationError("Delegation lease accounting is invalid")
        self.children -= 1
        if self.children == 0:
            self._ready.set()

    async def ensure_active(self) -> None:
        # Multiple ordinary tool calls of one Agent can run in separate asyncio
        # tasks. Only one may reacquire its suspended parent lease.
        async with self._ensure_lock:
            while True:
                await self._ready.wait()
                if self.closed or self.scheduler.closed:
                    raise DelegationError("Delegation lease is closed")
                if self.held:
                    return
                await self.scheduler._acquire()
                # A new child may have started while this task awaited a slot.
                if self.closed or self.scheduler.closed or self.children:
                    self.scheduler._release()
                    if self.closed or self.scheduler.closed:
                        raise DelegationError("Delegation lease is closed")
                    continue
                self.held = True
                return

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._ready.set()
            if self.held:
                self.held = False
                self.scheduler._release()


class HierarchicalDelegation:
    """Bound active non-root Agents while allowing suspended parents to yield.

    Asyncio context propagation scopes each lease to a delegation branch. The
    root coordinator has depth 0; ``delegate`` creates depth 1 or 2. Sibling
    descendants share a reference count so the parent cannot run model/tools
    until every descendant returns. All methods must run on the same event loop.
    """

    def __init__(self, *, max_active: int = 2, max_depth: int = 2):
        if isinstance(max_active, bool) or not isinstance(max_active, int) or max_active < 1:
            raise ValueError("max_active must be a positive integer")
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1:
            raise ValueError("max_depth must be a positive integer")
        self.max_active, self.max_depth = max_active, max_depth
        self._semaphore = asyncio.Semaphore(max_active)
        self._active = 0
        self._tasks: dict[asyncio.Task, int] = {}
        self._operations: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drain_task: asyncio.Task | None = None
        self.closed = False

    @property
    def active_count(self) -> int:
        return self._active

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise DelegationError("Delegation scheduler cannot cross event loops")

    async def _acquire(self) -> None:
        await self._semaphore.acquire()
        self._active += 1

    def _release(self) -> None:
        if self._active <= 0:
            raise DelegationError("Delegation permit accounting is invalid")
        self._active -= 1
        self._semaphore.release()

    def _parent(self) -> _Lease | None:
        parent = _current.get()
        if parent is not None and (parent.scheduler is not self or parent.closed):
            raise DelegationError("Delegation context belongs to another or closed Run")
        return parent

    async def ensure_active(self) -> None:
        """Call immediately before a model or non-delegation tool boundary."""
        self._check_loop()
        if self.closed:
            raise DelegationError("Delegation scheduler is closed")
        parent = self._parent()
        if parent is not None:
            await parent.ensure_active()

    @staticmethod
    def _cancel_once(task: asyncio.Task) -> None:
        # Repeated Task.cancel() can interrupt the provider's own async cleanup.
        if not task.done() and not task.cancelling():
            task.cancel()

    def _forget_operation(self, task: asyncio.Task) -> None:
        self._operations.discard(task)
        # A cancelled caller no longer awaits the shielded task. Retrieve its
        # exception to prevent asyncio logging unsanitized provider errors.
        if not task.cancelled():
            task.exception()

    async def run_tracked(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        """Shield and register one SDK handler until it and its cleanup finish.

        Call for model, ordinary tool and delegation handlers. Put the
        ``delegate`` context *inside* this operation, so a cancelled outer
        graph cannot release its permit while the child still runs.
        """
        self._check_loop()
        if self.closed:
            raise DelegationError("Delegation scheduler is closed")
        task = asyncio.create_task(operation())
        self._operations.add(task)
        task.add_done_callback(self._forget_operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._cancel_once(task)
            raise

    @asynccontextmanager
    async def delegate(self) -> AsyncIterator[int]:
        """Enter a child graph after permission/control checks; yield its depth.

        The host must apply role and delegation permissions separately. Do not
        put credentials or input messages in scheduler context.
        """
        self._check_loop()
        if self.closed:
            raise DelegationError("Delegation scheduler is closed")
        parent = self._parent()
        depth = (parent.depth if parent else 0) + 1
        if depth > self.max_depth:
            raise DelegationDepthExceeded("Maximum delegation depth exceeded")
        task = asyncio.current_task()
        if task is None:
            raise DelegationError("Delegation requires an asyncio task")
        self._tasks[task] = self._tasks.get(task, 0) + 1
        parent_suspended, lease, token, acquired = False, None, None, False
        try:
            if parent is not None:
                parent.suspend()
                parent_suspended = True
            await self._acquire()
            acquired = True
            if self.closed:
                raise DelegationError("Delegation scheduler is closed")
            lease = _Lease(self, depth)
            token = _current.set(lease)
            yield depth
        finally:
            # No await in cleanup: repeated cancellation must not interrupt it.
            if token is not None:
                _current.reset(token)
            if lease is not None:
                lease.close()
            elif acquired:
                self._release()
            if parent_suspended:
                parent.child_finished()
            remaining = self._tasks[task] - 1
            if remaining:
                self._tasks[task] = remaining
            else:
                del self._tasks[task]

    async def _drain(self, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self._tasks or self._operations:
            tasks = set(self._tasks) | self._operations
            for task in tasks:
                self._cancel_once(task)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise DelegationDrainTimeout("Delegation tasks did not stop before timeout")
            _, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                raise DelegationDrainTimeout("Delegation tasks did not stop before timeout")
        if self._active:
            raise DelegationError("Delegation permits remain active after drain")

    async def wait_operations(self) -> None:
        """Wait for already-dispatched handlers at an approval boundary.

        The caller first closes dispatch through its host control gate. This
        method neither grants work nor cancels evidence-producing operations.
        Run cancellation/time budgets remain the owning Worker's responsibility.
        """
        self._check_loop()
        if asyncio.current_task() in self._operations:
            raise DelegationError("An operation cannot wait for its own completion")
        while self._operations:
            await asyncio.wait(set(self._operations))

    async def aclose(self, *, timeout: float = 10.0) -> None:
        """Cancel and await child tasks; fail closed if any refuse to stop.

        Must be called by the host, not inside a registered child task. Cleanup
        survives repeated cancellation of the caller. After cancellation, caller
        cancellation is re-raised once the children have actually stopped.
        """
        self._check_loop()
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if asyncio.current_task() in self._tasks or asyncio.current_task() in self._operations:
            raise DelegationError("A child Agent cannot close its own Run scheduler")
        self.closed = True
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain(timeout))
        cancelled = False
        while not self._drain_task.done():
            try:
                await asyncio.shield(self._drain_task)
            except asyncio.CancelledError:
                cancelled = True
        self._drain_task.result()
        if cancelled:
            raise asyncio.CancelledError
