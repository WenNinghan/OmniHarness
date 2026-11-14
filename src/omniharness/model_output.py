"""Provider output channels and completion rules shared by chat and agents.

Only explicit protocol fields designate reasoning. Natural language and XML-like
strings in answer text are never reclassified. Opaque reasoning signatures stay
in their native message and are not exposed as display text.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_TRUNCATED_REASONS = {"length", "max_tokens", "max_output_tokens", "max-tokens"}


def is_truncated(finish_reason: str | None) -> bool:
    return isinstance(finish_reason, str) and finish_reason.lower() in _TRUNCATED_REASONS


class TruncatedEchoBuffer:
    """Delay only a potential verbatim copy of explicitly supplied reasoning.

    One verified compatible gateway copies its entire reasoning into `content`
    when the output budget is exhausted. Wait while the initial answer and
    reasoning are prefixes of each other. Once they diverge, release the answer
    and remain transparent. Suppress a copy only after an explicit truncation
    with exact full equality. Normal completion and exceptions release it.
    """

    def __init__(self):
        self.reasoning = ""
        self.pending = ""
        self.committed = False

    def feed(self, text: str, reasoning: str) -> str:
        if self.committed:
            return text
        self.reasoning += reasoning
        self.pending += text
        if not self.pending:
            return ""
        if self.reasoning and (self.reasoning.startswith(self.pending) or self.pending.startswith(self.reasoning)):
            return ""
        self.committed = True
        self.reasoning = ""
        return self.flush()

    def flush(self, finish_reason: str | None = None) -> str:
        text, self.pending = self.pending, ""
        if is_truncated(finish_reason) and self.reasoning and text == self.reasoning:
            return ""
        return text


def answer_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(item["text"] for item in content if isinstance(item, dict)
                       and item.get("type") in {"text", "output_text"}
                       and item.get("thought") is not True and isinstance(item.get("text"), str))
    return ""


def _reasoning_blocks(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    pieces = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in {"reasoning", "thinking", "reasoning.text", "reasoning.summary"} or item.get("thought") is True:
            value = next((item[key] for key in ("reasoning", "thinking", "text", "summary")
                          if isinstance(item.get(key), str)), None)
            if value is not None:
                pieces.append(value)
            elif isinstance(item.get("summary"), list):
                pieces.extend(part["text"] for part in item["summary"] if isinstance(part, dict)
                              and part.get("type") == "summary_text" and isinstance(part.get("text"), str))
    return "".join(pieces)


@dataclass(frozen=True)
class ParsedOutput:
    text: str
    reasoning: str
    finish_reason: str | None
    refusal: bool = False


def extract_output(message: Any, *, deduplicate_echo: bool = True) -> ParsedOutput:
    content = getattr(message, "content", "")
    extra = getattr(message, "additional_kwargs", None) or {}
    metadata = getattr(message, "response_metadata", None) or {}
    # Several gateways send both the reasoning and a redundant summary. Choose
    # one explicit representation instead of displaying the same content twice.
    reasoning = next((extra[key] for key in ("reasoning_content", "reasoning", "reasoning_text")
                      if isinstance(extra.get(key), str) and extra[key]), "")
    reasoning = reasoning or _reasoning_blocks(content) or _reasoning_blocks(extra.get("reasoning_details"))
    finish = metadata.get("finish_reason") or metadata.get("stop_reason")
    if not finish and metadata.get("status") == "incomplete":
        details = metadata.get("incomplete_details") or {}
        finish = details.get("reason") or "incomplete"
    if not finish and metadata.get("status") == "completed":
        finish = "stop"
    refusal = bool(extra.get("refusal")) or (
        isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "refusal"
                                         for part in content))
    finish = str(finish) if finish is not None else None
    text = answer_text(content)
    # Apply this only to whole responses, never individual stream fragments.
    # The original reasoning remains available in its own channel.
    if deduplicate_echo and is_truncated(finish) and reasoning and text == reasoning:
        text = ""
    return ParsedOutput(text, reasoning, finish, refusal)


@dataclass(frozen=True)
class OutputIssue:
    status: str
    code: str
    message: str


def output_issue(*, text: str, reasoning: str = "", finish_reason: str | None = None,
                 refusal: bool = False, require_finish: bool = False,
                 allow_tools: bool = False, has_tools: bool = False) -> OutputIssue | None:
    reason = finish_reason.lower() if isinstance(finish_reason, str) else None
    if is_truncated(reason):
        return OutputIssue("truncated", "OUTPUT_TRUNCATED",
                           "模型已达到输出上限，回答尚未完成。已保留收到的内容；可调整模型输出上限后重新发送。")
    if refusal or reason in {"content_filter", "safety", "recitation", "blocklist", "prohibited_content", "refusal"}:
        return OutputIssue("filtered", "OUTPUT_FILTERED", "模型拒绝或过滤了此次输出，未得到完整回答。")
    if reason in {"tool_calls", "function_call", "tool_use", "tool-calls"} or has_tools:
        if allow_tools and has_tools:
            return None
        return OutputIssue("incomplete", "UNEXPECTED_TOOL_CALL", "模型返回了工具调用，本次请求未得到最终回答。")
    if reason and reason not in {"stop", "end_turn", "stop_sequence", "completed"}:
        return OutputIssue("interrupted", "OUTPUT_INCOMPLETE", "模型输出未正常结束，已保留收到的内容。")
    if require_finish and not reason:
        return OutputIssue("interrupted", "OUTPUT_INCOMPLETE", "模型响应缺少结束标记，已保留收到的内容。")
    if not text.strip():
        if reasoning:
            return OutputIssue("reasoning_only", "REASONING_ONLY",
                               "模型只返回了思考过程，未提供最终回答。可调整输出上限后重新发送。")
        return OutputIssue("empty", "EMPTY_RESPONSE", "模型未返回正式回答或可显示的思考内容。")
    return None


def require_complete_output(message: Any, *, require_finish: bool = False, allow_tools: bool = False) -> str:
    from .providers import ProviderError

    output = extract_output(message)
    metadata = getattr(message, "response_metadata", None) or {}
    issue = output_issue(text=output.text, reasoning=output.reasoning, finish_reason=output.finish_reason,
                         refusal=output.refusal, require_finish=require_finish or bool(metadata.get("omni_require_finish")),
                         allow_tools=allow_tools, has_tools=bool(getattr(message, "tool_calls", None)))
    if issue:
        raise ProviderError(issue.code, issue.message)
    return output.text
