"""Narrow extension of the pinned Chat Completions adapter (langchain-openai 1.7.4).

The SDK keeps transport, tools, structured output and cancellation. These hooks
preserve fields it deliberately discards for third-party compatible providers.
No global monkey patch or alternate execution engine is used.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI

_NATIVE_REASONING = ("reasoning_content", "reasoning", "reasoning_text", "reasoning_details")
_USAGE_VALIDITY = "_omni_usage_validity"
_TERMINAL_REASONS = {"stop", "tool_calls", "function_call", "length", "content_filter"}


def _valid_cumulative_usage(usage) -> bool:
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not isinstance(usage, dict) or not all(type(usage.get(key)) is int and usage[key] >= 0 for key in keys):
        return False
    if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
        return False
    for detail, parent in (("prompt_tokens_details", "prompt_tokens"),
                           ("completion_tokens_details", "completion_tokens")):
        values = usage.get(detail)
        if values is not None and (not isinstance(values, dict) or any(
            value is not None and (type(value) is not int or not 0 <= value <= usage[parent])
            for value in values.values()
        )):
            return False
    return True


class _CumulativeStream:
    """One request's usage ledger; never shared by parallel calls on a model."""

    def __init__(self):
        self.usage = None
        self.invalid_usage = False
        self.finish = None
        self.terminal_usage = False

    def accept(self, chunk):
        info = chunk.generation_info or {}
        valid = info.pop(_USAGE_VALIDITY, None)
        usage, chunk.message.usage_metadata = chunk.message.usage_metadata, None
        # Only the normalized final usage may survive SDK aggregation. Raw
        # snapshots must not provide a second accounting path in metadata.
        info.pop("token_usage", None)
        chunk.message.response_metadata.pop("token_usage", None)
        message_finish = chunk.message.response_metadata.pop("finish_reason", None)
        finish = info.pop("finish_reason", None) or message_finish
        if finish is not None:
            if finish not in _TERMINAL_REASONS or (self.finish is not None and finish != self.finish):
                self._incomplete()
            self.finish = finish
        if valid is False:
            self.invalid_usage = True
        if usage is not None:
            self.terminal_usage = self.terminal_usage or self.finish is not None
            if self.usage is not None:
                keys = ("input_tokens", "output_tokens", "total_tokens")
                if any(usage[key] < self.usage[key] for key in keys):
                    self.invalid_usage = True
                for details in ("input_token_details", "output_token_details"):
                    if any(usage.get(details, {}).get(key, -1) < value
                           for key, value in self.usage.get(details, {}).items()):
                        self.invalid_usage = True
            self.usage = deepcopy(usage)
        chunk.generation_info = info or None
        return chunk

    @staticmethod
    def _incomplete():
        from .providers import ProviderError

        raise ProviderError("OUTPUT_INCOMPLETE", "模型流缺少有效结束标记，结果仍未知。")

    def final(self):
        if self.finish is None:
            self._incomplete()
        usage = self.usage if self.terminal_usage and not self.invalid_usage else None
        # A single final increment avoids double counting cumulative frames,
        # and allows a later malformed/decreasing frame to invalidate usage.
        return ChatGenerationChunk(
            message=AIMessageChunk(content="", usage_metadata=usage,
                                   response_metadata={"omni_stream_usage_status": "complete" if usage else "unknown",
                                                      "omni_transport": "compatible-native-stream-v1"}),
            generation_info={"finish_reason": self.finish},
        )


class CompatibleChatModel(ChatOpenAI):
    # Opt in only for a verified, frozen transport. Existing chat streams keep
    # their cumulative-usage contract and their external max-based accounting.
    omni_normalize_stream_usage: bool = False

    @property
    def _origin(self) -> dict[str, str]:
        # Nested `id` merges idempotently when LangChain joins streamed chunks.
        identity = f"{str(self.openai_api_base).rstrip('/')}\0{self.model_name}"
        return {"id": hashlib.sha256(identity.encode()).hexdigest()}

    def _preserve(self, message, raw):
        for key in (*_NATIVE_REASONING, "refusal"):
            value = raw.get(key)
            if isinstance(value, str) or (key == "reasoning_details" and isinstance(value, list)):
                message.additional_kwargs[key] = deepcopy(value)
        message.response_metadata["omni_require_finish"] = True
        message.response_metadata["omni_compatible_origin"] = self._origin

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        usage_valid = None
        if self.omni_normalize_stream_usage and chunk.get("usage") is not None:
            usage_valid = _valid_cumulative_usage(chunk["usage"])
            if not usage_valid:
                chunk = {**chunk, "usage": None}
        choices = chunk.get("choices") or []
        if choices and choices[0].get("delta") is None and choices[0].get("finish_reason"):
            chunk = {**chunk, "choices": [{**choices[0], "delta": {}}, *choices[1:]]}
        result = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if result is not None:
            choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
            self._preserve(result.message, (choices[0].get("delta") or {}) if choices else {})
            if self.omni_normalize_stream_usage:
                result.generation_info = {**(result.generation_info or {}), _USAGE_VALIDITY: usage_valid}
        return result

    def _stream(self, *args, **kwargs):
        state = _CumulativeStream() if self.omni_normalize_stream_usage else None
        for chunk in super()._stream(*args, **kwargs):
            yield state.accept(chunk) if state is not None else chunk
        if state is not None:
            final = state.final()
            self._preserve(final.message, {})
            yield final

    async def _astream(self, *args, **kwargs):
        state = _CumulativeStream() if self.omni_normalize_stream_usage else None
        async for chunk in super()._astream(*args, **kwargs):
            yield state.accept(chunk) if state is not None else chunk
        if state is not None:
            final = state.final()
            self._preserve(final.message, {})
            yield final

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        raw = response if isinstance(response, dict) else response.model_dump(
            exclude={"choices": {"__all__": {"message": {"parsed"}}}}, warnings=False)
        for generation, choice in zip(result.generations, raw.get("choices", []), strict=True):
            self._preserve(generation.message, choice.get("message") or {})
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        # Generic compatible gateways use max_tokens; the ChatOpenAI subclass
        # otherwise renames it based on the official OpenAI API evolution.
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        for original, wire in zip(messages, payload.get("messages", []), strict=True):
            if not isinstance(original, AIMessage):
                continue
            for key in _NATIVE_REASONING:
                wire.pop(key, None)
            # Native reasoning is replayed only to the same endpoint and model,
            # and only for a tools request whose protocol may require it.
            if payload.get("tools") and original.response_metadata.get("omni_compatible_origin") == self._origin:
                for key in _NATIVE_REASONING:
                    if key in original.additional_kwargs:
                        wire[key] = deepcopy(original.additional_kwargs[key])
        return payload
