"""Host-generated monotonic evidence for interactive checkpoint recovery.

A marker is informational until validated against the owner-fenced business
segment event and the persisted, Run-bound LangGraph interrupt.
"""
from __future__ import annotations

import re
import secrets
import time
from pathlib import Path

_BOOT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NONCE = re.compile(r"^[0-9a-f]{32}$")


def current_boot_id() -> str | None:
    """Linux kernel boot identity; no wall-clock-derived fallback."""
    try:
        value = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip().lower()
    except (OSError, UnicodeError):
        return None
    return value if _BOOT.fullmatch(value) else None


def new_segment(start_ns: int) -> dict | None:
    boot = current_boot_id()
    if boot is None or type(start_ns) is not int or start_ns < 0:
        return None
    return {'schema': 1, 'nonce': secrets.token_hex(16), 'boot_id': boot, 'start_ns': start_ns}


def user_interrupt(host, *, kind: str, **fields) -> dict:
    """Create only host-owned interrupt data; no model-provided timing is accepted."""
    value = {'type': kind, 'run_id': host.run_id, **fields}
    segment = getattr(host, '_active_segment_timing', None)
    if isinstance(segment, dict):
        value['timing'] = {'schema': 1, 'nonce': segment.get('nonce'),
                           'boot_id': segment.get('boot_id'), 'end_ns': time.monotonic_ns()}
    return value


def recovered_seconds(segment, marker, *, now_ns: int, boot_id: str | None) -> int | None:
    if not isinstance(segment, dict) or not isinstance(marker, dict):
        return None
    start, end = segment.get('start_ns'), marker.get('end_ns')
    nonce = segment.get('nonce')
    if (type(segment.get('schema')) is not int or segment.get('schema') != 1
            or type(marker.get('schema')) is not int or marker.get('schema') != 1
            or not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
            or marker.get('nonce') != nonce or not isinstance(boot_id, str)
            or not _BOOT.fullmatch(boot_id) or segment.get('boot_id') != boot_id
            or marker.get('boot_id') != boot_id or type(start) is not int or type(end) is not int
            or type(now_ns) is not int or start < 0 or not start <= end <= now_ns):
        return None
    return (end - start + 999_999_999) // 1_000_000_000
