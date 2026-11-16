"""Frozen local context limits; unknown provider capabilities stay unknown."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import ClassVar, Protocol

from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import count_tokens_approximately


class ContextPolicyError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def public_context_error(code: str) -> str:
    return {
        "CONTEXT_GROUP_LIMIT": "单条消息或完整工具交换超出本次上下文容量，请增大输入目标或缩小读取分页；原始记录已保留。",
        "CONTEXT_LIMIT": "当前任务与保留内容超过输入容量，请检查输入目标、模型窗口和输出预留；原始记录已保留。",
        "SUMMARY_INCOMPLETE": "上下文摘要未完整生成，已保留原对话历史和收到的摘要记录。",
        "SUMMARY_OUTPUT_LIMIT": "上下文摘要超过设定上限，未替换原对话历史。",
        "CONTEXT_TOOL_PAIR": "上下文中的工具调用与结果尚未完整配对，已停止整理并保留原记录。",
    }.get(code, "上下文未能安全整理，请检查上下文配置与原始记录；当前任务已停止。")


def _positive(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContextPolicyError("CONTEXT_CONFIG", f"{name} must be a positive integer")


@dataclass(frozen=True)
class ContextPolicy:
    expected_version: ClassVar[str] = "context-v1"
    version: str = "context-v1"
    input_target_tokens: int = 16_000
    summarize_at: float = 0.8
    summary_output_tokens: int = 4_000
    keep_recent_tokens: int = 4_000
    window_margin_tokens: int = 1_024
    max_messages: int = 100
    max_characters: int = 200_000

    def __post_init__(self):
        if self.version != self.expected_version:
            raise ContextPolicyError("CONTEXT_CONFIG", "Unsupported context policy version")
        for name in ("input_target_tokens", "summary_output_tokens", "keep_recent_tokens",
                     "window_margin_tokens", "max_messages", "max_characters"):
            _positive(getattr(self, name), name)
        if (isinstance(self.summarize_at, bool) or not isinstance(self.summarize_at, (int, float))
                or not math.isfinite(self.summarize_at) or not 0 < self.summarize_at < 1):
            raise ContextPolicyError("CONTEXT_CONFIG", "Summary threshold must be between zero and one")


@dataclass(frozen=True)
class ContextPolicyV2(ContextPolicy):
    expected_version: ClassVar[str] = "context-v2"
    version: str = "context-v2"
    counter_profile: str = "unicode-weighted-v2"
    summary_target_tokens: int = 1200
    summary_recovery_output_tokens: int = 8000
    summary_recovery_attempts: int = 1
    summary_min_new_tokens: int = 512
    summary_min_gain: float = 0.2
    post_summary_target: float = 0.6
    summary_thinking: str = "disable_if_verified"

    def __post_init__(self):
        super().__post_init__()
        for name in ("summary_target_tokens", "summary_recovery_output_tokens", "summary_min_new_tokens"):
            _positive(getattr(self, name), name)
        if (self.counter_profile != "unicode-weighted-v2"
                or type(self.summary_recovery_attempts) is not int or self.summary_recovery_attempts not in (0, 1)
                or self.summary_thinking not in {"inherit", "disable_if_verified"}
                or self.summary_target_tokens > self.summary_output_tokens
                or self.summary_recovery_output_tokens < self.summary_output_tokens
                or self.summary_recovery_output_tokens > 8000):
            raise ContextPolicyError("CONTEXT_CONFIG", "Unsupported summary v2 configuration")
        for value in (self.summary_min_gain, self.post_summary_target):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 1:
                raise ContextPolicyError("CONTEXT_CONFIG", "Summary ratios must be between zero and one")
        if self.post_summary_target >= self.summarize_at:
            raise ContextPolicyError("CONTEXT_CONFIG", "Summary target must be below its trigger")


@dataclass(frozen=True)
class ContextPolicyV3(ContextPolicyV2):
    """v2 plus deterministic tool-result masking and bounded degradation.

    Masking only replaces older host-produced tool results that carry a saved
    artifact reference; the full result stays retrievable with artifact_read.
    ``replayed_reasoning="ignore"`` is an explicit per-model choice for gateways
    whose usage evidence shows replayed native reasoning is not billed as input.
    """
    expected_version: ClassVar[str] = "context-v3"
    version: str = "context-v3"
    # The frozen window/output check in context_budget still applies; 16K was
    # below the largest observed coding-task request (2026-11-11 diagnosis).
    input_target_tokens: int = 32_000
    keep_recent_tokens: int = 8_000
    replayed_reasoning: str = "count"
    mask_tool_results: bool = True
    mask_keep_recent_exchanges: int = 4
    # Opt in only: if hard admission still fails after ordinary masking, allow
    # saved completed results within the recent exchanges to become pointers.
    pressure_mask_keep_recent_exchanges: int | None = None
    summary_failure: str = "degrade"

    def __post_init__(self):
        super().__post_init__()
        if self.replayed_reasoning not in {"count", "ignore"}:
            raise ContextPolicyError("CONTEXT_CONFIG", "replayed_reasoning must be count or ignore")
        if type(self.mask_tool_results) is not bool:
            raise ContextPolicyError("CONTEXT_CONFIG", "mask_tool_results must be a boolean")
        if type(self.mask_keep_recent_exchanges) is not int or not 1 <= self.mask_keep_recent_exchanges <= 20:
            raise ContextPolicyError("CONTEXT_CONFIG", "mask_keep_recent_exchanges must be between 1 and 20")
        pressure_keep = self.pressure_mask_keep_recent_exchanges
        if pressure_keep is not None and (
            type(pressure_keep) is not int or not 0 <= pressure_keep < self.mask_keep_recent_exchanges
        ):
            raise ContextPolicyError(
                "CONTEXT_CONFIG", "pressure_mask_keep_recent_exchanges must be below mask_keep_recent_exchanges",
            )
        if self.summary_failure not in {"degrade", "fail"}:
            raise ContextPolicyError("CONTEXT_CONFIG", "summary_failure must be degrade or fail")


@dataclass(frozen=True)
class ModelContextLimits:
    context_window_tokens: int | None = None
    max_input_tokens: int | None = None
    source: str | None = None
    verified_at: str | None = None

    def __post_init__(self):
        for name in ("context_window_tokens", "max_input_tokens"):
            value = getattr(self, name)
            if value is not None:
                _positive(value, name)
        for name in ("source", "verified_at"):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), str):
                raise ContextPolicyError("CONTEXT_CONFIG", f"{name} must be text or null")


def parse_context_configuration(value: dict | None) -> tuple[ContextPolicy, ModelContextLimits]:
    """Validate the ModelConfig JSON field; old empty values use v1 defaults."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"schema_version", "policy", "model_limits"}:
        raise ContextPolicyError("CONTEXT_CONFIG", "Unsupported context configuration fields")
    version = value.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise ContextPolicyError("CONTEXT_CONFIG", "Unsupported context configuration schema")
    policy, limits = value.get("policy", {}), value.get("model_limits", {})
    if not isinstance(policy, dict) or not isinstance(limits, dict):
        raise ContextPolicyError("CONTEXT_CONFIG", "Context policy and model limits must be objects")
    try:
        policy_type = {"context-v2": ContextPolicyV2, "context-v3": ContextPolicyV3}.get(policy.get("version"),
                                                                                       ContextPolicy)
        return policy_type(**policy), ModelContextLimits(**limits)
    except TypeError:
        raise ContextPolicyError("CONTEXT_CONFIG", "Unsupported context policy or limit fields") from None


def context_policy_dict(policy: ContextPolicy) -> dict:
    """Canonical frozen policy, preserving pre-extension V3 summary identities."""
    data = asdict(policy)
    if isinstance(policy, ContextPolicyV3) and policy.pressure_mask_keep_recent_exchanges is None:
        data.pop("pressure_mask_keep_recent_exchanges")
    return data


def context_configuration_dict(policy: ContextPolicy, limits: ModelContextLimits) -> dict:
    return {"schema_version": 1, "policy": context_policy_dict(policy), "model_limits": asdict(limits)}


@dataclass(frozen=True)
class ContextBudget:
    input_limit: int
    summarize_at_tokens: int
    output_reservation: int
    provider_window_known: bool


def context_budget(policy: ContextPolicy, limits: ModelContextLimits, *, output_tokens: int) -> ContextBudget:
    """An input-only limit is not a total window and never loses output twice."""
    _positive(output_tokens, "output_tokens")
    candidates = [policy.input_target_tokens]
    if limits.max_input_tokens is not None:
        candidates.append(limits.max_input_tokens)
    if limits.context_window_tokens is not None:
        available = limits.context_window_tokens - output_tokens - policy.window_margin_tokens
        if available <= 0:
            raise ContextPolicyError(
                "CONTEXT_WINDOW_CONFLICT",
                f"Model window {limits.context_window_tokens} cannot reserve output {output_tokens} "
                f"and margin {policy.window_margin_tokens}; output configuration was not changed",
            )
        candidates.append(available)
    available = min(candidates)
    return ContextBudget(available, max(1, math.floor(available * policy.summarize_at)), output_tokens,
                         limits.context_window_tokens is not None)


class ContextTokenCounter(Protocol):
    method: str
    estimated: bool

    def count(self, messages: list[BaseMessage], *, tools: tuple[dict, ...] = ()) -> int: ...


@dataclass(frozen=True)
class ApproximateContextCounter:
    """Conservative local fallback, not an upstream tokenizer or a bill estimate.

    One character per token avoids the SDK's English-oriented four-character
    assumption for Chinese. Callers may inject an already available local
    provider tokenizer; this fallback performs no download or network call.
    """
    method: str = "langchain-approximate-chars1-v1"
    estimated: bool = True

    def count(self, messages: list[BaseMessage], *, tools: tuple[dict, ...] = ()) -> int:
        return count_tokens_approximately(messages, chars_per_token=1.0, tools=list(tools))


@dataclass(frozen=True)
class UnicodeContextCounter:
    """A frozen heuristic, never a tokenizer or an invoice estimate.

    Count native reasoning only for admission checks; no text is returned or
    persisted. Summary input still uses the separate public message projection.
    """
    method: str = "unicode-weighted-v2"
    estimated: bool = True
    # context-v3 may exclude replayed native reasoning when a gateway does not
    # bill it as input. Provider usage remains the final evidence.
    include_replayed_reasoning: bool = True

    def count(self, messages: list[BaseMessage], *, tools: tuple[dict, ...] = ()) -> int:
        def weight(value):
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
            return sum(1 / 3 if ord(char) < 128 else 1 if (
                0x3400 <= ord(char) <= 0x9FFF or 0xF900 <= ord(char) <= 0xFAFF
                or 0x20000 <= ord(char) <= 0x323AF) else len(char.encode("utf-8")) for char in text)
        total = weight(list(tools)) if tools else 0
        for message in messages:
            total += 6 + weight(message.content) + weight(message.type)
            if message.name:
                total += weight(message.name)
            if getattr(message, "tool_calls", None):
                total += weight(message.tool_calls)
            if getattr(message, "tool_call_id", None):
                total += weight(message.tool_call_id)
            if self.include_replayed_reasoning:
                for key in ("reasoning_content", "reasoning", "reasoning_text", "reasoning_details"):
                    if key in message.additional_kwargs:
                        total += weight(message.additional_kwargs[key])
        return math.ceil(total)


def counter_for_policy(policy: ContextPolicy) -> ContextTokenCounter:
    if isinstance(policy, ContextPolicyV3) and policy.replayed_reasoning == "ignore":
        return UnicodeContextCounter(method="unicode-weighted-v3-no-replayed-reasoning",
                                     include_replayed_reasoning=False)
    return UnicodeContextCounter() if isinstance(policy, ContextPolicyV2) else ApproximateContextCounter()
