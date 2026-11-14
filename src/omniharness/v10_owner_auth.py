"""Bounded, process-local authentication primitives for a v10 owner handshake.

Authentication never establishes a pristine Run, an active database lock, a
workspace lease, or permission to call a model/tool. Callers must enforce all
of those boundaries independently. Nothing here persists or reconstructs
eligibility after a process or listener restart.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from datetime import datetime

from .m4_invalid_audit import _key

ORIGIN_FIELD = "v10_owner_origin"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_REPLY = re.compile(r"^v10r_[0-9a-f]{32}$")
_MAX_FINGERPRINT_BYTES = 2 * 1024 * 1024


class OwnerAuthenticationError(ValueError):
    """Malformed or untrusted handshake data, without sensitive input."""


def _encoded(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise OwnerAuthenticationError("Invalid handshake encoding") from None
    if len(raw) > _MAX_FINGERPRINT_BYTES:
        raise OwnerAuthenticationError("Handshake input exceeds limit")
    return raw


def _tag(settings, domain, payload):
    key = hmac.new(_key(settings, "stage-agent-v10"), b"omniharness/v10/owner-auth/v1/key",
                   hashlib.sha256).digest()
    return hmac.new(key, domain.encode("ascii") + b"\0" + _encoded(payload), hashlib.sha256).hexdigest()


def _id(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise OwnerAuthenticationError("Invalid identity")
    return value


def _hex(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise OwnerAuthenticationError("Invalid random identity")
    return value


def _owner(value, settings):
    if not isinstance(value, dict) or set(value) != {"pid", "backend_started", "instance_id", "owner_nonce"}:
        raise OwnerAuthenticationError("Invalid owner identity")
    if type(value["pid"]) is not int or not 0 < value["pid"] <= 2**31 - 1:
        raise OwnerAuthenticationError("Invalid owner identity")
    _id(value["instance_id"])
    if value["instance_id"] != getattr(settings, "instance_id", None):
        raise OwnerAuthenticationError("Owner instance mismatch")
    _hex(value["owner_nonce"])
    started = value["backend_started"]
    if not isinstance(started, str) or len(started) > 64:
        raise OwnerAuthenticationError("Invalid backend identity")
    try:
        parsed = datetime.fromisoformat(started)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise OwnerAuthenticationError("Invalid backend identity") from None
    return dict(value)


def new_owner_identity(pid, backend_started, instance_id):
    if isinstance(backend_started, datetime):
        backend_started = backend_started.isoformat()
    value = {"pid": pid, "backend_started": backend_started, "instance_id": instance_id,
             "owner_nonce": secrets.token_hex(32)}
    # Validate without involving or persisting session configuration.
    from types import SimpleNamespace
    return _owner(value, SimpleNamespace(instance_id=instance_id))


def _challenge(value):
    if not isinstance(value, dict) or set(value) != {"challenge", "reply_to"}:
        raise OwnerAuthenticationError("Invalid challenge")
    _hex(value["challenge"])
    if not isinstance(value["reply_to"], str) or not _REPLY.fullmatch(value["reply_to"]):
        raise OwnerAuthenticationError("Invalid reply channel")
    return dict(value)


def new_challenge(reply_to):
    return _challenge({"challenge": secrets.token_hex(32), "reply_to": reply_to})


def make_challenge_response(settings, owner, challenge):
    body = {"owner": _owner(owner, settings), **_challenge(challenge)}
    return {**body, "mac": _tag(settings, "challenge-response", body)}


def verify_challenge_response(settings, response, *, challenge):
    """Verify one response; caller must consume the challenge exactly once."""
    try:
        expected = _challenge(challenge)
        if not isinstance(response, dict) or set(response) != {"owner", "challenge", "reply_to", "mac"}:
            return None
        body = {key: response[key] for key in ("owner", "challenge", "reply_to")}
        owner = _owner(body["owner"], settings)
        if ({key: body[key] for key in expected} != expected or not _HEX.fullmatch(response["mac"])
                or not hmac.compare_digest(response["mac"], _tag(settings, "challenge-response", body))):
            return None
        return owner
    except (OwnerAuthenticationError, KeyError, TypeError, ValueError):
        return None


def run_fingerprint(settings, run, message):
    """Bind a Run and its original user message without exposing the message."""
    run_id, workspace_id, conversation_id = (_id(getattr(run, name, None))
                                              for name in ("id", "workspace_id", "conversation_id"))
    message_id = _id(getattr(message, "id", None))
    if (getattr(run, "workflow_version", None) != "stage-agent-v10"
            or getattr(message, "run_id", None) != run_id
            or getattr(message, "conversation_id", None) != conversation_id
            or getattr(message, "role", None) != "user"):
        raise OwnerAuthenticationError("Message or Run scope mismatch")
    content = getattr(message, "content", None)
    snapshot = getattr(run, "config_snapshot", None)
    if not isinstance(content, str) or not 1 <= len(content) <= 50000 or not isinstance(snapshot, dict):
        raise OwnerAuthenticationError("Invalid frozen Run input")
    model_id = _id(getattr(run, "model_config_id", None))
    snapshot = {key: value for key, value in snapshot.items() if key != ORIGIN_FIELD}
    if "v10_checkpoint_seal_origin" in snapshot:
        from .m4_invalid_audit import _key
        from .v10_sealed_saver import SEALED_PLUGIN_VERSIONS
        from .v10_sealed_saver import _tag as seal_tag

        seal = snapshot["v10_checkpoint_seal_origin"]
        if (not isinstance(seal, dict)
                or set(seal) != {"version", "run_id", "workspace_id", "nonce", "state", "mac"}
                or (snapshot.get("agent") or {}).get("plugin_versions") != SEALED_PLUGIN_VERSIONS
                or seal.get("version") != 1 or seal.get("run_id") != run_id
                or seal.get("workspace_id") != workspace_id or seal.get("state") not in {"unused", "started"}):
            raise OwnerAuthenticationError("Invalid checkpoint seal origin")
        _hex(seal.get("nonce"))
        _hex(seal.get("mac"))
        value = {name: seal[name] for name in ("version", "run_id", "workspace_id", "nonce", "state")}
        key = hmac.new(_key(settings, "stage-agent-v10"), b"omniharness/v10/checkpoint-seal/v1",
                       hashlib.sha256).digest()
        if not hmac.compare_digest(seal["mac"], seal_tag(key, "origin", value)):
            raise OwnerAuthenticationError("Invalid checkpoint seal origin MAC")
        # Only the signed state transition is mutable; the nonce and all other
        # frozen input fields remain bound to the owner origin.
        snapshot["v10_checkpoint_seal_origin"] = {name: seal[name] for name in
                                                   ("version", "run_id", "workspace_id", "nonce")}
    return _tag(settings, "run-fingerprint", {"run_id": run_id, "workspace_id": workspace_id,
        "conversation_id": conversation_id, "model_config_id": model_id, "workflow_version": "stage-agent-v10",
        "config_snapshot": snapshot, "message_id": message_id, "content": content})


def make_run_origin(settings, owner, run, message):
    body = {"owner": _owner(owner, settings), "run_id": _id(run.id), "workspace_id": _id(run.workspace_id),
            "event_id": secrets.token_hex(32), "digest": run_fingerprint(settings, run, message)}
    return {**body, "mac": _tag(settings, "run-origin", body)}


def _authenticated_origin(settings, origin, owner):
    if not isinstance(origin, dict) or set(origin) != {"owner", "run_id", "workspace_id", "event_id", "digest", "mac"}:
        return False
    expected_owner = _owner(owner, settings)
    actual_owner = _owner(origin["owner"], settings)
    _id(origin["run_id"])
    _id(origin["workspace_id"])
    for field in ("event_id", "digest", "mac"):
        _hex(origin[field])
    body = {key: origin[key] for key in ("owner", "run_id", "workspace_id", "event_id", "digest")}
    return actual_owner == expected_owner and hmac.compare_digest(origin["mac"], _tag(settings, "run-origin", body))


def verify_run_origin(settings, origin, *, owner, run, message):
    try:
        return bool(_authenticated_origin(settings, origin, owner) and origin["run_id"] == run.id
                    and origin["workspace_id"] == run.workspace_id
                    and hmac.compare_digest(origin["digest"], run_fingerprint(settings, run, message)))
    except (OwnerAuthenticationError, KeyError, TypeError, ValueError, AttributeError):
        return False


class ChallengeRegistry:
    """Process-local, bounded pending challenges; no persisted recovery."""

    def __init__(self, max_pending=128):
        if type(max_pending) is not int or not 1 <= max_pending <= 10000:
            raise ValueError("Invalid challenge limit")
        self._max_pending, self._pending = max_pending, {}
        self._lock = threading.Lock()

    def issue(self, reply_to):
        with self._lock:
            if len(self._pending) >= self._max_pending:
                raise OwnerAuthenticationError("Challenge limit reached")
            challenge = new_challenge(reply_to)
            self._pending[challenge["challenge"]] = challenge
            return dict(challenge)

    def consume(self, settings, response):
        if not isinstance(response, dict) or not isinstance(response.get("challenge"), str):
            return None
        with self._lock:
            challenge = self._pending.get(response["challenge"])
            if challenge is None:
                return None
            owner = verify_challenge_response(settings, response, challenge=challenge)
            if owner is not None:
                del self._pending[challenge["challenge"]]
            return owner

    def discard(self, challenge):
        if isinstance(challenge, dict) and isinstance(challenge.get("challenge"), str):
            with self._lock:
                self._pending.pop(challenge["challenge"], None)


class OwnerEventRegistry:
    """Deduplicate authenticated events per owner lifetime; never grants eligibility."""

    def __init__(self, owner, settings, max_events=10000):
        if type(max_events) is not int or not 1 <= max_events <= 10000:
            raise ValueError("Invalid event limit")
        self._owner, self._settings, self._max = _owner(owner, settings), settings, max_events
        self._events, self._runs = set(), set()
        self._lock = threading.Lock()

    def consume(self, origin, *, run, message):
        try:
            if not _authenticated_origin(self._settings, origin, self._owner):
                return False
            with self._lock:
                if len(self._events) >= self._max or origin["event_id"] in self._events or origin["run_id"] in self._runs:
                    return False
                # Consume authentic messages even when the saved Run differs; a later
                # rollback must not make a formerly rejected event acceptable.
                self._events.add(origin["event_id"])
                self._runs.add(origin["run_id"])
                return verify_run_origin(self._settings, origin, owner=self._owner, run=run, message=message)
        except (OwnerAuthenticationError, KeyError, TypeError, ValueError, AttributeError):
            return False
