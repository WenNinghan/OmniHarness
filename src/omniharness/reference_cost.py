"""Decimal, per-operation public reference costs. Never claims account billing."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from urllib.parse import urlsplit


def load_reference_prices() -> dict:
    return json.loads(files("omniharness").joinpath("data/reference-prices-v1.json").read_text(encoding="utf-8"))


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _integer(value):
    return value if type(value) is int and value >= 0 else None


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _quote(binding, snapshot, at):
    if not isinstance(binding, dict) or binding.get("adapter_type") != snapshot["adapter_type"]:
        return None
    url = urlsplit(binding.get("base_url") or "")
    if (url.username or url.password or url.query or url.fragment
            or (url.scheme + "://" + url.netloc + url.path.rstrip("/")) != snapshot["endpoint"]):
        return None
    observed = _time(snapshot["observed_at"])
    if at.tzinfo is None or at > observed + timedelta(days=snapshot["valid_days"]):
        return None
    quote = snapshot["quotes"].get(binding.get("model_name"))
    if not quote or any(_number(quote.get(key)) is None for key in ("input", "output")):
        return None
    return quote


def calculate_reference_cost(operations: list[dict], *, expected_calls: int | None = None,
                             snapshot: dict | None = None, at: datetime | None = None) -> dict:
    """Each model operation must carry the frozen model_binding and usage list.

    Input totals already include cached tokens; the default estimate intentionally
    prices all input at the highest uncached band. Reasoning is already in output.
    Unknown, partial and duplicate-conflicting evidence never becomes a zero bill.
    """
    snapshot = snapshot or load_reference_prices()
    at = at or datetime.now(timezone.utc)
    unique, entries, unknown, total, per_model = {}, [], [], Decimal(0), {}
    for operation in operations:
        identity = operation.get("operation_id")
        model_operation = (operation.get("operation_kind") == "model_calls"
                           or operation.get("node_id", "").rsplit(":", 1)[-1] == "model_calls")
        if not model_operation:
            continue
        if not identity:
            unknown.append({"reason": "missing_operation_identity"})
            continue
        if identity in unique:
            if unique[identity] != operation:
                unknown.append({"operation_id": identity, "reason": "conflicting_duplicate"})
            continue
        unique[identity] = operation
        binding = operation.get("model_binding")
        quote = _quote(binding, snapshot, at)
        usages = operation.get("normalized_usage")
        if usages is None:
            usages = operation.get("usage")
        if not isinstance(usages, list) or not usages:
            unknown.append({"operation_id": identity, "reason": "missing_usage"})
            continue
        for index, usage in enumerate(usages):
            valid = isinstance(usage, dict) and usage.get("complete", True) is not False
            counts = {key: _integer(usage.get(key)) if isinstance(usage, dict) else None
                      for key in ("input_tokens", "output_tokens", "total_tokens")}
            if not valid or any(value is None for value in counts.values()) or quote is None:
                unknown.append({"operation_id": identity, "usage_index": index,
                                "reason": "usage_or_frozen_quote_unavailable"})
                continue
            if counts["total_tokens"] != counts["input_tokens"] + counts["output_tokens"]:
                unknown.append({"operation_id": identity, "usage_index": index, "reason": "usage_totals_conflict"})
                continue
            cost = (counts["input_tokens"] * _number(quote["input"])
                    + counts["output_tokens"] * _number(quote["output"])) / Decimal(1_000_000)
            total += cost
            model = binding["model_name"]
            aggregate = per_model.setdefault(model, {"input_tokens": 0, "output_tokens": 0,
                                                     "calls": 0, "cost": Decimal(0)})
            aggregate["input_tokens"] += counts["input_tokens"]
            aggregate["output_tokens"] += counts["output_tokens"]
            aggregate["calls"] += 1
            aggregate["cost"] += cost
            entries.append({"operation_id": identity, "usage_index": index, "model_binding": binding,
                            "purpose": operation.get("purpose"), "recovery_of": operation.get("recovery_of"),
                            **counts, "reference_cost": str(cost)})
    if expected_calls is not None and len(entries) != expected_calls:
        unknown.append({"reason": "model_call_count_mismatch", "expected_calls": expected_calls,
                        "observed_complete_calls": len(entries)})
    complete = not unknown and bool(entries)
    return {"basis": "highest_band_no_cache_before_group", "source": snapshot["source"],
            "quote_version": snapshot["version"], "quote_observed_at": snapshot["observed_at"],
            "unit": "site_reference_units", "group_multiplier": None, "billed_cost": None,
            "status": "COMPLETE" if complete else "PARTIAL" if entries else "UNKNOWN",
            "reference_cost": str(total) if complete else None,
            "observed_reference_cost": str(total) if entries else None,
            "entries": entries, "unknown": unknown,
            "by_model": {key: {**value, "cost": str(value["cost"])} for key, value in per_model.items()}}
