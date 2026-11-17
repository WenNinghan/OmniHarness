"""Explicit host request controls; never forwarded as vendor parameters.

Absence preserves the historical adapter settings. An opt-in policy is stored
inside the existing parameters JSON so every Run freezes it with its model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

REQUEST_POLICY_KEY = "omni_request_policy"


class RequestPolicyError(ValueError):
    """Static local validation error without untrusted parameter values."""


@dataclass(frozen=True)
class RequestTimeouts:
    connect_seconds: float = 15
    read_seconds: float = 300
    write_seconds: float = 30
    pool_seconds: float = 15
    first_chunk_seconds: float = 300
    idle_seconds: float = 90
    total_seconds: float = 600


@dataclass(frozen=True)
class RequestRecovery:
    max_attempts: int = 1
    backoff_seconds: float = 1
    max_backoff_seconds: float = 30


@dataclass(frozen=True)
class RequestPolicy:
    version: int
    timeouts: RequestTimeouts
    recovery: RequestRecovery


def _object(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise RequestPolicyError("Request policy must contain only supported fields")
    return value


def _seconds(value, *, zero=False, maximum=3600):
    if (type(value) not in (int, float) or value < 0
            or (not zero and value == 0) or value > maximum or not math.isfinite(value)):
        raise RequestPolicyError("Request policy seconds must be finite and bounded")
    return float(value)


def parse_request_policy(parameters: dict) -> RequestPolicy | None:
    if not isinstance(parameters, dict):
        raise RequestPolicyError("Model parameters must be an object")
    if REQUEST_POLICY_KEY not in parameters:
        return None
    policy = _object(parameters[REQUEST_POLICY_KEY], {"version", "timeouts", "recovery"})
    if type(policy.get("version")) is not int or policy["version"] != 1:
        raise RequestPolicyError("Unsupported request policy version")
    defaults = RequestTimeouts()
    fields = defaults.__dataclass_fields__
    values = _object(policy.get("timeouts", {}), fields)
    timeouts = RequestTimeouts(**{name: _seconds(values.get(name, getattr(defaults, name)))
                                 for name in fields})
    if (timeouts.first_chunk_seconds > timeouts.total_seconds
            or timeouts.idle_seconds > timeouts.total_seconds):
        raise RequestPolicyError("Request progress timeouts cannot exceed the total deadline")
    defaults = RequestRecovery()
    values = _object(policy.get("recovery", {}), defaults.__dataclass_fields__)
    attempts = values.get("max_attempts", defaults.max_attempts)
    if type(attempts) is not int or attempts not in (1, 2):
        raise RequestPolicyError("Request recovery permits only one or two attempts")
    backoff = _seconds(values.get("backoff_seconds", defaults.backoff_seconds), zero=True, maximum=300)
    maximum = _seconds(values.get("max_backoff_seconds", defaults.max_backoff_seconds), zero=True, maximum=300)
    if backoff > maximum:
        raise RequestPolicyError("Request backoff cannot exceed its maximum")
    return RequestPolicy(1, timeouts, RequestRecovery(attempts, backoff, maximum))


def provider_parameters(parameters: dict) -> dict:
    """Remove only the reserved host namespace; callers validate vendor fields."""
    return {key: value for key, value in parameters.items() if key != REQUEST_POLICY_KEY}
