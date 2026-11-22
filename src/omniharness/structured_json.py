"""Deterministic extraction of a single structured JSON payload without repair.

This helper accepts either raw JSON text or Markdown containing one unambiguous,
properly closed JSON code fence. It never guesses among multiple JSON blocks,
never extracts bare braces from prose, and never repairs malformed or truncated
model output.
"""
from __future__ import annotations

import re

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")
_INLINE_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(?:json)?[ \t]*(\{.*\})[ \t]*\1\s*$", re.IGNORECASE | re.DOTALL)


def extract_json_payload(content: str) -> str:
    """Return raw JSON candidate string or raise ``ValueError`` on fence ambiguity."""
    if not isinstance(content, str):
        raise ValueError("Structured output must be text")
    value = content.strip()
    if not value:
        raise ValueError("Structured output is empty")

    inline = _INLINE_FENCE.fullmatch(value)
    if inline and inline.group(1)[0] not in inline.group(2):
        return inline.group(2).strip()

    lines = value.splitlines()
    has_fence_line = False
    open_marker: str | None = None
    open_tag: str = ""
    buffer: list[str] = []
    blocks: list[tuple[str, str]] = []

    for line in lines:
        if open_marker is None:
            match = _FENCE_OPEN.match(line)
            if match is None:
                continue
            marker, rest = match.group(1), match.group(2)
            # CommonMark: backtick fence info strings cannot contain backticks.
            if marker[0] == "`" and "`" in rest:
                continue
            has_fence_line = True
            tokens = rest.strip().split()
            open_marker = marker
            open_tag = tokens[0].lower() if tokens else ""
            buffer = []
        else:
            stripped = line.lstrip(" ")
            leading_spaces = len(line) - len(stripped)
            close_trimmed = stripped.rstrip(" \t")
            if (
                leading_spaces <= 3
                and len(close_trimmed) >= len(open_marker)
                and set(close_trimmed) == {open_marker[0]}
            ):
                blocks.append((open_tag, "\n".join(buffer).strip()))
                open_marker = None
                open_tag = ""
                buffer = []
            else:
                buffer.append(line)

    if open_marker is not None:
        raise ValueError("Unclosed Markdown code fence in structured output")

    if not has_fence_line:
        return value

    json_tagged = [body for tag, body in blocks if tag == "json" and body]
    untagged = [body for tag, body in blocks if tag == "" and body]

    if len(json_tagged) == 1 and len(untagged) == 0:
        return json_tagged[0]
    if len(json_tagged) == 0 and len(untagged) == 1:
        return untagged[0]
    raise ValueError("Structured output must contain exactly one unambiguous JSON code fence")


def supports_stage_json_mode(model_snapshot: dict | None) -> bool:
    """Return True only when protocol-level JSON mode was explicitly verified or enabled."""
    if not isinstance(model_snapshot, dict):
        return False
    params = model_snapshot.get("parameters")
    if isinstance(params, dict) and "omni_stage_json_mode" in params:
        if params["omni_stage_json_mode"] is True:
            return True
        if params["omni_stage_json_mode"] is False:
            return False
    caps = model_snapshot.get("capabilities")
    if isinstance(caps, dict):
        json_cap = caps.get("json_mode")
        if isinstance(json_cap, dict) and json_cap.get("status") == "supported":
            if "binding" in json_cap:
                binding = json_cap.get("binding")
                if not isinstance(binding, str) or not binding:
                    return False
                if all(key in model_snapshot for key in ("id", "adapter_type", "model_name")):
                    from .providers import summary_capability_binding

                    if binding != summary_capability_binding(model_snapshot):
                        return False
            return True
    return False
