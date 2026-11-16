"""Opt-in, host-accounted model recovery. Ambiguous dispatches never retry."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass, replace
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx
import httpx2
import openai

from .m3_agents import AgentBudgetExceeded, InvocationIdentity, _raise_sanitized


@dataclass(frozen=True)
class DispatchFailure:
    code: str
    retryable: bool
    retry_after: float | None = None


def attempt_identity(root: InvocationIdentity, attempt: int) -> InvocationIdentity:
    if attempt == 1:
        return root
    if attempt != 2:
        raise ValueError("Only one accounted recovery attempt is supported")
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return replace(root,
        invocation_id=digest({"provider_recovery_of": root.invocation_id, "attempt": attempt}),
        binding_fingerprint=digest({"request": root.binding_fingerprint,
                                    "provider_recovery_of": root.invocation_id, "attempt": attempt}))


def _retry_after(response) -> float | None:
    value = getattr(response, "headers", {}).get("retry-after")
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def classify_failure(error: BaseException) -> DispatchFailure | None:
    """Only typed transport evidence is trusted, never exception messages."""
    chain, seen, current = [], set(), error
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__
    # Even an empty parsed provider chunk may mean the request was accepted.
    if any(getattr(item, "omni_response_started", False) for item in chain):
        return None
    if any(getattr(item, "omni_timeout_phase", None) in {"first_chunk", "stream_idle", "total"}
           for item in chain):
        return None
    if any(isinstance(item, (asyncio.CancelledError, httpx.ReadTimeout, httpx.WriteTimeout,
                              httpx.ReadError, httpx.WriteError, httpx2.ReadTimeout, httpx2.WriteTimeout,
                              httpx2.ReadError, httpx2.WriteError)) for item in chain):
        return None
    for item in chain:
        if isinstance(item, (httpx.HTTPStatusError, httpx2.HTTPStatusError, openai.APIStatusError)):
            status = getattr(item, "status_code", None)
            response = getattr(item, "response", None)
            if type(status) is not int:
                status = getattr(response, "status_code", None)
            if status == 429:
                return DispatchFailure("HTTP_429_REJECTED", True, _retry_after(response))
            if type(status) is int and 400 <= status < 500:
                return DispatchFailure("HTTP_REQUEST_REJECTED", False)
            return None
    if any(isinstance(item, (httpx.PoolTimeout, httpx2.PoolTimeout)) for item in chain):
        return DispatchFailure("POOL_NOT_DISPATCHED", True)
    if any(isinstance(item, (httpx.ConnectTimeout, httpx.ConnectError,
                              httpx2.ConnectTimeout, httpx2.ConnectError)) for item in chain):
        return DispatchFailure("CONNECT_NOT_DISPATCHED", True)
    return None


async def run_provider_call(*, host, role, root, policy, dispatch, on_reserved_factory,
                            transport_mode="ainvoke_default"):
    """Return exact saved response and its actual attempt identity.

    Only the individual network-and-save operation is tracked. Backoff and
    control reconciliation run outside scheduler operations and DB locks.
    """
    while True:
        await host.check_control("model_calls", role, None)
        identity, saved, wait = await host.select_provider_attempt(role, root, policy.recovery.max_attempts)
        if saved is not None:
            return saved, identity
        if wait:
            remaining = await host.provider_remaining_seconds(role)
            if remaining <= wait:
                raise AgentBudgetExceeded("Insufficient active time for provider recovery")
            await asyncio.sleep(wait)
            await host.check_control("model_calls", role, None)
        if await host.provider_remaining_seconds(role) <= 0:
            raise AgentBudgetExceeded("Run active time exhausted before provider dispatch")
        if not await host.reserve_provider_attempt(role, root, identity, policy.recovery.max_attempts,
                                                   on_reserved=on_reserved_factory(identity)):
            raise AgentBudgetExceeded("Run model budget exhausted before provider dispatch")

        async def execute_and_save():
            response = await dispatch(identity)
            await host.finish_invocation("model_calls", role, None, identity, result=response)
            return response

        try:
            scheduler = getattr(host, "scheduler", None)
            result = await scheduler.run_tracked(execute_and_save) if scheduler else await execute_and_save()
            return result, identity
        except BaseException as exc:
            failure = classify_failure(exc)
            if failure is None:
                try:
                    await host.mark_invocation_uncertain_with_diagnostics(
                        "model_calls", role, None, identity, error=exc, transport_mode=transport_mode)
                except BaseException:
                    pass
                _raise_sanitized(exc)
            delay = max(policy.recovery.backoff_seconds, failure.retry_after or 0.0)
            retry = (failure.retryable and identity.invocation_id == root.invocation_id
                     and policy.recovery.max_attempts == 2
                     and host.workflow_version != "stage-agent-v10"
                     and delay <= policy.recovery.max_backoff_seconds
                     and delay < await host.provider_remaining_seconds(role))
            await host.fail_provider_attempt(role, root, identity, failure=failure, retry_allowed=retry,
                delay=delay, error=exc, transport_mode=transport_mode,
                blocked_reason="V10_CLOSURE_UNSUPPORTED" if host.workflow_version == "stage-agent-v10"
                and failure.retryable and policy.recovery.max_attempts == 2 else None)
            if not retry:
                _raise_sanitized(exc)
