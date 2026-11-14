"""Controlled Deep Agents building block; not wired to the product Worker.

The caller owns durable control, atomic budgets, checkpoints and fencing. This
module does not persist requests or credentials and is not a security sandbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphBubbleUp

from .m3_delegation import HierarchicalDelegation


class AgentBoundaryError(RuntimeError):
    """Safe, static public error raised before an invocation is dispatched."""


class AgentBudgetExceeded(AgentBoundaryError):
    pass


class AgentContextLimitExceeded(AgentBoundaryError):
    """A model request exceeds the configured local context boundary."""


class AgentToolDenied(AgentBoundaryError):
    pass


class AgentExternalCallFailed(AgentBoundaryError):
    pass


def _raise_sanitized(exc: BaseException):
    """Never let provider, tool or database error text reach a checkpoint."""
    if isinstance(exc, (GraphBubbleUp, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        raise exc
    from .services import ServiceError
    from .worker import WorkerOwnershipLost

    if isinstance(exc, WorkerOwnershipLost):
        raise WorkerOwnershipLost("Worker execution ownership was lost") from None
    if isinstance(exc, ServiceError):
        safe_messages = {"HISTORY_SCOPE_INVALID": "历史范围中的项目不可用或范围无效。",
                         "HISTORY_SOURCE_INVALID": "所选历史来源已不可用。"}
        if exc.code in safe_messages:
            raise ServiceError(exc.code, safe_messages[exc.code], 409) from None
    if isinstance(exc, AgentBoundaryError):
        raise type(exc)("Agent execution stopped at a protected boundary") from None
    raise AgentExternalCallFailed("Agent model, tool or control boundary failed safely") from None


class BoundaryHooks(Protocol):
    """Per-Run host boundary. Implementations must share durable atomic limits.

    Do not include prompts, tool arguments, credentials, or provider responses in
    these callbacks. ``check_control`` may raise to abort at a safe boundary.
    The caller must enforce execution ownership and avoid replay of side effects.
    """

    async def check_control(self, kind: str, role: str, tool_name: str | None) -> None: ...

    async def reserve(self, kind: str, role: str, tool_name: str | None) -> bool: ...


@dataclass(frozen=True)
class InvocationIdentity:
    """Opaque stable identity and binding; no raw input or credentials."""

    thread_id: str
    invocation_id: str
    binding_fingerprint: str
    command_ids: tuple[str, ...] = ()
    checkpoint_ns: str = ""
    task_id: str = ""
    delegated_role: str | None = None
    purpose: str = "task"


class DurableBoundaryHooks(BoundaryHooks, Protocol):
    async def reserve_invocation(self, kind: str, role: str, tool_name: str | None,
                                 identity: InvocationIdentity) -> bool: ...


def _hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise AgentBoundaryError("Invocation input cannot be bound safely") from None
    return hashlib.sha256(encoded).hexdigest()


def validate_model_context(request, max_messages: int, max_characters: int) -> None:
    """Bound the actual serialized model input without storing or logging it.

    This is a conservative character bound, not a model token calculation.
    Include structured content, tool calls, metadata, and system instructions;
    the serialized JSON overhead deliberately counts against the limit.
    """
    try:
        messages = list(request.messages)
        system = request.system_message
        if len(messages) + (1 if system is not None else 0) > max_messages:
            raise AgentContextLimitExceeded("Model input exceeds the local context limit")
        serialized = [message.model_dump(mode="json") for message in messages]
        if system is not None:
            serialized.append(system.model_dump(mode="json"))
        text = json.dumps(serialized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(text) > max_characters:
            raise AgentContextLimitExceeded("Model input exceeds the local context limit")
    except AgentContextLimitExceeded:
        raise
    except (AttributeError, TypeError, ValueError, UnicodeError):
        raise AgentBoundaryError("Model input cannot be checked safely") from None


def invocation_identity(request, *, kind: str, role: str, tool_name: str | None) -> InvocationIdentity:
    """Bind a graph task/tool call to its input; missing persistence IDs fail closed."""
    runtime = getattr(request, "runtime", None)
    info = getattr(runtime, "execution_info", None)
    namespace = getattr(info, "checkpoint_ns", None)
    task_id = getattr(info, "task_id", None)
    thread_id = getattr(info, "thread_id", None)
    if not isinstance(namespace, str) or not isinstance(task_id, str) or not task_id:
        raise AgentBoundaryError("Durable graph invocation identity is unavailable")
    if not isinstance(thread_id, str) or not thread_id:
        raise AgentBoundaryError("Durable graph thread identity is unavailable")
    call = getattr(request, "tool_call", None)
    if kind == "tool_calls":
        if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
            raise AgentBoundaryError("Durable tool invocation identity is unavailable")
        identity = {"namespace": namespace, "task_id": task_id, "tool_call_id": call["id"]}
        binding = {"role": role, "kind": kind, "tool_name": tool_name, "args": call.get("args")}
    elif kind == "model_calls":
        identity = {"namespace": namespace, "task_id": task_id, "model": True}
        try:
            messages = [message.model_dump(mode="json") for message in request.messages]
            system = request.system_message.model_dump(mode="json") if request.system_message else None
        except (AttributeError, TypeError, ValueError):
            raise AgentBoundaryError("Model invocation cannot be bound safely") from None
        binding = {"role": role, "kind": kind, "messages": messages, "system": system}
    else:
        raise AgentBoundaryError("Unsupported durable invocation kind")
    command_ids = ()
    if kind == "model_calls":
        command_ids = tuple(message.id[len("omni-command:"):]
                            for message in request.messages
                            if isinstance(message, HumanMessage) and isinstance(message.id, str)
                            and message.id.startswith("omni-command:"))
    return InvocationIdentity(thread_id=thread_id, invocation_id=_hash(identity),
                              binding_fingerprint=_hash(binding), command_ids=command_ids,
                              checkpoint_ns=namespace, task_id=task_id,
                              delegated_role=(call.get("args") or {}).get("subagent_type")
                              if kind == "tool_calls" and tool_name == "task" else None)


class NoImplicitSummarization(AgentMiddleware):
    """Replace SDK summarization, which can call a model outside model hooks.

    Until a budgeted summarizer is integrated the host must impose explicit
    context limits; the SDK's automatic compaction is intentionally disabled.
    """

    @property
    def name(self) -> str:
        return "SummarizationMiddleware"


@dataclass(frozen=True)
class AgentRole:
    name: str
    description: str
    model: BaseChatModel
    system_prompt: str = ""
    tools: tuple[BaseTool, ...] = ()


class SharedBoundaryMiddleware(AgentMiddleware):
    """Control every model, explicit tool and parent delegation boundary."""

    def __init__(self, *, hooks: BoundaryHooks, role: str, allowed_tools: frozenset[str],
                 allowed_agents: frozenset[str], semaphore: asyncio.Semaphore, parent: bool,
                 require_durable_ids: bool = False, scheduler: HierarchicalDelegation | None = None,
                 max_context_messages: int = 100, max_context_characters: int = 200_000):
        self.hooks = hooks
        self.role = role
        self.allowed_tools = allowed_tools
        self.allowed_agents = allowed_agents
        self.semaphore = semaphore
        self.parent = parent
        self.require_durable_ids = require_durable_ids
        self.scheduler = scheduler
        self.max_context_messages = max_context_messages
        self.max_context_characters = max_context_characters

    async def _reserve(self, kind: str, tool_name: str | None = None, request=None):
        try:
            await self.hooks.check_control(kind, self.role, tool_name)
            if kind == "model_calls":
                validate_model_context(request, self.max_context_messages, self.max_context_characters)
            if self.require_durable_ids:
                identity = invocation_identity(request, kind=kind, role=self.role, tool_name=tool_name)
                try:
                    allowed = await self.hooks.reserve_invocation(kind, self.role, tool_name, identity)
                except AgentBoundaryError:
                    # A pause can arrive after the first check but before the
                    # reservation. Recheck outside the DB transaction so a
                    # real LangGraph interrupt can be checkpointed.
                    await self.hooks.check_control(kind, self.role, tool_name)
                    raise
            else:
                allowed = await self.hooks.reserve(kind, self.role, tool_name)
        except BaseException as exc:
            _raise_sanitized(exc)
        if not allowed:
            raise AgentBudgetExceeded("Run budget exceeded before dispatch")
        return identity if self.require_durable_ids else None

    async def abefore_model(self, state, runtime):
        del runtime
        try:
            if self.require_durable_ids and callable(getattr(self.hooks, "stage_requirements", None)):
                await self.hooks.check_control("model_calls", self.role, None)
                messages = await self.hooks.stage_requirements(self.role, state.get("messages", []))
                if messages:
                    return {"messages": messages}
        except BaseException as exc:
            _raise_sanitized(exc)
        return None

    async def _dispatch(self, request, handler, identity, kind, tool_name):
        # A task invocation re-enters a LangGraph child; it has no direct
        # external effect and must propagate interrupts to the child graph.
        tracked = identity is not None and tool_name != "task"
        try:
            if self.scheduler is not None and tool_name != "task":
                result = await self.scheduler.run_tracked(lambda: handler(request))
            else:
                result = await handler(request)
        except BaseException as exc:
            if tracked and callable(getattr(self.hooks, "mark_invocation_uncertain", None)):
                try:
                    await self.hooks.mark_invocation_uncertain(kind, self.role, tool_name, identity)
                except BaseException:
                    # Preserve GraphInterrupt/CancelledError; a lost owner cannot
                    # write and the next owner must reconcile the running record.
                    pass
            _raise_sanitized(exc)
        if tracked and callable(getattr(self.hooks, "finish_invocation", None)):
            try:
                await self.hooks.finish_invocation(kind, self.role, tool_name, identity)
            except BaseException as exc:
                if callable(getattr(self.hooks, "mark_invocation_uncertain", None)):
                    try:
                        await self.hooks.mark_invocation_uncertain(kind, self.role, tool_name, identity)
                    except BaseException:
                        # Completion may already be committed, or ownership may
                        # be lost; preserve the original cancellation/control.
                        pass
                _raise_sanitized(exc)
        return result

    async def awrap_model_call(self, request, handler):
        if self.scheduler is not None:
            await self.scheduler.ensure_active()
        identity = await self._reserve("model_calls", request=request)
        return await self._dispatch(request, handler, identity, "model_calls", None)

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        name = call.get("name")
        # Do not include raw model-generated input in exceptions or hooks.
        if name == "task":
            try:
                await self.hooks.check_control("delegation", self.role, "task")
            except BaseException as exc:
                _raise_sanitized(exc)
            subagent = (call.get("args") or {}).get("subagent_type")
            if not self.parent or subagent not in self.allowed_agents:
                raise AgentToolDenied("Delegation is not authorized")
            async def delegate():
                # A queued task must not launch after the host requests pause.
                identity = await self._reserve("tool_calls", "task", request)
                return await self._dispatch(request, handler, identity, "tool_calls", "task")
            if self.scheduler is not None:
                async def tracked_delegate():
                    async with self.scheduler.delegate():
                        return await delegate()
                return await self.scheduler.run_tracked(tracked_delegate)
            async with self.semaphore:
                return await delegate()
        if not isinstance(name, str) or name not in self.allowed_tools:
            # The generated name itself is untrusted and may contain secrets.
            try:
                await self.hooks.check_control("tool_calls", self.role, None)
            except BaseException as exc:
                _raise_sanitized(exc)
            raise AgentToolDenied("Tool is not authorized")
        if self.scheduler is not None:
            await self.scheduler.ensure_active()
        identity = await self._reserve("tool_calls", name, request)
        return await self._dispatch(request, handler, identity, "tool_calls", name)


def _tool_names(tools: Sequence[BaseTool]) -> frozenset[str]:
    names: set[str] = set()
    for item in tools:
        if not isinstance(item, BaseTool) or not item.name or item.name == "task":
            raise ValueError("Tools must be explicit trusted BaseTool instances and cannot be task")
        if item.name in names:
            raise ValueError("Duplicate tool name")
        names.add(item.name)
    return frozenset(names)


def build_controlled_agent(*, coordinator: AgentRole, subagents: Sequence[AgentRole],
                           hooks: BoundaryHooks, checkpointer: Any = None, require_durable_ids: bool = False,
                           scheduler: HierarchicalDelegation | None = None, nested_delegation: bool = False,
                           max_context_messages: int = 100, max_context_characters: int = 200_000):
    """Build a Deep Agent with shared host hooks and max two concurrent delegates.

    The supplied model is explicit (never SDK ambient defaults). By default
    subagents cannot delegate; opt-in nested graphs have one further level.
    Callers must disable cloud tracing, choose a safe checkpointer, and ensure
    the model has tool-calling support.
    """
    if require_durable_ids and (checkpointer is None or not callable(getattr(hooks, "reserve_invocation", None))):
        raise ValueError("Durable identity requires a checkpointer and compatible host hooks")
    if nested_delegation and (scheduler is None or scheduler.max_depth < 2):
        raise ValueError("Nested delegation requires a depth-two scheduler")
    if (isinstance(max_context_messages, bool) or not isinstance(max_context_messages, int)
            or max_context_messages < 1 or isinstance(max_context_characters, bool)
            or not isinstance(max_context_characters, int) or max_context_characters < 1):
        raise ValueError("Context limits must be positive integers")
    if not isinstance(coordinator.model, BaseChatModel):
        raise ValueError("An explicit coordinator model is required")
    if len({r.name for r in subagents}) != len(subagents):
        raise ValueError("Duplicate subagent name")
    if not coordinator.name or any(not r.name or r.name == coordinator.name for r in subagents):
        raise ValueError("Invalid agent role name")
    if any(not isinstance(r.model, BaseChatModel) for r in subagents):
        raise ValueError("Every subagent requires an explicit model")
    allowed_agents = frozenset(role.name for role in subagents)
    semaphore = asyncio.Semaphore(2)
    main_middleware = SharedBoundaryMiddleware(
        hooks=hooks, role=coordinator.name, allowed_tools=_tool_names(coordinator.tools),
        allowed_agents=allowed_agents, semaphore=semaphore, parent=True, require_durable_ids=require_durable_ids,
        scheduler=scheduler, max_context_messages=max_context_messages,
        max_context_characters=max_context_characters,
    )
    child_specs = [
        {
            "name": role.name,
            "description": role.description,
            "model": role.model,
            "system_prompt": role.system_prompt,
            "tools": list(role.tools),
            "middleware": [NoImplicitSummarization(), SharedBoundaryMiddleware(
                hooks=hooks, role=role.name, allowed_tools=_tool_names(role.tools),
                allowed_agents=frozenset(), semaphore=semaphore, parent=False, require_durable_ids=require_durable_ids,
                scheduler=scheduler, max_context_messages=max_context_messages,
                max_context_characters=max_context_characters,
            )],
        }
        for role in subagents
    ]
    if nested_delegation:
        # Compile each first-level child as a graph with its own explicitly
        # configured leaf roles. Leaf middleware never permits further tasks.
        compiled_children = []
        for role in subagents:
            child_middleware = SharedBoundaryMiddleware(
                hooks=hooks, role=role.name, allowed_tools=_tool_names(role.tools),
                allowed_agents=allowed_agents, semaphore=semaphore, parent=True,
                require_durable_ids=require_durable_ids, scheduler=scheduler,
                max_context_messages=max_context_messages, max_context_characters=max_context_characters,
            )
            child = create_deep_agent(
                model=role.model, tools=list(role.tools), system_prompt=role.system_prompt,
                middleware=[NoImplicitSummarization(), child_middleware], subagents=child_specs, name=role.name,
            )
            compiled_children.append({"name": role.name, "description": role.description, "runnable": child})
        child_specs = compiled_children
    return create_deep_agent(
        model=coordinator.model, tools=list(coordinator.tools), system_prompt=coordinator.system_prompt,
        middleware=[NoImplicitSummarization(), main_middleware], subagents=child_specs, checkpointer=checkpointer,
        name=coordinator.name,
    )
