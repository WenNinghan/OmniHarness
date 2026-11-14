"""Invocation-bound budget accounting. Requires a caller-owned transaction.

This component provides accounting, never permission to repeat an operation.
The caller must provide a fenced, exclusive Worker transaction and separately
enforce operation idempotency, version compatibility and uncertain outcomes.
"""

import re
from enum import Enum

from sqlalchemy import insert, select, update

from .models import RunBudget, RunBudgetReservation, now

_HEX = re.compile(r"^[0-9a-f]{64}$")
_KINDS = {"model_calls": (RunBudget.model_calls, RunBudget.max_model_calls),
          "tool_calls": (RunBudget.tool_calls, RunBudget.max_tool_calls)}
reservations = RunBudgetReservation.__table__


class ReservationResult(str, Enum):
    RESERVED = "reserved"
    ALREADY_RESERVED = "already_reserved"
    EXHAUSTED = "exhausted"


class ReservationConflict(RuntimeError):
    pass


def reserve_invocation(db, *, run_id: str, kind: str, invocation_id: str,
                       binding_fingerprint: str) -> ReservationResult:
    """Reserve once under a caller's transaction; existing budget must be frozen.

    Locking is an explicit no-op UPDATE so SQLite obtains its write lock too.
    An owner-bound connection is mandatory when a host has multiple workers.
    """
    if kind not in _KINDS or not isinstance(run_id, str) or not run_id:
        raise ValueError("Invalid run or budget kind")
    if not isinstance(invocation_id, str) or not _HEX.fullmatch(invocation_id):
        raise ValueError("Invalid invocation identity")
    if not isinstance(binding_fingerprint, str) or not _HEX.fullmatch(binding_fingerprint):
        raise ValueError("Invalid binding fingerprint")
    if not db.in_transaction():
        raise ReservationConflict("Caller-owned transaction is required")

    budget_row = db.execute(
        update(RunBudget).where(RunBudget.run_id == run_id)
        .values(updated_at=RunBudget.updated_at).returning(RunBudget.run_id)
    ).scalar_one_or_none()
    if budget_row is None:
        raise ReservationConflict("Frozen run budget is missing")
    existing = db.execute(select(reservations.c.kind, reservations.c.binding_fingerprint).where(
        reservations.c.run_id == run_id, reservations.c.invocation_id == invocation_id,
    )).one_or_none()
    if existing:
        if existing.kind != kind or existing.binding_fingerprint != binding_fingerprint:
            raise ReservationConflict("Invocation identity binding changed")
        return ReservationResult.ALREADY_RESERVED
    counter, limit = _KINDS[kind]
    charged = db.execute(update(RunBudget).where(RunBudget.run_id == run_id, counter < limit)
                         .values({counter: counter + 1, RunBudget.updated_at: now()})
                         .returning(RunBudget.run_id)).scalar_one_or_none()
    if charged is None:
        return ReservationResult.EXHAUSTED
    db.execute(insert(reservations).values(run_id=run_id, invocation_id=invocation_id, kind=kind,
                                           binding_fingerprint=binding_fingerprint, created_at=now()))
    return ReservationResult.RESERVED
