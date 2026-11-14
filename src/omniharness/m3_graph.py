"""M3 durable graph building blocks; not connected to the product Worker yet.

This component does not enforce database fencing. Its session factory does not
check execution ownership; it MUST NOT be connected to product-side effects
until the host implements fencing across every claim, callback, and write.
Checkpoint state deliberately contains no model credentials or user payload.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable, TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy.engine import make_url

from .services import begin_operation, finish_operation, mark_uncertain

WORKFLOW_VERSION = "m3-graph-1"
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


class GraphError(RuntimeError):
    """Safe, static error messages only; never embed provider or connection data."""


class GraphAlreadyExists(GraphError):
    pass


class GraphNotResumable(GraphError):
    pass


class VersionMismatch(GraphError):
    pass


class OperationNeedsReview(GraphError):
    pass


class OperationFailed(GraphError):
    pass


@dataclass(frozen=True)
class OperationContext:
    run_id: str
    node_id: str
    invocation_id: str
    workflow_version: str
    input_fingerprint: str


class GraphState(TypedDict, total=False):
    run_id: str
    node_id: str
    invocation_id: str
    workflow_version: str
    input_fingerprint: str
    pause_required: bool
    result_ref: str


Execute = Callable[[OperationContext], Awaitable[str]]
ShouldPause = Callable[[OperationContext], Awaitable[bool]]


def _validate_context(context: OperationContext) -> None:
    for value in (context.run_id, context.node_id, context.invocation_id, context.workflow_version):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError("Invalid graph identity")
    if not isinstance(context.input_fingerprint, str) or not _FINGERPRINT.fullmatch(context.input_fingerprint):
        raise ValueError("input_fingerprint must be a SHA-256 hexadecimal digest")
    if context.workflow_version != WORKFLOW_VERSION:
        raise VersionMismatch("Workflow version is incompatible with this graph")


def _context(state: GraphState) -> OperationContext:
    value = OperationContext(
        run_id=state["run_id"], node_id=state["node_id"], invocation_id=state["invocation_id"],
        workflow_version=state["workflow_version"], input_fingerprint=state["input_fingerprint"],
    )
    _validate_context(value)
    return value


def _reference(value: str) -> str:
    # Result references are host-created opaque identifiers, not model output or URLs.
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("result_ref must be an opaque identifier")
    return value


class OperationLedger:
    """Commit operation claims before calling the host; never retry uncertain work.

    Not fenced: this class does not check the Worker's database lock or lease.
    Holding a lock initially is insufficient if its connection can later fail.
    Do not connect to real external side effects before fencing is implemented;
    otherwise interpreting a running entry as abandoned and writing its outcome
    can race with a new Worker after ownership is lost.
    """

    def __init__(self, session_factory):
        self.sessions = session_factory

    @staticmethod
    def _parameters(context: OperationContext) -> dict[str, str]:
        # The invocation identifies the operation. Bind version and fingerprint
        # separately so a changed input cannot create a second executable claim.
        return {"invocation_id": context.invocation_id}

    def _claim(self, context: OperationContext):
        with self.sessions.begin() as db:
            row, executable = begin_operation(
                db, context.run_id, context.node_id, self._parameters(context), context.workflow_version
            )
            if row.workflow_version != context.workflow_version:
                raise VersionMismatch("Stored operation version is incompatible")
            if executable:
                row.result = {"input_fingerprint": context.input_fingerprint}
                return row.operation_id, None
            if (row.result or {}).get("input_fingerprint") != context.input_fingerprint:
                raise GraphError("Operation input does not match its existing identity")
            if row.status == "completed":
                return None, _reference((row.result or {}).get("result_ref"))
            if row.status == "running":
                mark_uncertain(db, row.operation_id, "ABANDONED_OPERATION")
                return None, None
            if row.status == "uncertain":
                return None, None
            raise OperationFailed("Previous operation did not complete; explicit review is required")

    def _finish(self, operation_id: str, result_ref: str, input_fingerprint: str) -> bool:
        with self.sessions.begin() as db:
            return finish_operation(db, operation_id, "completed", result={
                "result_ref": result_ref, "input_fingerprint": input_fingerprint,
            })

    def _uncertain(self, operation_id: str) -> None:
        with self.sessions.begin() as db:
            mark_uncertain(db, operation_id, "RESULT_UNKNOWN")

    async def execute_once(self, context: OperationContext, execute: Execute) -> str:
        _validate_context(context)
        operation_id, completed_ref = await asyncio.to_thread(self._claim, context)
        if completed_ref is not None:
            return completed_ref
        if operation_id is None:
            raise OperationNeedsReview("Operation result is unknown; review is required")
        try:
            result_ref = _reference(await execute(context))
            if not await asyncio.to_thread(self._finish, operation_id, result_ref, context.input_fingerprint):
                raise OperationNeedsReview("Operation status changed; review is required")
            return result_ref
        except BaseException as exc:
            # The operation might already have produced a side effect. Failure to
            # reach DB leaves it running; recovery then turns it uncertain.
            try:
                await asyncio.shield(asyncio.to_thread(self._uncertain, operation_id))
            except Exception:
                pass
            if not isinstance(exc, Exception):
                raise
            raise OperationNeedsReview("Operation result is unknown; review is required") from None


def build_graph(checkpointer, ledger: OperationLedger, execute: Execute, should_pause: ShouldPause):
    if checkpointer is None:
        raise ValueError("A checkpointer is required")

    async def boundary(state: GraphState):
        return {"pause_required": bool(await should_pause(_context(state)))}

    async def operation(state: GraphState):
        context = _context(state)
        if state.get("pause_required"):
            answer = interrupt({"type": "pause", "run_id": context.run_id, "node_id": context.node_id})
            if answer != "continue":
                raise GraphNotResumable("Invalid resume signal")
        return {"result_ref": await ledger.execute_once(context, execute)}

    builder = StateGraph(GraphState)
    builder.add_node("boundary", boundary)
    builder.add_node("operation", operation)
    builder.add_edge(START, "boundary")
    builder.add_edge("boundary", "operation")
    builder.add_edge("operation", END)
    return builder.compile(checkpointer=checkpointer, name="omniharness-m3-operation")


def _config(run_id: str):
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
        raise ValueError("Invalid run identity")
    return {"configurable": {"thread_id": run_id}}


async def start(graph, context: OperationContext):
    _validate_context(context)
    config = _config(context.run_id)
    if (await graph.aget_state(config)).values:
        raise GraphAlreadyExists("This Run already has a checkpoint")
    state: GraphState = {
        "run_id": context.run_id, "node_id": context.node_id, "invocation_id": context.invocation_id,
        "workflow_version": context.workflow_version, "input_fingerprint": context.input_fingerprint,
    }
    return await graph.ainvoke(state, config, durability="sync")


async def resume(graph, run_id: str):
    config = _config(run_id)
    snapshot = await graph.aget_state(config)
    if not snapshot.values or not snapshot.next or not snapshot.interrupts:
        raise GraphNotResumable("This Run is not waiting for a resumable interrupt")
    context = _context(snapshot.values)
    if context.run_id != run_id:
        raise GraphNotResumable("Checkpoint identity does not match Run")
    return await graph.ainvoke(Command(resume="continue"), config, durability="sync")


@asynccontextmanager
async def postgres_checkpointer(database_url: str, *, initialize: bool = False):
    """Open official async PostgreSQL saver. Initialization must be explicit.

    On Windows, the caller must configure SelectorEventLoopPolicy before running
    the event loop. The URL and its password must never be logged.
    """
    url = make_url(database_url)
    if url.drivername not in {"postgresql+psycopg", "postgresql"}:
        raise ValueError("A PostgreSQL connection is required")
    conn_string = url.set(drivername="postgresql").render_as_string(hide_password=False)
    async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
        if initialize:
            await saver.setup()
        yield saver
