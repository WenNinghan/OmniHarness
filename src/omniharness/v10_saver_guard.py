"""Process-local fail-closed exclusion for sealed-v10 checkpoint writers.

This registry is an additional guard, not durable cross-process authority. It
never enables correction. Same-owner waits may retire one writer and issue a
new generation; closed phases and changed owners remain closed. A restarted
owner still needs an authenticated database origin and seal check.
"""

from __future__ import annotations

import threading
import weakref

_lock = threading.Lock()
_entries = {}
_correction_entries = {}
_LIMIT = 10000


class SaverPhaseUnavailable(RuntimeError):
    """A saver cannot establish the exclusive execution write phase."""


def _key(ticket):
    return ticket.schema, ticket.thread_id


def begin_ordinary_write(ticket):
    """Track a legacy v10 write across all awaits; older versions are unchanged."""
    from .checkpoint_fencing import SEALED_V10_FENCE_VERSION

    if ticket.workflow_version == SEALED_V10_FENCE_VERSION:
        raise SaverPhaseUnavailable("Sealed v10 rejects ordinary checkpoint writes")
    if ticket.workflow_version != "stage-agent-v10":
        return None
    key = _key(ticket)
    with _lock:
        entry = _entries.get(key)
        if entry is not None and entry.get("phase") != "ORDINARY":
            raise SaverPhaseUnavailable("Sealed v10 owns this checkpoint thread")
        if entry is None:
            if len(_entries) >= _LIMIT:
                raise SaverPhaseUnavailable("Saver phase registry is full")
            entry = {"phase": "ORDINARY", "inflight": 0}
            _entries[key] = entry
        entry["inflight"] += 1
    return key


def end_ordinary_write(key):
    if key is None:
        return
    with _lock:
        entry = _entries.get(key)
        if entry is None or entry.get("phase") != "ORDINARY" or entry.get("inflight", 0) < 1:
            raise SaverPhaseUnavailable("Ordinary saver tracking was lost")
        entry["inflight"] -= 1
        if entry["inflight"] == 0:
            del _entries[key]


def register_execution_writer(ticket, token, saver):
    """One execution writer; only a same-owner paused generation may resume."""
    from .checkpoint_fencing import SEALED_V10_FENCE_VERSION

    if ticket.workflow_version != SEALED_V10_FENCE_VERSION:
        raise SaverPhaseUnavailable("Sealed v10 ticket required")
    key = _key(ticket)
    with _lock:
        previous = _entries.get(key)
        if previous is not None:
            if (
                previous.get("phase") != "RESUME_READY"
                or previous.get("epoch") != ticket.epoch
                or previous.get("owner_pid") != ticket.owner_pid
                or previous.get("owner_started") != ticket.owner_started
                or previous.get("business_schema") != saver.business_schema
            ):
                raise SaverPhaseUnavailable("Checkpoint thread already has a writer or phase")
        elif len(_entries) >= _LIMIT:
            raise SaverPhaseUnavailable("Saver phase registry is full")
        _entries[key] = {
            "phase": "EXECUTION",
            "writer": token,
            "saver": weakref.ref(saver),
            "business_schema": saver.business_schema,
            "epoch": ticket.epoch,
            "owner_pid": ticket.owner_pid,
            "owner_started": ticket.owner_started,
        }


def authorize_same_owner_resume(db, ticket, expected_checkpoint_id, settings):
    """Authorize one same-owner generation from a pinned, authenticated Run.

    Caller owns the worker transaction and must use the same owner connection.
    The checkpoint fence is locked before Run and is kept until commit. This
    is not correction authorization.
    """
    from .v10_sealed_saver import verify_pinned_seal_inventory

    verify_pinned_seal_inventory(db, ticket, expected_checkpoint_id, settings)
    with _lock:
        entry = _entries.get(_key(ticket))
        if (
            entry is None
            or entry.get("phase") != "PAUSED"
            or entry.get("epoch") != ticket.epoch
            or entry.get("owner_pid") != ticket.owner_pid
            or entry.get("owner_started") != ticket.owner_started
        ):
            raise SaverPhaseUnavailable("Same-owner paused generation is unavailable")
        entry["phase"] = "RESUME_READY"


def revoke_resume(ticket):
    """A failed owner transaction cannot leave a reusable resume grant."""
    with _lock:
        entry = _entries.get(_key(ticket))
        if entry is not None and entry.get("phase") == "RESUME_READY":
            entry["phase"] = "EXECUTION_CLOSED"


def current_execution_writer(ticket):
    """Resolve the exact live object, solely for its owning context finalizer."""
    with _lock:
        entry = _entries.get(_key(ticket))
        if (
            entry is None
            or entry.get("phase") != "EXECUTION"
            or entry.get("epoch") != ticket.epoch
            or entry.get("owner_pid") != ticket.owner_pid
            or entry.get("owner_started") != ticket.owner_started
        ):
            return None
        return entry["saver"]()


def begin_retire_execution_writer(ticket, token):
    with _lock:
        entry = _entries.get(_key(ticket))
        if (
            entry is None
            or entry.get("phase") != "EXECUTION"
            or entry.get("writer") is not token
            or entry.get("epoch") != ticket.epoch
            or entry.get("owner_pid") != ticket.owner_pid
            or entry.get("owner_started") != ticket.owner_started
        ):
            raise SaverPhaseUnavailable("Execution writer phase differs")
        entry["phase"] = "RETIRING"


def finish_retire_execution_writer(ticket, token, *, waiting):
    with _lock:
        entry = _entries.get(_key(ticket))
        if (
            entry is None
            or entry.get("phase") != "RETIRING"
            or entry.get("writer") is not token
            or entry.get("epoch") != ticket.epoch
            or entry.get("owner_pid") != ticket.owner_pid
            or entry.get("owner_started") != ticket.owner_started
        ):
            raise SaverPhaseUnavailable("Execution writer retirement differs")
        entry["phase"] = "PAUSED" if waiting else "EXECUTION_CLOSED"
        entry["writer"] = None
        entry["saver"] = None


def assert_execution_writer(ticket, writer):
    with _lock:
        entry = _entries.get(_key(ticket))
        if (
            entry is None
            or entry.get("phase") != "EXECUTION"
            or entry.get("writer") is not writer
            or entry.get("epoch") != ticket.epoch
            or entry.get("owner_pid") != ticket.owner_pid
            or entry.get("owner_started") != ticket.owner_started
        ):
            raise SaverPhaseUnavailable("Execution writer phase is closed or differs")


def assert_closed_execution_writer(ticket, writer):
    """Fail if this exact owner/generation is not irreversibly closed."""
    with _lock:
        entry = _entries.get(_key(ticket))
        if (
            entry is None
            or entry.get("phase") != "EXECUTION_CLOSED"
            or entry.get("writer") is not writer
            or entry.get("epoch") != ticket.epoch
            or entry.get("owner_pid") != ticket.owner_pid
            or entry.get("owner_started") != ticket.owner_started
        ):
            raise SaverPhaseUnavailable("Closed execution generation is unavailable")


def close_execution_writer(ticket, writer):
    with _lock:
        entry = _entries.get(_key(ticket))
        if (
            entry is None
            or entry.get("writer") is not writer
            or entry.get("epoch") != ticket.epoch
            or entry.get("owner_pid") != ticket.owner_pid
            or entry.get("owner_started") != ticket.owner_started
            or entry.get("phase") not in {"EXECUTION", "RETIRING", "EXECUTION_CLOSED"}
        ):
            raise SaverPhaseUnavailable("Execution writer phase differs")
        entry["phase"] = "EXECUTION_CLOSED"


def register_correction_writer(ticket, token, saver, execution_saver):
    """One separate writer while the exact execution generation stays closed."""
    key = _key(ticket)
    with _lock:
        original = _entries.get(key)
        if (original is None or original.get("phase") != "EXECUTION_CLOSED"
                or original.get("writer") is not execution_saver._phase_token
                or original.get("saver") is None or original["saver"]() is not execution_saver
                or original.get("epoch") != ticket.epoch or original.get("owner_pid") != ticket.owner_pid
                or original.get("owner_started") != ticket.owner_started
                or not execution_saver._writes_closed or execution_saver._active_writes != 0):
            raise SaverPhaseUnavailable("Original execution generation is not closed")
        if key in _correction_entries or len(_correction_entries) >= _LIMIT:
            raise SaverPhaseUnavailable("Correction generation already exists or capacity is exhausted")
        _correction_entries[key] = {"writer": token, "saver": weakref.ref(saver),
                                    "execution": weakref.ref(execution_saver), "closed": False,
                                    "epoch": ticket.epoch, "owner_pid": ticket.owner_pid,
                                    "owner_started": ticket.owner_started}


def assert_correction_writer(ticket, token, saver, execution_saver):
    with _lock:
        key = _key(ticket)
        entry, original = _correction_entries.get(key), _entries.get(key)
        if (entry is None or entry.get("closed") or entry.get("writer") is not token
                or entry["saver"]() is not saver or entry["execution"]() is not execution_saver
                or entry.get("epoch") != ticket.epoch or entry.get("owner_pid") != ticket.owner_pid
                or entry.get("owner_started") != ticket.owner_started
                or original is None or original.get("phase") != "EXECUTION_CLOSED"
                or original.get("writer") is not execution_saver._phase_token
                or original.get("saver") is None or original["saver"]() is not execution_saver
                or not execution_saver._writes_closed):
            raise SaverPhaseUnavailable("Correction generation is closed or differs")


def close_correction_writer(ticket, token):
    with _lock:
        entry = _correction_entries.get(_key(ticket))
        if entry is None or entry.get("writer") is not token:
            raise SaverPhaseUnavailable("Correction generation differs")
        entry["closed"] = True
