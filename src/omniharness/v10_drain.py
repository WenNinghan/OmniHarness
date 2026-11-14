"""Host-local candidate for a drained v10 execution; it grants no dispatch.

The runtime must create this only at its own graph completion boundary. It is
discarded on process/owner change and cannot reconstruct authority from DB.
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
import weakref
from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph

from .checkpoint_fencing import SEALED_V10_FENCE_VERSION, FenceTicket
from .m3_delegation import HierarchicalDelegation
from .v10_sealed_saver import V10SealedSaver


class DrainUnavailable(RuntimeError):
    """The live host cannot establish a completed execution boundary."""


_assemblies = {}
_executors = {}
_assemblies_lock = threading.Lock()


def register_host_executor(executor, *, host, gateway, middleware, scheduler):
    """Bind the executor produced by the workspace gateway factory."""
    from .m4_runtime import ExecutionBoundary
    from .m4_tools import WorkspaceToolGateway

    if (not isinstance(executor, CompiledStateGraph) or not isinstance(gateway, WorkspaceToolGateway)
            or not isinstance(middleware, ExecutionBoundary) or gateway.host is not host
            or gateway.scheduler is not scheduler or middleware.host is not host or middleware.gateway is not gateway
            or middleware.scheduler is not scheduler or getattr(host, "scheduler", None) is not scheduler):
        raise DrainUnavailable("Host execution gateway assembly is invalid")
    identity = id(executor)

    def forget(reference):
        with _assemblies_lock:
            record = _executors.get(identity)
            if record and record[0] is reference:
                _executors.pop(identity, None)

    record = (weakref.ref(executor, forget), weakref.ref(host), weakref.ref(gateway),
              weakref.ref(middleware), weakref.ref(scheduler), weakref.ref(middleware.model))
    with _assemblies_lock:
        if identity in _executors or len(_executors) >= 10000:
            raise DrainUnavailable("Execution gateway assembly is already registered or capacity is exhausted")
        _executors[identity] = record


def _executor_assembled(executor, host, scheduler):
    with _assemblies_lock:
        record = _executors.get(id(executor))
        if not record:
            return False
        expected, expected_host, gateway, middleware, expected_scheduler, model = (ref() for ref in record)
        return bool(expected is executor and expected_host is host and expected_scheduler is scheduler
                    and gateway is not None and middleware is not None and model is not None and gateway.host is host
                    and gateway.scheduler is scheduler and middleware.host is host
                    and middleware.gateway is gateway and middleware.scheduler is scheduler and middleware.model is model)


def register_host_graph(graph, *, host, executor, saver):
    """Remember the exact objects assembled by the host's v10 graph builder."""
    scheduler = getattr(host, "scheduler", None)
    if (not isinstance(graph, CompiledStateGraph) or graph.checkpointer is not saver
            or not isinstance(executor, CompiledStateGraph) or not isinstance(saver, V10SealedSaver)
            or not isinstance(scheduler, HierarchicalDelegation)
            or not _executor_assembled(executor, host, scheduler)
            or getattr(host, "workflow_version", None) != "stage-agent-v10"
            or getattr(host, "run_id", None) != saver.ticket.thread_id):
        raise DrainUnavailable("Host graph assembly is invalid")
    identity = id(graph)

    def forget(reference):
        with _assemblies_lock:
            record = _assemblies.get(identity)
            if record and record[0] is reference:
                _assemblies.pop(identity, None)

    record = (weakref.ref(graph, forget), weakref.ref(host), weakref.ref(executor),
              weakref.ref(saver), weakref.ref(scheduler), getattr(host, "_active_segment_started", None))
    with _assemblies_lock:
        if identity in _assemblies or len(_assemblies) >= 10000:
            raise DrainUnavailable("Host graph assembly is already registered or capacity is exhausted")
        _assemblies[identity] = record


def _assembled(graph, worker, ticket, executor, saver, scheduler):
    with _assemblies_lock:
        record = _assemblies.get(id(graph))
        if not record:
            return False
        expected_graph, host, expected_executor, expected_saver, expected_scheduler = (ref() for ref in record[:5])
        segment_started = record[5]
        basic = bool(expected_graph is graph and host is not None and getattr(host, "worker", None) is worker
            and getattr(host, "run_id", None) == ticket.thread_id and getattr(host, "scheduler", None) is scheduler
            and getattr(host, "_active_segment_started", None) == segment_started
            and expected_executor is executor and expected_saver is saver and expected_scheduler is scheduler)
    return basic and _executor_assembled(executor, host, scheduler)


@dataclass(frozen=True)
class DrainedExecution:
    owner: object
    owner_connection: object
    ticket: FenceTicket
    graph: CompiledStateGraph
    executor: CompiledStateGraph
    saver: V10SealedSaver
    scheduler: HierarchicalDelegation
    graph_task: asyncio.Task
    host: object
    segment_started: float


def _assembly_host_and_start(graph):
    with _assemblies_lock:
        record = _assemblies.get(id(graph))
        if not record or record[0]() is not graph:
            raise DrainUnavailable("Host graph assembly is unavailable")
        host, started = record[1](), record[5]
    if (host is None or isinstance(started, bool) or not isinstance(started, (int, float))
            or not math.isfinite(started) or started <= 0 or started > time.monotonic()
            or getattr(host, "_active_segment_started", None) != started):
        raise DrainUnavailable("Active segment start is unavailable")
    return host, float(started)


def start_host_graph(worker, *, ticket, graph, executor, saver, scheduler, payload, config):
    """Launch the exact host graph whose later completion may be inspected."""
    owner = getattr(worker, "lock", None)
    if (owner is None or owner.lost or owner.connection is None
            or not isinstance(ticket, FenceTicket) or ticket.workflow_version != SEALED_V10_FENCE_VERSION
            or not isinstance(saver, V10SealedSaver) or saver.ticket != ticket
            or not isinstance(graph, CompiledStateGraph) or graph.checkpointer is not saver
            or not isinstance(executor, CompiledStateGraph) or not isinstance(scheduler, HierarchicalDelegation)
            or scheduler.closed or not isinstance(config, dict)
            or config.get("configurable", {}).get("thread_id") != ticket.thread_id
            or not _assembled(graph, worker, ticket, executor, saver, scheduler)):
        raise DrainUnavailable("Host graph launch binding is unavailable")
    _assembly_host_and_start(graph)
    running = getattr(worker, "_v10_running_executions", None)
    if running is None:
        running = worker._v10_running_executions = {}
    if (ticket.thread_id in running or ticket.thread_id in getattr(worker, "_v10_drained_executions", {})
            or ticket.thread_id in getattr(worker, "_v10_drain_tainted", set())
            or len(running) >= 10000):
        raise DrainUnavailable("Host graph execution was already registered")
    task = asyncio.create_task(graph.ainvoke(payload, config, durability="sync"))
    running[ticket.thread_id] = (owner, owner.connection, ticket, graph, executor, saver, scheduler, task)
    return task


def _scheduler_drained(scheduler):
    if (not isinstance(scheduler, HierarchicalDelegation) or scheduler.closed is not True
            or scheduler.active_count != 0 or scheduler._tasks or scheduler._operations
            or scheduler._loop is not asyncio.get_running_loop()):
        return False
    drain = scheduler._drain_task
    return bool(drain and drain.done() and not drain.cancelled() and drain.exception() is None)


async def capture_drained_execution(worker, *, ticket, graph, executor, saver, scheduler, graph_task):
    """Freeze a host-built saver after the real graph and scheduler have ended.

    This is a candidate building block, not closure or a model reservation.
    The caller must retain graph/task identity from the host execution path.
    """
    owner = getattr(worker, "lock", None)
    registered = (getattr(worker, "_v10_running_executions", {}).get(ticket.thread_id)
                  if isinstance(ticket, FenceTicket) else None)
    matching = (isinstance(registered, tuple) and len(registered) == 8 and registered[0] is owner
                and registered[1] is getattr(owner, "connection", None) and registered[2] == ticket
                and registered[3] is graph and registered[4] is executor and registered[5] is saver
                and registered[6] is scheduler and registered[7] is graph_task)
    if (owner is None or owner.lost or owner.connection is None
            or not isinstance(ticket, FenceTicket) or ticket.workflow_version != SEALED_V10_FENCE_VERSION
            or not isinstance(saver, V10SealedSaver) or saver.ticket != ticket
            or not isinstance(graph, CompiledStateGraph) or graph.checkpointer is not saver
            or not isinstance(executor, CompiledStateGraph) or not isinstance(graph_task, asyncio.Task)
            or graph_task.get_loop() is not asyncio.get_running_loop() or not graph_task.done()
            or graph_task.cancelled() or graph_task.exception() is not None or not _scheduler_drained(scheduler)
            or not matching or not _assembled(graph, worker, ticket, executor, saver, scheduler)):
        raise DrainUnavailable("Host graph, scheduler or owner is not drained")
    result = graph_task.result()
    if not isinstance(result, dict) or result.get("__interrupt__"):
        raise DrainUnavailable("Graph did not complete normally")
    # The saver barrier must drain preprocessing as well as DB writes; no
    # checkpoint writer may be reopened from this closed generation.
    try:
        await saver.close_writes()
        saver.assert_write_drain(ticket)
        if owner.lost or owner.connection is None or not _scheduler_drained(scheduler):
            raise DrainUnavailable("Execution owner or scheduler changed while draining")
        host, segment_started = _assembly_host_and_start(graph)
        proof = DrainedExecution(owner, owner.connection, ticket, graph, executor, saver, scheduler, graph_task,
                                 host, segment_started)
        registry = getattr(worker, "_v10_drained_executions", None)
        if registry is None:
            registry = worker._v10_drained_executions = {}
        if ticket.thread_id in registry or len(registry) >= 10000:
            raise DrainUnavailable("Execution drain was already recorded or capacity is exhausted")
        registry[ticket.thread_id] = proof
        del worker._v10_running_executions[ticket.thread_id]
    except BaseException:
        getattr(worker, "_v10_running_executions", {}).pop(ticket.thread_id, None)
        tainted = getattr(worker, "_v10_drain_tainted", None)
        if tainted is None:
            tainted = worker._v10_drain_tainted = set()
        tainted.add(ticket.thread_id)
        raise
    return proof


def get_drained_execution(worker, run_id, *, ticket, graph, executor, saver):
    """Recheck the same live in-process objects; return no dispatch authority."""
    proof = getattr(worker, "_v10_drained_executions", {}).get(run_id)
    owner = getattr(worker, "lock", None)
    if (not isinstance(proof, DrainedExecution) or owner is not proof.owner or owner.lost
            or owner.connection is not proof.owner_connection or ticket != proof.ticket
            or graph is not proof.graph or executor is not proof.executor or saver is not proof.saver
            or not saver._writes_closed or not _scheduler_drained(proof.scheduler)
            or not proof.graph_task.done() or proof.graph_task.cancelled()
            or _assembly_host_and_start(graph) != (proof.host, proof.segment_started)
            or not _assembled(graph, worker, ticket, executor, saver, proof.scheduler)):
        raise DrainUnavailable("Host drain is missing or changed")
    try:
        saver.assert_write_drain(ticket)
    except Exception:
        raise DrainUnavailable("Checkpoint writer drain changed") from None
    return proof


def get_drained_model(worker, run_id, *, ticket, graph, executor, saver):
    """Return only the model fixed by the recorded workspace executor factory."""
    get_drained_execution(worker, run_id, ticket=ticket, graph=graph, executor=executor, saver=saver)
    with _assemblies_lock:
        record = _executors.get(id(executor))
        model = record[5]() if record else None
    if model is None:
        raise DrainUnavailable("The frozen execution model identity is unavailable")
    return model


def abandon_host_graph(worker, run_id):
    """A stopped, interrupted or uncertain execution cannot regain drain authority."""
    getattr(worker, "_v10_running_executions", {}).pop(run_id, None)
    getattr(worker, "_v10_drained_executions", {}).pop(run_id, None)
    tainted = getattr(worker, "_v10_drain_tainted", None)
    if tainted is None:
        tainted = worker._v10_drain_tainted = set()
    tainted.add(run_id)
