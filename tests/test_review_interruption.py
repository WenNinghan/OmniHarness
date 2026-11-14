"""Diagnostics recorded when a review closes in-flight calls (2026-11-11 gap:
three unknown model results had no classification). Fixed fields only."""
import json
from datetime import datetime, timedelta, timezone

import httpx

from omniharness.m3_agents import AgentExternalCallFailed, _raise_sanitized
from omniharness.m3_worker import _interruption, _root_diagnostic


def test_interruption_records_elapsed_and_review_code_only():
    end = datetime(2026, 11, 11, tzinfo=timezone.utc)
    value = _interruption("AGENT_EXECUTION_REVIEW", end - timedelta(seconds=331.6), end)
    assert value == {"review_code": "AGENT_EXECUTION_REVIEW", "elapsed_seconds": 331.6}
    naive = (end - timedelta(seconds=2)).replace(tzinfo=None)  # SQLite returns naive UTC
    assert _interruption("X", naive, end)["elapsed_seconds"] == 2.0
    assert _interruption("X", None, end)["elapsed_seconds"] is None
    assert _interruption("Y" * 81, None, end)["review_code"] == "UNKNOWN"


def test_root_diagnostic_sees_through_sanitized_boundary_without_text():
    request = httpx.Request("POST", "https://private-canary.invalid/v1")
    response = httpx.Response(524, request=request, text="private-canary body")
    try:
        try:
            raise httpx.HTTPStatusError("private-canary", request=request, response=response)
        except httpx.HTTPStatusError as exc:
            _raise_sanitized(exc)
    except AgentExternalCallFailed as sanitized:
        diagnostic = _root_diagnostic(sanitized)
    assert diagnostic["http_status"] == 524 and diagnostic["exception_category"] == "http_status"
    assert diagnostic["exception_type"] == "HTTPStatusError" and diagnostic["chain_depth"] == 1
    assert "private-canary" not in json.dumps(diagnostic)
    assert _interruption("R", None, datetime.now(timezone.utc), diagnostic)["diagnostic"] == diagnostic


def test_root_diagnostic_hides_unknown_exception_types():
    class PluginSecretError(Exception):
        pass
    diagnostic = _root_diagnostic(PluginSecretError("private-canary"))
    assert diagnostic["exception_type"] == "other" and diagnostic["exception_category"] == "unknown"
    assert "private-canary" not in json.dumps(diagnostic)
