"""M4 host integration around the installed LangChain agent graph."""
from __future__ import annotations

import json

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, RemoveMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import interrupt
from sqlalchemy import select

from .m3_agents import (
    AgentBoundaryError,
    AgentBudgetExceeded,
    AgentContextLimitExceeded,
    _raise_sanitized,
    invocation_identity,
    validate_model_context,
)
from .m3_host import FencedAgentHost
from .m4_contract import COLLAB_WORKFLOWS, workspace_prompts
from .m4_tools import WorkspaceToolGateway, tool_definitions
from .model_output import require_complete_output
from .models import Approval, Message, Run, RunCommand, ToolOperation
from .services import add_event, apply_commands


class M4AgentHost(FencedAgentHost):
    dispatch_blocked = False
    scheduler = None

    async def check_control(self, kind, role, tool_name):
        async with self.serial:
            with self.worker.transaction() as db:
                apply_commands(db, self.run_id, defer_requirements=True)
                run, _ = self._run(db, role, allow_supplements=True, allow_control=True)
                pausing = run.status == "pause_requested"
        if pausing and self.scheduler is not None:
            await self.scheduler.wait_operations()
        await super().check_control(kind, role, tool_name)
        if self.dispatch_blocked:
            async with self.serial:
                with self.worker.transaction() as db:
                    from .models import Clarification
                    pending = db.scalar(select(Approval.id).join(ToolOperation).where(
                        ToolOperation.run_id == self.run_id, Approval.status == "pending").limit(1))
                    pending_user = db.scalar(select(Clarification.id).where(
                        Clarification.run_id == self.run_id, Clarification.status == "pending").limit(1))
            if pending is not None:
                if self.scheduler is not None:
                    await self.scheduler.wait_operations()
                signal = interrupt({"type": "approval_barrier", "run_id": self.run_id})
                if signal != "continue":
                    raise AgentBoundaryError("Invalid approval barrier resume")
                await super().check_control(kind, role, tool_name)
            if pending_user is not None:
                if self.scheduler is not None:
                    await self.scheduler.wait_operations()
                signal = interrupt({"type": "user_barrier", "run_id": self.run_id})
                if signal != "continue":
                    raise AgentBoundaryError("Invalid clarification barrier resume")
                await super().check_control(kind, role, tool_name)
            self.dispatch_blocked = False


class ExecutionBoundary(AgentMiddleware):
    """Every native model/tool call crosses one shared, owner-fenced host."""

    def __init__(self, *, host, gateway, scheduler, max_messages=100, max_characters=200_000):
        self.host, self.gateway, self.scheduler = host, gateway, scheduler
        self.max_messages, self.max_characters = max_messages, max_characters
        self.model = None
        self.prompt = workspace_prompts(host.workflow_version)["execute"]
        if host.workflow_version in {"stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
            self.prompt += (" 如确有必须用户回答才能继续的问题，调用 ask_user 工具等待回复；"
                            "不要以普通文本假装用户已回答。不要无谓地询问可合理推断的信息。")

    async def abefore_model(self, state, runtime):
        from .context_host import prepare_agent_context
        from .context_policy import parse_context_configuration
        await self.host.check_control("model_calls", "execute", None)
        additions = await self.host.stage_requirements("execute", state.get("messages", []))
        messages = [*state.get("messages", []), *additions]
        info = runtime.execution_info
        prepared = await prepare_agent_context(self.host, "execute", messages, model=self.model,
            system_message=SystemMessage(content=self.prompt),
                    tool_schemas=tuple(convert_to_openai_tool(tool) for tool in tool_definitions(
                        self.gateway.workspace, interactive=self.host.workflow_version in {"stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10"},
                        patch_schema_v2=self.host.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"})),
            checkpoint_ns=info.checkpoint_ns, task_id=info.task_id)
        # Provider-native tool-call signatures belong to this same execution
        # Agent. Keep surviving pairs valid; other stages entered as public text.
        native = {message.id: message for message in messages if isinstance(message, AIMessage) and message.tool_calls}
        effective = [native.get(message.id, message) for message in prepared.messages]
        async with self.host.serial:
            with self.host.worker.transaction() as db:
                _, role = self.host._run(db, "execute", allow_supplements=True)
                policy, _ = parse_context_configuration(role["model"].get("context_policy"))
                self.context_snapshot = role["model"]
        self.max_messages, self.max_characters = policy.max_messages, policy.max_characters
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *effective]}

    async def awrap_model_call(self, request, handler):
        from .context_policy import context_budget, counter_for_policy, parse_context_configuration
        await self.host.check_control("model_calls", "execute", None)
        # A restart may resume the model node after before_model was already
        # checkpointed. Read the frozen configuration instead of relying on a
        # transient middleware instance field set by that earlier node.
        async with self.host.serial:
            with self.host.worker.transaction() as db:
                _, role = self.host._run(db, "execute", allow_supplements=True)
                snapshot = role["model"]
        policy, limits = parse_context_configuration(snapshot.get("context_policy"))
        validate_model_context(request, policy.max_messages, policy.max_characters)
        tools = tuple(convert_to_openai_tool(tool) for tool in request.tools)
        messages = ([request.system_message] if request.system_message else []) + list(request.messages)
        serialized = json.dumps([message.model_dump(mode="json") for message in messages], ensure_ascii=False)
        budget = context_budget(policy, limits, output_tokens=snapshot["max_output_tokens"])
        if (len(serialized) + len(json.dumps(tools, ensure_ascii=False)) > policy.max_characters
                or counter_for_policy(policy).count(messages, tools=tools) > budget.input_limit):
            raise AgentContextLimitExceeded("Actual native messages and tools exceed the frozen input budget")
        identity = invocation_identity(request, kind="model_calls", role="execute", tool_name=None)
        saved = await self.host.replay_invocation("model_calls", "execute", None, identity)
        if saved is not None:
            messages = [AIMessage.model_validate(item) for item in saved["messages"]]
            for message in messages:
                require_complete_output(message, allow_tools=True)
            return ModelResponse(result=messages)
        from .context_visibility import model_input_callback
        on_reserved = model_input_callback(self.host, "execute", identity, request.messages,
                                           system_present=request.system_message is not None,
                                           tool_count=len(tools), system_message=request.system_message,
                                           tool_schemas=tools)
        if self.host.workflow_version == "stage-agent-v10":
            from .m4_collaboration import current_task_scope
            try:
                logical_task = current_task_scope()["id"]
            except (KeyError, ValueError, RuntimeError):
                raise AgentBoundaryError("V10 execution task scope is unavailable") from None
            if not identity.checkpoint_ns or not identity.task_id or not logical_task:
                raise AgentBoundaryError("V10 execution checkpoint identity is unavailable")
            visibility_callback = on_reserved

            def on_reserved(db, operation):
                visibility_callback(db, operation)
                operation.result = {**(operation.result or {}), "v10_model_scope": {
                    "run_id": self.host.run_id, "task_id": logical_task,
                    "checkpoint_ns": identity.checkpoint_ns, "graph_task_id": identity.task_id,
                    "invocation_id": identity.invocation_id,
                }}
        if not await self.host.reserve_invocation("model_calls", "execute", None, identity,
                                                  on_reserved=on_reserved):
            raise AgentBudgetExceeded("Run model budget exhausted")

        async def execute_and_save():
            try:
                response = await handler(request)
                if (not isinstance(response, ModelResponse) or len(response.result) != 1
                        or not isinstance(response.result[0], AIMessage)):
                    raise AgentBoundaryError("Unexpected execution model response")
                if self.host.workflow_version in {"stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}:
                    calls = response.result[0].tool_calls
                    if any(call.get("name") == "ask_user" for call in calls) and len(calls) != 1:
                        # A question cannot race another tool invocation in
                        # the same model response. Interactive task dispatch is
                        # also serialized by the parent stage graph.
                        raise AgentBoundaryError("Clarification must be the sole tool call")
                evidence = {"messages": [message.model_dump(mode="json") for message in response.result]}
                await self.host.finish_invocation("model_calls", "execute", None, identity, result=evidence)
            except BaseException as exc:
                try:
                    diagnose = getattr(self.host, "mark_invocation_uncertain_with_diagnostics", None)
                    if callable(diagnose):
                        model = request.model
                        transport = ("compatible-native-stream-v1" if
                                     getattr(model, "omni_normalize_stream_usage", False)
                                     and getattr(model, "streaming", False) else "ainvoke_default")
                        await diagnose("model_calls", "execute", None, identity,
                                       error=exc, transport_mode=transport)
                    else:
                        await self.host.mark_invocation_uncertain("model_calls", "execute", None, identity)
                except BaseException:
                    pass
                _raise_sanitized(exc)
            require_complete_output(response.result[0], allow_tools=True)
            return response
        return await self.scheduler.run_tracked(execute_and_save)

    async def awrap_tool_call(self, request, handler):
        del handler  # A model never reaches the unguarded placeholder function.
        try:
            return await self.gateway.invoke(request)
        except BaseException as exc:
            _raise_sanitized(exc)


def build_execution_agent(*, model, host, workspace, broker, sandbox, artifacts, scheduler,
                          max_context_messages=100, max_context_characters=200_000):
    gateway = WorkspaceToolGateway(host=host, workspace=workspace, broker=broker, sandbox=sandbox,
                                   artifacts=artifacts, scheduler=scheduler)
    host.scheduler = scheduler
    middleware = ExecutionBoundary(host=host, gateway=gateway, scheduler=scheduler,
                                    max_messages=max_context_messages, max_characters=max_context_characters)
    middleware.model = model
    # None inherits the parent's saver with per-invocation namespaces. True is
    # per-thread history with task IDs removed; retain that only for v3 replay.
    checkpoint_mode = None if host.workflow_version in COLLAB_WORKFLOWS else True
    executor = create_agent(model=model, tools=tool_definitions(workspace, interactive=host.workflow_version in {"stage-agent-v7", "stage-agent-v8", "stage-agent-v9", "stage-agent-v10"},
                                                           patch_schema_v2=host.workflow_version in {"stage-agent-v8", "stage-agent-v9", "stage-agent-v10"}), system_prompt=middleware.prompt,
                        middleware=[middleware], checkpointer=checkpoint_mode, name="workspace_executor")
    if host.workflow_version == "stage-agent-v10":
        from .v10_sealed_saver import SEALED_PLUGIN_VERSIONS
        if host.plugin_versions == SEALED_PLUGIN_VERSIONS:
            from .v10_drain import register_host_executor
            register_host_executor(executor, host=host, gateway=gateway, middleware=middleware, scheduler=scheduler)
    return executor


def approval_resume_map(db, run_id, interrupts):
    """Only the current persisted interrupt IDs may consume matching decisions."""
    result = {}
    for item in interrupts:
        value = item.value
        if not isinstance(value, dict) or value.get("run_id") != run_id:
            raise AgentBoundaryError("Interrupt does not belong to this Run")
        if value.get("type") == "pause":
            result[item.id] = "continue"
        elif value.get("type") == "approval":
            approval = db.get(Approval, value.get("approval_id"))
            if (approval is None or approval.operation_id != value.get("operation_id")
                    or approval.fingerprint != value.get("fingerprint")):
                raise AgentBoundaryError("Approval checkpoint binding differs")
            operation = db.get(ToolOperation, approval.operation_id)
            if operation is None or operation.run_id != run_id:
                raise AgentBoundaryError("Approval belongs to another Run")
            if approval.status == "pending":
                return None
            result[item.id] = {"approval_id": approval.id, "decision": approval.status}
        elif value.get("type") == "approval_barrier":
            pending = db.scalar(select(Approval.id).join(ToolOperation).where(
                ToolOperation.run_id == run_id, Approval.status == "pending").limit(1))
            if pending:
                return None
            result[item.id] = "continue"
        else:
            raise AgentBoundaryError("Unknown interrupt type")
    return result


def reconcile_approval_waits(worker):
    """Worker-owned readiness only; HTTP approval never executes or resumes work."""
    from .m4_services import expire_approvals
    from .models import Clarification
    with worker.transaction() as db:
        for run in db.scalars(select(Run).where(Run.status == "awaiting_approval").with_for_update()):
            has_requirements = db.scalar(select(RunCommand.id).where(
                RunCommand.run_id == run.id, RunCommand.command_type == "append_requirement",
                RunCommand.status == "pending").limit(1))
            if has_requirements:
                expire_approvals(db, run.id)
            pending = db.scalar(select(Approval.id).join(ToolOperation).where(
                ToolOperation.run_id == run.id, Approval.status == "pending").limit(1))
            if pending is None:
                question = db.scalar(select(Clarification.id).where(
                    Clarification.run_id == run.id, Clarification.status == "pending").limit(1))
                run.status = "awaiting_user" if question else "queued"
                assistant = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "assistant"))
                if assistant:
                    assistant.status = run.status
                add_event(db, run, "run." + run.status, {"reason": "approval_decisions_ready"})
