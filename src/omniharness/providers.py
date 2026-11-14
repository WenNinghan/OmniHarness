"""Provider boundary: explicit credentials, tested capabilities, safe public errors.

No provider exception text or response body crosses this module. LangSmith cloud
tracing is disabled here; model calls go to the endpoint selected by the user.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import math
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from .model_output import (
    TruncatedEchoBuffer,
    answer_text,
    extract_output,
    output_issue,
    require_complete_output,
)


@dataclass
class RuntimeModelConfig:
    id: str
    adapter_type: str
    model_name: str
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    parameters: dict = field(default_factory=dict)
    timeout_seconds: int = 90
    max_output_tokens: int = 100_000


@dataclass
class ModelDelta:
    text: str = ""
    usage: dict | None = None
    reasoning: str = ""
    finish_reason: str | None = None
    output_status: str | None = None


class ProviderError(Exception):
    def __init__(self, code: str, public_message: str):
        self.code = code
        self.public_message = public_message
        super().__init__(public_message)


_ADAPTERS = {"openai", "openai-compatible", "anthropic", "gemini", "mock"}
_PROBES = {"chat", "streaming", "tools", "structured", "summary_non_thinking"}
_ALLOWED = {
    "openai": {"temperature", "top_p", "seed", "reasoning_effort"},
    "openai-compatible": {"temperature", "top_p", "seed", "thinking"},
    "anthropic": {"temperature", "top_p", "top_k"},
    "gemini": {"temperature", "top_p", "top_k"},
    "mock": set(),
}


def _safe_error(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    # Status and type are sufficient for actionable errors. Never interpolate
    # str(exc), request URLs, response bodies, or authentication headers.
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__.lower()
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return ProviderError("DEPENDENCY_MISSING", "模型适配器依赖未安装，请检查安装环境。")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in name:
        return ProviderError("PROVIDER_TIMEOUT", "模型请求超时，请重试或调整超时设置。")
    if status in (401, 403) or "authentication" in name or "permissiondenied" in name:
        return ProviderError("PROVIDER_AUTH", "模型认证或权限检查失败，请检查密钥和模型权限。")
    if status == 429 or "ratelimit" in name:
        return ProviderError("PROVIDER_RATE_LIMIT", "模型服务达到配额或速率限制，请稍后重试。")
    if status == 404 or "notfound" in name:
        return ProviderError("PROVIDER_NOT_FOUND", "未找到模型或接口，请检查模型名称与服务地址。")
    if status in (400, 422) or "badrequest" in name or "invalidrequest" in name:
        return ProviderError("PROVIDER_REQUEST_REJECTED", "模型服务拒绝请求，请检查模型能力和参数。")
    if isinstance(status, int) and status >= 500:
        return ProviderError("PROVIDER_UNAVAILABLE", "模型服务暂时不可用，请稍后重试。")
    if "connection" in name or "connecterror" in name:
        return ProviderError("PROVIDER_CONNECTION", "无法连接模型服务，请检查网络和服务地址。")
    return ProviderError("PROVIDER_ERROR", "模型调用失败；原始服务错误已隐藏以保护凭据。")


def _validate_config(config: RuntimeModelConfig) -> dict[str, Any]:
    if config.adapter_type not in _ADAPTERS:
        raise ProviderError("INVALID_ADAPTER", "不支持的模型适配器。")
    if not isinstance(config.model_name, str) or not config.model_name.strip():
        raise ProviderError("INVALID_MODEL", "模型名称不能为空。")
    for value, upper in ((config.timeout_seconds, 3600), (config.max_output_tokens, 1048576)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise ProviderError("INVALID_LIMIT", "超时与输出上限必须是允许范围内的正整数。")
    if config.base_url:
        try:
            parts = urlsplit(config.base_url)
            valid = (
                parts.scheme in ("http", "https")
                and parts.hostname
                and not parts.username
                and not parts.password
                and not parts.query
                and not parts.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ProviderError("INVALID_ENDPOINT", "服务地址必须是无凭据、查询参数和片段的 HTTP(S) 地址。")
    if config.adapter_type == "openai-compatible" and not config.base_url:
        raise ProviderError("MISSING_ENDPOINT", "兼容适配器必须明确配置服务地址。")
    if config.adapter_type not in ("mock", "openai-compatible") and not config.api_key:
        raise ProviderError("MISSING_API_KEY", "此模型未配置 API Key；不会自动使用模拟模型。")
    return validate_model_parameters(config.adapter_type, config.parameters)


def validate_model_parameters(adapter_type: str, parameters: dict) -> dict[str, Any]:
    """Validate editable settings without requiring a saved credential or endpoint.

    API schemas and runtime adapters share this rule; saving an incomplete model
    configuration must not require a network request or a decrypted credential.
    """
    if adapter_type not in _ALLOWED:
        raise ProviderError("INVALID_ADAPTER", "不支持的模型适配器。")
    if not isinstance(parameters, dict) or set(parameters) - _ALLOWED[adapter_type]:
        raise ProviderError("INVALID_PARAMETERS", "存在不支持的模型参数，请检查适配器参数白名单。")
    params = dict(parameters)
    for key, value in params.items():
        if key in ("temperature", "top_p"):
            ceiling = 1 if key == "top_p" or adapter_type == "anthropic" else 2
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= ceiling
            ):
                raise ProviderError("INVALID_PARAMETERS", "采样参数超出允许范围。")
        elif key in ("seed", "top_k"):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2147483647:
                raise ProviderError("INVALID_PARAMETERS", "整数采样参数超出允许范围。")
        elif key == "reasoning_effort" and (
            not isinstance(value, str) or value not in {"none", "minimal", "low", "medium", "high", "xhigh"}
        ):
            raise ProviderError("INVALID_PARAMETERS", "不支持的推理强度。")
        elif key == "thinking" and (not isinstance(value, dict) or set(value) != {"type"}
                                     or value["type"] not in ("enabled", "disabled")):
            raise ProviderError("INVALID_PARAMETERS", "thinking 仅支持明确的 enabled 或 disabled。")
    return params


def validate_config(config: RuntimeModelConfig) -> dict[str, Any]:
    """Validate a runtime configuration without constructing a client or doing I/O."""
    return _validate_config(config)


NATIVE_AGENT_STREAM = "compatible-native-stream-v1"


def agent_streaming_enabled(snapshot: dict, workflow_version: str, config=None) -> bool:
    """Only a frozen, exact compatible tool probe enables the new transport."""
    if workflow_version not in {"stage-agent-v4", "stage-agent-v5", "stage-agent-v6"} or snapshot.get("adapter_type") != "openai-compatible":
        return False
    tools = (snapshot.get("capabilities") or {}).get("tools") or {}
    return (tools.get("status") == "supported" and tools.get("transport") == NATIVE_AGENT_STREAM
            and tools.get("transport_binding") == summary_capability_binding(snapshot)
            and (config is None or tools["transport_binding"] == summary_capability_binding(config)))


def build_chat_model(config: RuntimeModelConfig, *, http_async_client=None, native_streaming=False) -> Any:
    """Build an official LangChain model without ambient credential fallback."""
    params = _validate_config(config)
    if native_streaming and config.adapter_type != "openai-compatible":
        raise ProviderError("INVALID_PARAMETERS", "原生工具流式模式仅适用于已验证的兼容接口。")
    if config.adapter_type == "mock":
        raise ProviderError("MOCK_NOT_LIVE", "模拟模式不创建真实模型客户端。")
    try:
        if config.adapter_type in ("openai", "openai-compatible"):
            thinking = params.pop("thinking", None)
            if thinking is not None:
                params["extra_body"] = {"thinking": deepcopy(thinking)}
            if native_streaming:
                params.update(streaming=True, omni_normalize_stream_usage=True,
                              stream_chunk_timeout=config.timeout_seconds)
            cls = (importlib.import_module("omniharness.compatible_model").CompatibleChatModel
                   if config.adapter_type == "openai-compatible"
                   else importlib.import_module("langchain_openai").ChatOpenAI)
            return cls(
                model=config.model_name,
                api_key=config.api_key or "omniharness-no-key-configured",
                base_url=config.base_url or "https://api.openai.com/v1",
                timeout=config.timeout_seconds,
                max_tokens=config.max_output_tokens,
                max_retries=0,
                use_responses_api=config.adapter_type == "openai",
                stream_usage=True,
                http_async_client=http_async_client,
                **params,
            )
        if config.adapter_type == "anthropic":
            cls = importlib.import_module("langchain_anthropic").ChatAnthropic
            return cls(
                model=config.model_name,
                api_key=config.api_key,
                base_url=config.base_url or "https://api.anthropic.com",
                timeout=config.timeout_seconds,
                max_tokens=config.max_output_tokens,
                max_retries=0,
                **params,
            )
        cls = importlib.import_module("langchain_google_genai").ChatGoogleGenerativeAI
        return cls(
            model=config.model_name,
            api_key=config.api_key,
            vertexai=False,
            base_url=config.base_url or "https://generativelanguage.googleapis.com",
            timeout=config.timeout_seconds,
            max_output_tokens=config.max_output_tokens,
            max_retries=0,
            **params,
        )
    except Exception as exc:
        raise _safe_error(exc) from None


def _text(content: Any) -> str:
    return answer_text(content)


def _messages(messages: list[dict]) -> list[tuple[str, str]]:
    result = []
    for message in messages:
        if message.get("role") not in {"system", "user", "assistant"} or not isinstance(
            message.get("content"), str
        ):
            raise ProviderError("INVALID_MESSAGES", "消息角色或文本格式无效。")
        result.append((message["role"], message["content"]))
    if not result:
        raise ProviderError("INVALID_MESSAGES", "聊天消息不能为空。")
    return result


async def stream_chat(config: RuntimeModelConfig, messages: list[dict]) -> AsyncIterator[ModelDelta]:
    """Yield separate display channels, usage and an explicit completion state."""
    _validate_config(config)
    formatted = _messages(messages)
    if config.adapter_type == "mock":
        response = "[模拟] 这是离线演示回复，未调用真实模型。你发送了：" + formatted[-1][1]
        for start in range(0, len(response), 12):
            await asyncio.sleep(0.01)
            yield ModelDelta(text=response[start : start + 12])
        yield ModelDelta(finish_reason="stop", output_status="complete")
        return
    finish_reason = None
    echo_buffer = TruncatedEchoBuffer()
    try:
        from langchain_core.language_models import BaseChatModel

        model = build_chat_model(config)
        tracing_context = importlib.import_module("langsmith").tracing_context
        usage: dict[str, int | None] = {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost": None,
        }
        saw_text = saw_reasoning = refusal = has_tools = False
        require_finish = isinstance(model, BaseChatModel)
        with tracing_context(enabled=False):
            async with asyncio.timeout(config.timeout_seconds):
                async for chunk in model.astream(formatted):
                    output = extract_output(chunk, deduplicate_echo=False)
                    reasoning = output.reasoning
                    text = echo_buffer.feed(output.text, reasoning)
                    saw_text = saw_text or bool(text.strip())
                    saw_reasoning = saw_reasoning or bool(reasoning)
                    refusal = refusal or output.refusal
                    has_tools = has_tools or bool(getattr(chunk, "tool_calls", None))
                    finish_reason = output.finish_reason or finish_reason
                    require_finish = require_finish or bool(
                        getattr(chunk, "response_metadata", {}).get("omni_require_finish"))
                    metadata = getattr(chunk, "usage_metadata", None)
                    changed = False
                    if isinstance(metadata, dict):
                        for key in ("input_tokens", "output_tokens", "total_tokens"):
                            value = metadata.get(key)
                            # Gemini's SDK can emit a negative final input delta
                            # when the provider corrects its cumulative prompt
                            # count. It is a correction, not negative token use.
                            correction = config.adapter_type == "gemini" and key == "input_tokens"
                            if isinstance(value, int) and not isinstance(value, bool) and (value >= 0 or correction):
                                # Chat Completions usage is a cumulative snapshot;
                                # repeated usage frames must not double the bill.
                                next_value = (max(usage[key] or 0, value) if config.adapter_type == "openai-compatible"
                                              else max(0, (usage[key] or 0) + value))
                                changed = changed or usage[key] != next_value
                                usage[key] = next_value
                    if text or reasoning or changed:
                        yield ModelDelta(text=text, reasoning=reasoning, usage=dict(usage) if changed else None)
        pending_text = echo_buffer.flush(finish_reason)
        if pending_text:
            saw_text = saw_text or bool(pending_text.strip())
            yield ModelDelta(text=pending_text)
        issue = output_issue(text="answer" if saw_text else "", reasoning="reasoning" if saw_reasoning else "",
                             finish_reason=finish_reason, refusal=refusal, has_tools=has_tools,
                             require_finish=require_finish)
        yield ModelDelta(usage=dict(usage), finish_reason=finish_reason,
                         output_status=issue.status if issue else "complete")
        if issue:
            raise ProviderError(issue.code, issue.message)
    except Exception as exc:
        # An interrupted stream cannot prove an exact truncated echo; keep all
        # candidate content, even when it happened to match a reasoning prefix.
        if pending_text := echo_buffer.flush():
            yield ModelDelta(text=pending_text)
        raise _safe_error(exc) from None


class _StructuredAnswer(BaseModel):
    answer: Literal[42]


def _probe_add(a: int, b: int) -> int:
    """Add two integers for a harmless capability test."""
    return a + b


async def _run_probe(model: Any, adapter: str, probe: str) -> tuple[bool, str]:
    if probe == "chat":
        result = await model.ainvoke([("user", "Reply with the single word OK.")])
        return bool(require_complete_output(result)), "已收到实际模型文本响应。"
    if probe == "streaming":
        from langchain_core.language_models import BaseChatModel

        count = 0
        finish_reason, saw_reasoning, refusal = None, False, False
        async for chunk in model.astream([("user", "Write one short sentence about the sky.")]):
            output = extract_output(chunk, deduplicate_echo=False)
            if output.text.strip():
                count += 1
            finish_reason = output.finish_reason or finish_reason
            saw_reasoning = saw_reasoning or bool(output.reasoning)
            refusal = refusal or output.refusal
        issue = output_issue(text="answer" if count else "", reasoning="reasoning" if saw_reasoning else "",
                             finish_reason=finish_reason, refusal=refusal,
                             require_finish=isinstance(model, BaseChatModel))
        if issue:
            raise ProviderError(issue.code, issue.message)
        return count > 0, "已从模型流式接口收到文本分片。"
    if probe == "structured":
        method = "function_calling" if adapter == "anthropic" else "json_schema"
        structured = model.with_structured_output(_StructuredAnswer, method=method)
        result = await structured.ainvoke([("user", "Return answer=42 using the supplied schema.")])
        answer = _StructuredAnswer.model_validate(result)
        return answer.answer == 42, "实际结构化输出已通过 Schema 校验。"
    from langchain_core.messages import HumanMessage, ToolMessage

    first = HumanMessage(
        content="You must call _probe_add with a=19 and b=23. Do not calculate without the tool."
    )
    # Compatible thinking endpoints may reject forced "required" selection
    # while supporting normal tool use. Test the same auto mode as the Agent
    # loop; the actual call, exact arguments and result round trip still have
    # to succeed before we report tools supported.
    bound = model.bind_tools([_probe_add], tool_choice="auto" if adapter == "openai-compatible" else "any")
    reply = await bound.ainvoke([first])
    require_complete_output(reply, allow_tools=True)
    calls = getattr(reply, "tool_calls", [])
    if len(calls) != 1 or calls[0].get("name") != "_probe_add":
        return False, "模型未按要求返回工具调用。"
    call = calls[0]
    args = call.get("args", {})
    if args != {"a": 19, "b": 23} or not call.get("id"):
        return False, "工具调用参数或调用 ID 未通过校验。"
    output = _probe_add(**args)
    # Bind without forced tool_choice for the second leg, so it can consume the
    # tool result and answer. Merely finding bind_tools is not a passing probe.
    final = await model.bind_tools([_probe_add]).ainvoke(
        [
            first,
            reply,
            ToolMessage(content=str(output), tool_call_id=call["id"]),
        ]
    )
    return "42" in require_complete_output(final), "已完成模型调用工具、回传结果和模型读取结果的完整往返。"


async def probe_model(config: RuntimeModelConfig, probes: list[str]) -> dict:
    if any(probe not in _PROBES for probe in probes):
        raise ProviderError("INVALID_PROBE", "不支持的模型能力测试。")
    _validate_config(config)
    if config.adapter_type == "mock":
        return {
            probe: {"status": "unsupported", "message": "[模拟] 离线演示不证明真实模型的任何能力。"}
            for probe in probes
        }
    results = {}
    for probe in dict.fromkeys(probes):
        if probe == "summary_non_thinking":
            results[probe] = await _probe_summary_non_thinking(config)
            continue
        try:
            native_streaming = probe == "tools" and config.adapter_type == "openai-compatible"
            model = build_chat_model(config, native_streaming=True) if native_streaming else build_chat_model(config)
            tracing_context = importlib.import_module("langsmith").tracing_context
            with tracing_context(enabled=False):
                async with asyncio.timeout(config.timeout_seconds):
                    supported, message = await _run_probe(model, config.adapter_type, probe)
            results[probe] = {
                "status": "supported" if supported else "unsupported",
                "message": message if supported or probe == "tools" else "实际探针未得到符合要求的结果。",
            }
            if native_streaming and supported:
                results[probe].update(transport=NATIVE_AGENT_STREAM,
                                      transport_binding=summary_capability_binding(config))
        except NotImplementedError:
            results[probe] = {"status": "unsupported", "message": "此适配器未实现该能力。"}
        except Exception as exc:
            error = _safe_error(exc)
            results[probe] = {"status": "error", "message": error.public_message}
    return results


def summary_capability_binding(config: RuntimeModelConfig | dict) -> str:
    def get(name, default=None):
        return config.get(name, default) if isinstance(config, dict) else getattr(config, name, default)
    identity = {name: get(name) for name in ("id", "adapter_type", "model_name")}
    identity.update(base_url=str(get("base_url") or "").rstrip("/"), parameters=get("parameters", {}))
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def freeze_summary_capability(snapshot: dict, capability: dict | None) -> dict:
    """Freeze only bounded evidence tied to the exact endpoint/model/parameters."""
    binding = summary_capability_binding(snapshot)
    unknown = {"schema_version": 1, "status": "unknown", "binding": binding,
               "parameter_accepted": None, "non_thinking_observed": None}
    if (not isinstance(capability, dict) or type(capability.get("schema_version")) is not int
            or capability.get("schema_version") != 1
            or capability.get("binding") != binding or snapshot.get("adapter_type") != "openai-compatible"):
        return unknown
    if (not isinstance(capability.get("status"), str)
            or capability.get("status") not in {"supported", "unsupported", "unknown", "error"}
            or (capability.get("observation_basis") is not None
                and not isinstance(capability.get("observation_basis"), str))
            or capability.get("observation_basis") not in {
                None, "reasoning_observed", "reported_zero_reasoning", "explicit_empty_reasoning"}):
        return unknown
    frozen = {key: capability[key] for key in ("schema_version", "status", "binding")}
    for key in ("parameter_accepted", "non_thinking_observed"):
        value = capability.get(key)
        frozen[key] = value if type(value) is bool else None
    if isinstance(capability.get("checked_at"), str):
        frozen["checked_at"] = capability["checked_at"][:64]
    frozen["observation_basis"] = capability.get("observation_basis")
    if type(capability.get("model_calls")) is int and capability["model_calls"] in (0, 1):
        frozen["model_calls"] = capability["model_calls"]
    usage = capability.get("usage")
    frozen["usage"] = ({key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")
                        if type(usage.get(key)) is int and usage[key] >= 0} if isinstance(usage, dict) else None)
    if (frozen.get("status") == "supported" and (frozen.get("parameter_accepted") is not True
            or frozen.get("non_thinking_observed") is not True or frozen.get("observation_basis") not in {
                "reported_zero_reasoning", "explicit_empty_reasoning"})):
        return unknown
    return frozen


async def _probe_summary_non_thinking(config: RuntimeModelConfig) -> dict:
    result = {"schema_version": 1, "status": "unknown", "binding": summary_capability_binding(config),
              "parameter_accepted": None, "non_thinking_observed": None,
              "checked_at": datetime.now(timezone.utc).isoformat(), "model_calls": 0, "usage": None}
    if config.adapter_type != "openai-compatible":
        return {**result, "status": "unsupported", "message": "此窄探针仅适用于显式兼容端点。"}
    # One bounded request, no tools/retry/model switching and no response text saved.
    probe = replace(config, parameters={**config.parameters, "thinking": {"type": "disabled"}},
                    max_output_tokens=512, timeout_seconds=min(config.timeout_seconds, 60))
    try:
        model = build_chat_model(probe)
        result["model_calls"] = 1
        with importlib.import_module("langsmith").tracing_context(enabled=False):
            async with asyncio.timeout(probe.timeout_seconds):
                message = await model.ainvoke([("user", "Summarize in one sentence: the file was read; the test passed. No analysis.")])
        result["parameter_accepted"] = True
        output = extract_output(message)
        usage = getattr(message, "usage_metadata", None) or {}
        result["usage"] = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")
                           if type(usage.get(key)) is int and usage[key] >= 0} or None
        require_complete_output(message, require_finish=True)
        reasoning = (usage.get("output_token_details") or {}).get("reasoning")
        raw = getattr(message, "additional_kwargs", {})
        empty_field = any(key in raw and raw[key] == "" for key in ("reasoning_content", "reasoning_text"))
        if (type(reasoning) is int and reasoning > 0) or output.reasoning:
            result.update(status="unsupported", non_thinking_observed=False,
                          observation_basis="reasoning_observed")
        elif (type(reasoning) is int and reasoning == 0) or empty_field:
            result.update(status="supported", non_thinking_observed=True,
                          observation_basis="reported_zero_reasoning" if reasoning == 0 else "explicit_empty_reasoning")
        result["message"] = "已观察到非思考摘要。" if result["status"] == "supported" else "参数已接受，但非思考效果未确认或未生效。"
    except Exception as exc:
        result.update(status="error", message=_safe_error(exc).public_message)
    return result
