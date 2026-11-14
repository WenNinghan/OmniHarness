"""Versioned, checkpointed stage workflow for explicitly selected models.

Stage calls have no external tools. All provider invocations pass through the
same owner-bound reservations as the legacy agent. Plans are data, never code.
"""
from __future__ import annotations

import hashlib
import json
import re
from operator import add
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, convert_to_messages
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .m3_agents import (
    AgentBoundaryError,
    AgentBudgetExceeded,
    AgentContextLimitExceeded,
    InvocationIdentity,
    _raise_sanitized,
)
from .m4_contract import (
    COLLAB_WORKFLOWS,
    EVIDENCE_WORKFLOW_VERSION,
    HANDOFF_WORKFLOW_VERSION,
    INTERACTIVE_COMPAT_WORKFLOW_VERSION,
    INTERACTIVE_V9_WORKFLOW_VERSION,
    INTERACTIVE_V10_WORKFLOW_VERSION,
    INTERACTIVE_WORKFLOW_VERSION,
    WORKSPACE_WORKFLOWS,
    workspace_prompts,
)
from .model_output import answer_text, extract_output, require_complete_output
from .providers import ProviderError
from .stage_contract import (
    INTERACTIVE_STAGE_WORKFLOW_VERSION,
    STAGE_KEYS,
    STAGE_PROMPTS,
    STAGE_V1_PROMPTS,
    STAGE_V1_WORKFLOW_VERSION,
    STAGE_WORKFLOW_VERSION,
)


class StageOutputError(AgentBoundaryError):
    """Structured stage output cannot be used safely."""

    def __init__(self, message, *, code="STAGE_OUTPUT_INVALID", public_message=None, output=None, stage=None):
        super().__init__(message)
        self.code = code
        self.public_message = public_message or "阶段输出格式无效，任务已停止；没有伪造成功结果。"
        self.output = output
        self.stage = stage


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    task: str = Field(min_length=1, max_length=4000)
    acceptance: str = Field(min_length=1, max_length=4000)
    depends_on: list[str] = Field(default_factory=list, max_length=8)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[PlanStep] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_graph(self):
        seen = set()
        for item in self.steps:
            if item.id in seen or not set(item.depends_on).issubset(seen):
                raise ValueError("duplicate or forward dependency")
            seen.add(item.id)
        return self


class AssignedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    step_id: str = Field(min_length=1, max_length=80)
    instruction: str = Field(min_length=1, max_length=4000)
    depends_on: list[str] = Field(default_factory=list, max_length=8)


class Dispatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[AssignedTask] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_graph(self):
        seen = set()
        for item in self.tasks:
            if item.id in seen or not set(item.depends_on).issubset(seen):
                raise ValueError("duplicate or forward dependency")
            seen.add(item.id)
        return self


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location: str = Field(min_length=1, max_length=1000)
    severity: Literal["HIGH", "MEDIUM", "LOW", "INFO"]
    description: str = Field(min_length=1, max_length=4000)
    status: Literal["PASS", "FAIL", "UNVERIFIED", "BLOCKED", "DISPUTED"]
    recommendation: str = Field(min_length=1, max_length=4000)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[ReviewFinding] = Field(max_length=20)
    unverified: list[str] = Field(max_length=20)


class StageState(TypedDict, total=False):
    messages: list
    plan: dict
    dispatch: dict
    execution: str
    review: str
    review_data: dict
    stage_outputs: dict
    task_results: Annotated[list[dict], add]
    task: dict
    constraints: list[str]
    task_goal: str
    interaction_decision: dict
    task_answers: dict


def _digest(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise AgentBoundaryError("Stage invocation cannot be bound safely") from None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse(content: str, schema):
    # Accept standard fenced JSON without executing or repairing model output.
    value = content.strip()
    match = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", value, flags=re.DOTALL | re.IGNORECASE)
    if match:
        value = match.group(1).strip()
    try:
        return schema.model_validate_json(value)
    except (ValidationError, ValueError):
        raise StageOutputError("Stage produced invalid structured output") from None


def build_stage_graph(*, models, hooks, checkpointer, max_context_messages=100, max_context_characters=200_000,
                      workflow_version=STAGE_WORKFLOW_VERSION, execution_agent=None, execution_strategy="staged"):
    """Build a versioned durable five-stage graph (external tools not available)."""
    if workflow_version not in (STAGE_V1_WORKFLOW_VERSION, STAGE_WORKFLOW_VERSION, INTERACTIVE_STAGE_WORKFLOW_VERSION, *WORKSPACE_WORKFLOWS):
        raise ValueError("Unsupported stage workflow version")
    tools_enabled = workflow_version in WORKSPACE_WORKFLOWS
    collaboration = workflow_version in COLLAB_WORKFLOWS
    interactive = workflow_version in {INTERACTIVE_STAGE_WORKFLOW_VERSION, INTERACTIVE_WORKFLOW_VERSION, INTERACTIVE_COMPAT_WORKFLOW_VERSION, INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}
    handoff_enabled = workflow_version in {HANDOFF_WORKFLOW_VERSION, EVIDENCE_WORKFLOW_VERSION, INTERACTIVE_WORKFLOW_VERSION, INTERACTIVE_COMPAT_WORKFLOW_VERSION, INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}
    if (execution_strategy not in ("staged", "single", "compact")
            or execution_strategy == "single" and not tools_enabled
            or execution_strategy == "compact" and not collaboration):
        raise ValueError("Unsupported execution strategy")
    if tools_enabled and execution_agent is None:
        raise ValueError("Workspace workflow requires its authorized execution graph")
    prompts = workspace_prompts(workflow_version) if tools_enabled else (
        STAGE_V1_PROMPTS if workflow_version == STAGE_V1_WORKFLOW_VERSION else STAGE_PROMPTS)
    if set(models) != set(STAGE_KEYS):
        raise ValueError("All five stages require an explicit model")
    graph = StateGraph(StageState)

    def compile_graph():
        compiled = graph.compile(checkpointer=checkpointer)
        if workflow_version == INTERACTIVE_V10_WORKFLOW_VERSION:
            from .v10_sealed_saver import V10SealedSaver
            if isinstance(checkpointer, V10SealedSaver):
                from .v10_drain import register_host_graph
                register_host_graph(compiled, host=hooks, executor=execution_agent, saver=checkpointer)
        return compiled

    def make_prepare(stage):
        async def prepare(state, config):
            await hooks.check_control("model_calls", stage, None)
            messages = convert_to_messages(state.get("messages", []))
            # Requirements are committed to the graph before the following call.
            additions = await hooks.stage_requirements(stage, messages)
            instruction = prompts[stage]
            if workflow_version == INTERACTIVE_V10_WORKFLOW_VERSION and stage == "plan":
                from sqlalchemy import select

                from .models import Message
                async with hooks.serial:
                    with hooks.worker.transaction() as db:
                        hooks._run(db, "plan", allow_supplements=True)
                        source = db.scalar(select(Message).where(
                            Message.run_id == hooks.run_id, Message.role == "user"))
                        if source is not None:
                            instruction += "\n本次原始用户消息的 source_message_id：" + source.id
            if workflow_version in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION} and stage == "review":
                instruction += ("\n每个 candidate_refs 引用须明确 dimension：execution、exit_code、log、"
                                "initial_snapshot 或 runtime_source。log 引用还须提供 artifact_id 和 "
                                "expected_sha256；各维度独立核验，退出码 0 不证明源码一致或质量。")
            protected_evidence = ()
            if collaboration and stage in ("dispatch", "review"):
                from .m4_collaboration import protected_task_messages
                messages = await protected_task_messages(hooks, messages)
            if collaboration and stage == "review":
                from .review_evidence import load_review_evidence
                packet = await load_review_evidence(hooks)
                evidence_message = HumanMessage(id="omni-evidence:review", content=json.dumps({
                    "host_evidence": packet,
                    "executor_claims": state.get("execution", ""),
                    "acceptance": state.get("plan", {}),
                }, ensure_ascii=False, separators=(",", ":")))
                messages.append(evidence_message)
                if workflow_version in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
                    protected_evidence = (evidence_message,)
            if tools_enabled and stage == "dispatch":
                # This validated state is authoritative even if the earlier
                # model message was summarized. Protect it with the CURRENT
                # stage instruction, never replay all previous stage requests.
                instruction += "\n本阶段只输出任务分配 JSON。下面是已校验的完整计划：\n" + json.dumps(
                    state["plan"], ensure_ascii=False, separators=(",", ":"))
            messages = [*messages, *additions, HumanMessage(content=instruction,
                        id=f"omni-stage:{stage}" if tools_enabled else None)]
            if tools_enabled:
                from .context_host import prepare_agent_context
                messages = [message.model_copy(update={"id": message.id or "omni-source:" + _digest({
                    "index": index, "type": message.type, "content": message.content,
                })}) for index, message in enumerate(messages)]
                if protected_evidence:
                    protected_evidence = tuple(message for message in messages if message.id == "omni-evidence:review")
                prepared = await prepare_agent_context(hooks, stage, messages, model=models[stage],
                    protected_messages=protected_evidence,
                    system_message=SystemMessage(content="你是 OmniHarness 的阶段 Agent。只依据提供的信息工作，不得虚构工具操作或验证结果。"),
                    checkpoint_ns=config["configurable"].get("checkpoint_ns", ""), task_id=f"prepare-{stage}")
                messages = list(prepared.messages)
            return {"messages": messages}
        return prepare

    async def invoke(stage, messages, *, task_id=None):
        system = SystemMessage(content="你是 OmniHarness 的阶段 Agent。只依据提供的信息工作，不得虚构工具操作或验证结果。")
        if not tools_enabled and len(messages) + 1 > max_context_messages:
            raise AgentContextLimitExceeded("Stage input exceeds local context limit")
        serialized = [m.model_dump(mode="json") for m in [system, *messages]]
        if not tools_enabled and len(json.dumps(serialized, ensure_ascii=False)) > max_context_characters:
            raise AgentContextLimitExceeded("Stage input exceeds local context limit")
        return system, serialized

    async def call_model(stage, messages, config, *, task_id=None):
        # Stages exchange public task artifacts, not another model's private
        # reasoning/signatures. Native messages remain intact in checkpoints.
        request_messages = [AIMessage(content=answer_text(message.content), id=message.id)
                            if isinstance(message, AIMessage) else message for message in messages]
        system, serialized = await invoke(stage, request_messages, task_id=task_id)
        thread_id = config["configurable"]["thread_id"]
        identity = InvocationIdentity(
            thread_id=thread_id,
            invocation_id=_digest({"thread_id": thread_id, "stage": stage, "task_id": task_id, "attempt": 1}),
            binding_fingerprint=_digest({"stage": stage, "task_id": task_id, "messages": serialized}),
            command_ids=tuple(m.id[len("omni-command:"):] for m in messages
                              if isinstance(m, HumanMessage) and isinstance(m.id, str)
                              and m.id.startswith("omni-command:")),
        )
        evidence_review = workflow_version in {EVIDENCE_WORKFLOW_VERSION, INTERACTIVE_WORKFLOW_VERSION, INTERACTIVE_COMPAT_WORKFLOW_VERSION, INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION} and stage == "review"
        await hooks.check_control("model_calls", stage, None)
        saved = await hooks.replay_invocation("model_calls", stage, None, identity) if tools_enabled else None
        if saved is not None:
            response = AIMessage.model_validate(saved["message"])
            try:
                content = require_complete_output(response).strip()
            except ProviderError as exc:
                if not evidence_review:
                    raise
                if evidence_review and extract_output(response).text.strip():
                    from .review_evidence import persist_review_evidence
                    await persist_review_evidence(hooks, identity, extract_output(response).text.strip(),
                                                  allow_incomplete=True)
                raise StageOutputError(exc.public_message, code=exc.code, public_message=exc.public_message,
                                       output=extract_output(response), stage=stage) from None
            return (response, content, identity) if evidence_review else (response, content)
        try:
            if tools_enabled and hasattr(hooks, "worker"):
                from .context_visibility import model_input_callback
                on_reserved = model_input_callback(hooks, stage, identity, request_messages,
                                                   system_present=True, system_message=system)
                if evidence_review:
                    from .review_evidence import review_input_on_reserved
                    previous = on_reserved
                    capture = review_input_on_reserved(hooks, identity, request_messages)
                    def on_reserved(db, row):
                        previous(db, row)
                        capture(db, row)
                allowed = await hooks.reserve_invocation("model_calls", stage, None, identity,
                                                         on_reserved=on_reserved)
            else:
                allowed = await hooks.reserve_invocation("model_calls", stage, None, identity)
        except AgentBoundaryError:
            await hooks.check_control("model_calls", stage, None)
            raise
        if not allowed:
            raise AgentBudgetExceeded("Run budget exceeded before stage dispatch")
        try:
            async def model_and_save():
                response = await models[stage].ainvoke([system, *request_messages])
                if tools_enabled:
                    await hooks.finish_invocation("model_calls", stage, None, identity,
                                                   result={"message": response.model_dump(mode="json")})
                return response
            response = await hooks.scheduler.run_tracked(model_and_save) if tools_enabled else await model_and_save()
        except BaseException as exc:
            try:
                diagnose = getattr(hooks, "mark_invocation_uncertain_with_diagnostics", None)
                if callable(diagnose):
                    await diagnose("model_calls", stage, None, identity,
                                   error=exc, transport_mode=("compatible-native-stream-v1" if
                                       getattr(models[stage], "omni_normalize_stream_usage", False)
                                       and getattr(models[stage], "streaming", False) else "ainvoke_default"))
                else:
                    await hooks.mark_invocation_uncertain("model_calls", stage, None, identity)
            finally:
                _raise_sanitized(exc)
        if not tools_enabled:
            await hooks.finish_invocation("model_calls", stage, None, identity)
        if not isinstance(response, AIMessage) or response.tool_calls:
            raise StageOutputError("Stage returned an unsupported response")
        try:
            content = require_complete_output(response).strip()
        except ProviderError as exc:
            if evidence_review and extract_output(response).text.strip():
                from .review_evidence import persist_review_evidence
                await persist_review_evidence(hooks, identity, extract_output(response).text.strip(),
                                              allow_incomplete=True)
            raise StageOutputError(exc.public_message, code=exc.code, public_message=exc.public_message,
                                   output=extract_output(response), stage=stage) from None
        return (response, content, identity) if evidence_review else (response, content)

    def make_call(stage):
        async def call(state, config):
            messages = convert_to_messages(state["messages"])
            evidence_review = workflow_version in {EVIDENCE_WORKFLOW_VERSION, INTERACTIVE_WORKFLOW_VERSION, INTERACTIVE_COMPAT_WORKFLOW_VERSION, INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION} and stage == "review"
            called = await call_model(stage, messages, config)
            if evidence_review:
                response, content, identity = called
            else:
                response, content = called
            outputs = {**state.get("stage_outputs", {}), stage: content}
            result = {"messages": [*messages, response], "stage_outputs": outputs}
            if stage == "plan":
                if collaboration:
                    from .m4_collaboration import CollaborationPlan
                    if workflow_version == INTERACTIVE_V10_WORKFLOW_VERSION:
                        value = content.strip()
                        match = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", value,
                                             flags=re.DOTALL | re.IGNORECASE)
                        try:
                            raw = json.loads(match.group(1).strip() if match else value)
                            if not isinstance(raw, dict):
                                raise ValueError
                            candidates = raw.pop("action_candidates", None)
                            result["plan"] = CollaborationPlan.model_validate(raw).model_dump(mode="json")
                        except (ValueError, TypeError, ValidationError):
                            raise StageOutputError("Stage produced invalid structured output") from None
                        await save_v10_actions(candidates)
                    else:
                        result["plan"] = _parse(content, CollaborationPlan).model_dump(mode="json")
                else:
                    result["plan"] = _parse(content, Plan).model_dump(mode="json")
            elif stage == "dispatch":
                dispatch = _parse(content, Dispatch)
                steps = {s["id"] for s in state.get("plan", {}).get("steps", [])}
                if {t.step_id for t in dispatch.tasks} != steps:
                    raise StageOutputError("Dispatch must cover each planned step")
                # A dispatcher may add dependencies, but cannot silently erase
                # the plan's prerequisites, including indirect task links.
                task_map = {t.id: t for t in dispatch.tasks}
                step_deps = {s["id"]: set(s["depends_on"]) for s in state["plan"]["steps"]}
                for task in dispatch.tasks:
                    ancestors, frontier = set(), list(task.depends_on)
                    while frontier:
                        dependency = frontier.pop()
                        if dependency not in ancestors:
                            ancestors.add(dependency)
                            frontier.extend(task_map[dependency].depends_on)
                    required = {t.id for t in dispatch.tasks if t.step_id in step_deps[task.step_id]}
                    if not required.issubset(ancestors):
                        raise StageOutputError("Dispatch removed a planned dependency")
                result["dispatch"] = dispatch.model_dump(mode="json")
                if collaboration:
                    from .m4_collaboration import enrich_tasks
                    result["dispatch"] = enrich_tasks(result["dispatch"], state["plan"])
                    if workflow_version == INTERACTIVE_V10_WORKFLOW_VERSION:
                        await bind_v10_tasks(result["dispatch"])
            elif stage == "review":
                if evidence_review:
                    from .review_contract import ReviewV6
                    from .review_evidence import persist_review_evidence, render_review_evidence
                    schema = ReviewV6
                    if workflow_version in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
                        from .review_contract import ReviewV9
                        schema = ReviewV9
                    try:
                        review = _parse(content, schema)
                    except StageOutputError:
                        # Preserve the public model response with the invocation;
                        # never manufacture a successful structured review.
                        await persist_review_evidence(hooks, identity, content)
                        raise
                    report = await persist_review_evidence(hooks, identity, content, review)
                    result["review"] = render_review_evidence(report)
                    result["review_data"] = report
                    result["stage_outputs"] = {**outputs, "review": result["review"]}
                elif workflow_version == STAGE_V1_WORKFLOW_VERSION:
                    result["review"] = content
                else:
                    review = _parse(content, Review)
                    lines = [review.summary]
                    lines.extend(f"[{item.severity}/{item.status}] {item.location}：{item.description}；建议：{item.recommendation}"
                                 for item in review.findings)
                    if review.unverified:
                        lines.append("未验证：" + "；".join(review.unverified))
                    result["review"] = "\n".join(lines)
                    result["review_data"] = review.model_dump(mode="json")
            return result
        return call

    async def save_v10_actions(candidates, role="plan"):
        from sqlalchemy import select

        from .action_contract import validate_candidates
        from .models import Message
        async with hooks.serial:
            with hooks.worker.transaction() as db:
                run, _ = hooks._run(db, role, allow_supplements=True)
                source = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == "user"))
                contract = run.config_snapshot.get("action_contract") or {}
                frozen = contract.get("explicit")
                supplied = ((contract.get("request") or {}).get("requirements") is not None
                            or contract.get("version", 1) > 1)
                envelope = frozen if isinstance(frozen, dict) and supplied else validate_candidates(
                    candidates, run_id=run.id, workspace_id=run.workspace_id,
                    source_message_id=source.id if source else None,
                    source_text=source.content if source else None,
                    source_run_id=source.run_id if source else None)
                outcome = dict(run.outcome or {})
                old = outcome.get("v10_actions")
                if old is not None:
                    unbound = {**old, "actions": [{k: v for k, v in item.items() if k != "task_id"}
                                                  for item in old.get("actions", [])]}
                    if unbound != envelope:
                        raise AgentBoundaryError("Action contract differs from persisted Run")
                else:
                    outcome["v10_actions"] = envelope
                run.outcome = outcome

    async def bind_v10_tasks(dispatch):
        async with hooks.serial:
            with hooks.worker.transaction() as db:
                run, _ = hooks._run(db, "execute", allow_supplements=True)
                outcome = dict(run.outcome or {})
                envelope = dict(outcome.get("v10_actions") or {})
                tasks = dispatch.get("tasks", [])
                actions = []
                for original in envelope.get("actions", []):
                    item = dict(original)
                    step = item.get("step_id")
                    matches = [task for task in tasks if task.get("step_id") == step] if step else tasks
                    if len(matches) == 1:
                        task_id = matches[0]["id"]
                        if item.get("task_id") not in (None, task_id):
                            raise AgentBoundaryError("Action task binding differs from persisted Run")
                        item["task_id"] = task_id
                    actions.append(item)
                envelope["actions"] = actions
                outcome["v10_actions"] = envelope
                run.outcome = outcome

    # Each batch is checkpointed before dispatch. New user requirements enter
    # here before any later calls, preserving the same command fencing as M3.
    async def prepare_batch(state):
        await hooks.check_control("model_calls", "execute", None)
        messages = convert_to_messages(state["messages"])
        additions = await hooks.stage_requirements("execute", messages)
        return {"messages": [*messages, *additions]}

    def choose_tasks(state):
        tasks = state["dispatch"]["tasks"]
        latest = {item["task_id"]: item for item in state.get("task_results", [])}
        if interactive and any(item.get("status") == "needs_user" for item in latest.values()):
            return "wait_task_user"
        completed = {key for key, item in latest.items() if item.get("status", "completed") == "completed"}
        if len(completed) == len(tasks):
            return "aggregate"
        ready = [task for task in tasks if task["id"] not in completed
                 and set(task["depends_on"]).issubset(completed)]
        if not ready:
            raise StageOutputError("No executable task remains")
        selected = ready[:1] if interactive else ready[:2]
        if collaboration:
            from .m4_collaboration import independent_batch
            selected = independent_batch(ready[:1] if interactive else ready)
        return [Send("execute_task", {"messages": state["messages"], "task": task,
                                      **({"constraints": state.get("plan", {}).get("constraints", []),
                                          "task_goal": state.get("plan", {}).get("goal", "")}
                                         if collaboration else {}),
                                      **({"dispatch": state["dispatch"]} if handoff_enabled else {}),
                                      "task_results": list(latest.values()) if interactive else state.get("task_results", []),
                                      **({"task_answers": state.get("task_answers", {})} if interactive else {})})
                for task in selected]

    async def execute_task(state, config):
        task = state["task"]
        if workflow_version == INTERACTIVE_V10_WORKFLOW_VERSION:
            from .m3_host import AgentOperationNeedsReview
            async with hooks.serial:
                with hooks.worker.transaction() as db:
                    run, _ = hooks._run(db, "execute", allow_supplements=True)
                    observations = (run.outcome or {}).get("v10_child_executions") or {}
                    if task["id"] in observations:
                        raise AgentOperationNeedsReview("Completed child requires checkpoint reconciliation")
        latest_results = {item["task_id"]: item for item in state.get("task_results", [])}
        dependencies = {key: item["content"] for key, item in latest_results.items()
                        if key in task["depends_on"] and item.get("status", "completed") == "completed"}
        task_instruction = (prompts["execute"] + "\n只执行当前子任务：\n"
                            + json.dumps(task, ensure_ascii=False)
                            + "\n已完成的依赖结果：\n" + json.dumps(dependencies, ensure_ascii=False))
        messages = [*convert_to_messages(state["messages"]), HumanMessage(content=task_instruction,
                    id=f"omni-task:{task['id']}" if tools_enabled else None)]
        if collaboration:
            from .m4_collaboration import protected_task_messages
            from .m4_collaboration import task_instruction as compact_instruction
            if handoff_enabled and execution_strategy != "single":
                from .m4_collaboration import handoff_task_instruction
                from .review_evidence import prepare_dependency_handoffs
                handoffs = await prepare_dependency_handoffs(
                    hooks, task=task, tasks=state["dispatch"]["tasks"],
                    task_results=state.get("task_results", []),
                    task_goal=state.get("task_goal", ""), constraints=state.get("constraints", []),
                    messages=convert_to_messages(state["messages"]))
                messages = await protected_task_messages(hooks, convert_to_messages(state["messages"]))
                instructions = handoff_task_instruction(task, goal=state.get("task_goal", ""),
                    constraints=state.get("constraints", []), handoffs=handoffs)
                messages.append(HumanMessage(id=f"omni-task:{task['id']}", content=instructions))
            else:
                from .review_evidence import load_review_evidence
                dependencies = [{**item, "content": item["content"][:2500], "truncated": len(item["content"]) > 2500}
                                for item in state.get("task_results", []) if item["task_id"] in task["depends_on"]]
                references = [identity for item in dependencies for identity in item.get("operation_ids", [])]
                packet = await load_review_evidence(hooks, operation_ids=references) if references else None
                messages = await protected_task_messages(hooks, convert_to_messages(state["messages"]))
                instructions = compact_instruction(task, goal=state.get("task_goal", ""),
                                                   constraints=state.get("constraints", []), dependencies=dependencies)
                messages.append(HumanMessage(id=f"omni-task:{task['id']}", content=instructions))
                if packet is not None:
                    messages.append(HumanMessage(id=f"omni-evidence:{task['id']}",
                                                 content=json.dumps({"host_evidence": packet}, ensure_ascii=False)))
            # Child config is checkpointed through the parent's immutable task.
            config = {**config, "configurable": {**config["configurable"],
                       "omni_task_scope": {"id": task["id"], "write_paths": task.get("write_paths")}}}
        if interactive:
            from .interaction_contract import TASK_DECISION_INSTRUCTION
            replies = state.get("task_answers", {}).get(task["id"], [])
            if replies and workflow_version not in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
                messages.append(HumanMessage(content="该任务中用户已确认的问答：\n" +
                    json.dumps(replies, ensure_ascii=False), id=f"omni-interaction:answers:{task['id']}"))
            messages.append(HumanMessage(content=TASK_DECISION_INSTRUCTION,
                                         id=f"omni-interaction:task:{task['id']}"))
        if tools_enabled:
            messages = [AIMessage(content=answer_text(message.content), id=message.id)
                        if isinstance(message, AIMessage) else message for message in messages]
            messages = [message.model_copy(update={"id": message.id or "omni-executor:" + _digest({
                "task": task["id"], "index": index, "type": message.type, "content": message.content,
            })}) for index, message in enumerate(messages)]
            result = await execution_agent.ainvoke({"messages": messages}, config)
            if result.get("__interrupt__"):
                raise AgentBoundaryError("Nested graph did not propagate its interrupt")
            content = require_complete_output(result["messages"][-1]).strip()
        else:
            attempt = len(state.get("task_answers", {}).get(task["id"], [])) if interactive else 0
            invocation_task = f"{task['id']}:answer:{attempt}" if interactive else task["id"]
            _, content = await call_model("execute", messages, config, task_id=invocation_task)
        if interactive:
            from .interaction_contract import TaskDecision
            task_decision = _parse(content, TaskDecision)
            if task_decision.status == "needs_user":
                if len(state.get("task_answers", {}).get(task["id"], [])) >= 3:
                    raise StageOutputError("Too many clarification rounds", code="CLARIFICATION_LIMIT",
                        public_message="子任务反复请求澄清，已停止以避免无限循环。")
                if tools_enabled:
                    # A resumed model could repeat earlier effects; until the
                    # child graph supports a dedicated durable interrupt we
                    # refuse to restart any Run that already used tools.
                    from sqlalchemy import select

                    from .models import ToolOperation
                    async with hooks.serial:
                        with hooks.worker.transaction() as db:
                            used = db.scalar(select(ToolOperation.id).where(ToolOperation.run_id == hooks.run_id).limit(1))
                    if used:
                        raise StageOutputError("Tool-bearing mid-task clarification requires review",
                            code="CLARIFICATION_REQUIRES_REVIEW",
                            public_message="子任务需要进一步询问，但任务已调用工具；为避免重复操作，已停止并保留记录。")
                return {"task_results": [{"task_id": task["id"], "step_id": task["step_id"],
                         "status": "needs_user", "content": task_decision.content,
                         "questions": [q.model_dump(mode="json") for q in task_decision.questions]}]}
            content = task_decision.content
        if workflow_version == INTERACTIVE_V10_WORKFLOW_VERSION and tools_enabled:
            from .v10_closure import record_zero_tool_child_terminal
            await record_zero_tool_child_terminal(hooks, execution_agent, checkpointer, task["id"])
        result = {"task_id": task["id"], "step_id": task["step_id"], "content": content}
        if collaboration:
            from .review_evidence import persist_task_result, task_evidence_references
            result["operation_ids"] = await task_evidence_references(hooks, task["id"])
            result["result_artifact"] = await persist_task_result(hooks, task["id"], content)
        return {"task_results": [result]}

    def aggregate(state):
        # Model-generated assignment is distinct from these actual results.
        by_id = {item["task_id"]: item for item in state.get("task_results", [])}
        task_results = [by_id[task["id"]] for task in state["dispatch"]["tasks"]]
        execution = "\n\n".join(f"子任务 {item['task_id']}：\n{item['content']}" for item in task_results)
        return {"execution": execution,
                "messages": [*state["messages"], AIMessage(content=execution)],
                "stage_outputs": {**state.get("stage_outputs", {}), "execute": execution}}

    if interactive:
        async def decide_user_input(state, config):
            from .interaction_contract import DECISION_INSTRUCTION, UserDecision
            messages = [*convert_to_messages(state.get("messages", [])),
                        HumanMessage(content=DECISION_INSTRUCTION, id="omni-interaction:preflight")]
            if workflow_version in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
                from .context_host import prepare_agent_context
                messages = [message.model_copy(update={"id": message.id or "omni-source:" + _digest({
                    "index": index, "type": message.type, "content": message.content,
                })}) for index, message in enumerate(messages)]
                prepared = await prepare_agent_context(hooks, "think", messages, model=models["think"],
                    system_message=SystemMessage(content="你是 OmniHarness 的阶段 Agent。只依据提供的信息工作，不得虚构工具操作或验证结果。"),
                    checkpoint_ns=config["configurable"].get("checkpoint_ns", ""), task_id="interaction-preflight")
                messages = list(prepared.messages)
            _, content = await call_model("think", messages, config, task_id="interaction-preflight")
            decision = _parse(content, UserDecision)
            return {"interaction_decision": decision.model_dump(mode="json")}

        async def wait_for_user(state):
            from .clarification_service import create_clarification
            from .models import Run
            if not hasattr(hooks, "worker"):
                raise AgentBoundaryError("User clarification requires durable host storage")
            questions = state["interaction_decision"]["questions"]
            if not questions:
                return {}
            ids = []
            async with hooks.serial:
                with hooks.worker.transaction() as db:
                    run = db.get(Run, hooks.run_id)
                    if run is None:
                        raise AgentBoundaryError("Run not found")
                    for index, item in enumerate(questions):
                        row = create_clarification(db, run, f"preflight:{index}", None,
                                                   item["question"], item["reason"], item["options"], stage="think")
                        ids.append(row.id)
            answers, canonical_answers = [], []
            for question_id in ids:
                from .interrupt_timing import user_interrupt
                signal = interrupt(user_interrupt(hooks, kind="user_clarification",
                                                   clarification_id=question_id))
                if (not isinstance(signal, dict) or signal.get("clarification_id") != question_id
                        or not isinstance(signal.get("answer"), str) or not signal["answer"].strip()):
                    raise AgentBoundaryError("Clarification resume does not match its checkpoint")
                if workflow_version in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
                    from .clarification_context import clarification_message, verify_clarification_signal
                    async with hooks.serial:
                        with hooks.worker.transaction() as db:
                            hooks._run(db, "think", allow_supplements=True)
                            row = verify_clarification_signal(db, hooks.run_id, question_id, signal["answer"],
                                                              questions[len(answers)]["question"])
                            canonical_answers.append(clarification_message(row))
                answers.append(signal["answer"])
            messages = list(convert_to_messages(state.get("messages", [])))
            if workflow_version in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
                messages.extend(canonical_answers)
            else:
                for index, (item, answer) in enumerate(zip(questions, answers)):
                    messages.append(HumanMessage(id=f"omni-clarification:{ids[index]}",
                        content="用户对问题的回复：\n问题：" + item["question"] + "\n回答：" + answer))
            return {"messages": messages}

        async def wait_for_task_user(state):
            from .clarification_service import create_clarification
            from .models import Run
            latest = {item["task_id"]: item for item in state.get("task_results", [])}
            waiting = [item for item in latest.values() if item.get("status") == "needs_user"]
            if len(waiting) != 1 or not hasattr(hooks, "worker"):
                raise AgentBoundaryError("Task clarification cannot be safely bound")
            task_result = waiting[0]
            task_id, questions = task_result["task_id"], task_result["questions"]
            previous = state.get("task_answers", {}).get(task_id, [])
            ids = []
            async with hooks.serial:
                with hooks.worker.transaction() as db:
                    run = db.get(Run, hooks.run_id)
                    if run is None:
                        raise AgentBoundaryError("Run not found")
                    for index, item in enumerate(questions):
                        key = f"task:{task_id}:{len(previous)}:{index}"
                        row = create_clarification(db, run, key, task_id, item["question"],
                                                   item["reason"], item["options"], stage="execute")
                        ids.append(row.id)
            replies, canonical_replies = [], []
            for question_id, item in zip(ids, questions):
                from .interrupt_timing import user_interrupt
                signal = interrupt(user_interrupt(hooks, kind="user_clarification",
                                                   clarification_id=question_id))
                if (not isinstance(signal, dict) or signal.get("clarification_id") != question_id
                        or not isinstance(signal.get("answer"), str) or not signal["answer"].strip()):
                    raise AgentBoundaryError("Task clarification resume differs from checkpoint")
                if workflow_version in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
                    from .clarification_context import clarification_message, verify_clarification_signal
                    async with hooks.serial:
                        with hooks.worker.transaction() as db:
                            hooks._run(db, "execute", allow_supplements=True)
                            row = verify_clarification_signal(db, hooks.run_id, question_id, signal["answer"], item["question"])
                            canonical_replies.append(clarification_message(row))
                replies.append({"question": item["question"], "answer": signal["answer"]})
            messages = list(convert_to_messages(state.get("messages", [])))
            if workflow_version in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
                messages.extend(canonical_replies)
            else:
                for question_id, item in zip(ids, replies):
                    messages.append(HumanMessage(id=f"omni-clarification:{question_id}",
                        content="用户对问题的回复：\n问题：" + item["question"] + "\n回答：" + item["answer"]))
            return {"messages": messages,
                    "task_answers": {**state.get("task_answers", {}), task_id: [*previous, *replies]},
                    "task_results": [{"task_id": task_id, "step_id": task_result["step_id"],
                                      "status": "retry", "content": task_result["content"]}]}

        def after_preflight(state):
            return "wait_user" if state["interaction_decision"]["status"] == "needs_user" else "preflight_done"

        def preflight_done(_state):
            return {}

        graph.add_node("decide_user_input", decide_user_input)
        graph.add_node("wait_user", wait_for_user)
        graph.add_node("wait_task_user", wait_for_task_user)
        graph.add_node("preflight_done", preflight_done)
        graph.add_edge(START, "decide_user_input")
        graph.add_conditional_edges("decide_user_input", after_preflight, ["wait_user", "preflight_done"])
        graph.add_edge("wait_user", "preflight_done")

    if execution_strategy == "single":
        async def single_prepare(state):
            await hooks.check_control("model_calls", "execute", None)
            messages = convert_to_messages(state.get("messages", []))
            additions = await hooks.stage_requirements("execute", messages)
            if workflow_version == INTERACTIVE_V10_WORKFLOW_VERSION:
                await save_v10_actions(None, role="execute")
                await bind_v10_tasks({"tasks": [{"id": "single", "step_id": "single"}]})
            return {"messages": [*messages, *additions], "task": {
                "id": "single", "step_id": "single", "instruction": "独立完成用户原目标，自主规划、调用工具并验证。",
                "depends_on": [],
                **({"acceptance": "满足用户原始目标及全部验收约束，保存实际验证证据。", "write_paths": None}
                   if collaboration else {}),
            }}

        def single_finish(state):
            content = state["task_results"][-1]["content"]
            return {"execution": content, "stage_outputs": {"execute": content},
                    "messages": [*state["messages"], AIMessage(content=content)]}

        graph.add_node("prepare_single", single_prepare)
        graph.add_node("execute_task", execute_task)
        graph.add_node("finish", single_finish)
        graph.add_edge("preflight_done" if interactive else START, "prepare_single")
        graph.add_edge("prepare_single", "execute_task")
        if interactive:
            graph.add_conditional_edges("execute_task", lambda state: (
                "wait_task_user" if state["task_results"][-1].get("status") == "needs_user" else "finish"),
                ["wait_task_user", "finish"])
            graph.add_edge("wait_task_user", "execute_task")
        else:
            graph.add_edge("execute_task", "finish")
        graph.add_edge("finish", END)
        return compile_graph()

    initial_stages = ("plan",) if execution_strategy == "compact" else ("think", "plan", "dispatch")
    for index, stage in enumerate(initial_stages):
        prep, call = f"prepare_{stage}", f"call_{stage}"
        graph.add_node(prep, make_prepare(stage))
        graph.add_node(call, make_call(stage))
        graph.add_edge(("preflight_done" if interactive else START) if index == 0 else f"call_{initial_stages[index - 1]}", prep)
        graph.add_edge(prep, call)

    if execution_strategy == "compact":
        async def dispatch_without_model(state):
            from .m4_collaboration import deterministic_dispatch
            dispatch = deterministic_dispatch(state["plan"])
            if workflow_version == INTERACTIVE_V10_WORKFLOW_VERSION:
                await bind_v10_tasks(dispatch)
            return {"dispatch": dispatch}
        graph.add_node("dispatch_validated", dispatch_without_model)
        graph.add_edge("call_plan", "dispatch_validated")

    graph.add_node("prepare_batch", prepare_batch)
    graph.add_node("execute_task", execute_task)
    graph.add_node("aggregate", aggregate)
    graph.add_edge("dispatch_validated" if execution_strategy == "compact" else "call_dispatch", "prepare_batch")
    graph.add_conditional_edges("prepare_batch", choose_tasks,
                                ["execute_task", "aggregate", "wait_task_user"] if interactive else ["execute_task", "aggregate"])
    graph.add_edge("execute_task", "prepare_batch")
    if interactive:
        graph.add_edge("wait_task_user", "prepare_batch")
    graph.add_node("prepare_review", make_prepare("review"))
    graph.add_node("call_review", make_call("review"))
    graph.add_edge("aggregate", "prepare_review")
    graph.add_edge("prepare_review", "call_review")

    async def finish_interactive(state, config):
        instruction = ("请直接回答用户原始问题，综合已经完成的工作和审查，"
                       "不要逐条粘贴子任务原文。只依据真实记录说明验证和未验证事项；不要虚构执行结果。")
        if workflow_version in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
            from .review_evidence import load_review_evidence
            packet = await load_review_evidence(hooks)
            instruction += ("\n宿主核验的当前 Run 证据（各维度分别理解；命令退出码为 0 "
                            "只表示该次命令成功退出，不证明代码质量或任务完成）：\n") + json.dumps(
                {"host_evidence": packet}, ensure_ascii=False, separators=(",", ":"))
        messages = [*convert_to_messages(state["messages"]), HumanMessage(
            id="omni-stage:think" if workflow_version in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION} else "omni-interaction:final",
            content=instruction)]
        if workflow_version in {INTERACTIVE_V9_WORKFLOW_VERSION, INTERACTIVE_V10_WORKFLOW_VERSION}:
            from .context_host import prepare_agent_context
            messages = [message.model_copy(update={"id": message.id or "omni-source:" + _digest({
                "index": index, "type": message.type, "content": message.content,
            })}) for index, message in enumerate(messages)]
            prepared = await prepare_agent_context(hooks, "think", messages, model=models["think"],
                system_message=SystemMessage(content="你是 OmniHarness 的阶段 Agent。只依据提供的信息工作，不得虚构工具操作或验证结果。"),
                checkpoint_ns=config["configurable"].get("checkpoint_ns", ""), task_id="interaction-final")
            messages = list(prepared.messages)
        response, _ = await call_model("think", messages, config, task_id="interaction-final")
        return {"messages": [*state["messages"], response]}

    def finish(state):
        answer = ("执行结果：\n" + state["execution"] + "\n\n独立审查：\n" + state["review"]
                  + ("\n\n工具记录与测试证据可在运行详情查看；快照中的改动不会自动写回原项目。" if tools_enabled
                     else "\n\n说明：本次流程没有文件、命令或联网工具，未执行外部验证。"))
        return {"messages": [*state["messages"], AIMessage(content=answer)]}

    graph.add_node("finish", finish_interactive if interactive else finish)
    graph.add_edge("call_review", "finish")
    graph.add_edge("finish", END)
    return compile_graph()
