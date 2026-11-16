"""Bounded admission facts; never include conversation or provider payloads."""
from __future__ import annotations

from typing import Literal, TypedDict


class ContextAdmissionDiagnostic(TypedDict):
    reason: Literal["message_count", "serialized_characters", "input_tokens"]
    observed: int
    limit: int
    window_known: bool


def safe_admission_diagnostic(value: object) -> ContextAdmissionDiagnostic | None:
    """Accept only bounded rejection facts before persisting an exception detail."""
    if type(value) is not dict or len(value) != 4 or set(value) != {
        "reason", "observed", "limit", "window_known",
    }:
        return None
    if (type(value["reason"]) is not str
            or value["reason"] not in {"message_count", "serialized_characters", "input_tokens"}
            or type(value["window_known"]) is not bool
            or any(type(value[key]) is not int or not 0 < value[key] <= 2**63 - 1
                   for key in ("observed", "limit"))
            or value["observed"] <= value["limit"]):
        return None
    return value.copy()


def context_admission_diagnostic(
    *, input_tokens: int, input_limit: int, message_count: int, message_limit: int,
    serialized_characters: int, character_limit: int, window_known: bool,
) -> ContextAdmissionDiagnostic | None:
    """Return the first violated frozen guard, using a fixed four-field schema.

    The order preserves the existing message/serialized-size/token guards. A
    local input target is still a limit when the provider window is unknown;
    ``window_known`` describes evidence, not permission to exceed that target.
    """
    checks = (
        ("message_count", message_count, message_limit),
        ("serialized_characters", serialized_characters, character_limit),
        ("input_tokens", input_tokens, input_limit),
    )
    if type(window_known) is not bool or any(
        type(observed) is not int or observed < 0 or type(limit) is not int or limit <= 0
        for _, observed, limit in checks
    ):
        raise ValueError("Admission counts and frozen limits must be valid integers")
    for reason, observed, limit in checks:
        if observed > limit:
            return {"reason": reason, "observed": observed, "limit": limit, "window_known": window_known}
    return None
