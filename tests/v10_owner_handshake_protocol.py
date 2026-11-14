"""Test-only authenticated messages for an owner-lifetime experiment."""
import hashlib
import hmac
import json

from omniharness.m4_invalid_audit import _key


def tag(settings, domain, value):
    key = hmac.new(_key(settings, "stage-agent-v10"), b"v10-owner-handshake-experiment-v1", hashlib.sha256).digest()
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hmac.new(key, domain.encode("ascii") + b"\0" + payload, hashlib.sha256).hexdigest()


def run_digest(run, message):
    snapshot = dict(run.config_snapshot)
    snapshot.pop("v10_test_owner_origin", None)
    payload = {"run_id": run.id, "workspace_id": run.workspace_id, "conversation_id": run.conversation_id,
               "model_config_id": run.model_config_id, "workflow_version": run.workflow_version,
               "config_snapshot": snapshot, "message_id": message.id, "content": message.content}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()
