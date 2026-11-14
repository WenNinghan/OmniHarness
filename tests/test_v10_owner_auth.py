"""Pure, offline v10 handshake authentication tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from omniharness import v10_owner_auth as auth


@pytest.fixture
def data(monkeypatch):
    monkeypatch.setattr(auth, "_key", lambda *_: b"isolated-test-key")
    settings = SimpleNamespace(instance_id="local")
    owner = auth.new_owner_identity(42, datetime(2026, 1, 1, tzinfo=timezone.utc), "local")
    run = SimpleNamespace(id="run-1", workspace_id="workspace-1", conversation_id="conversation-1",
        model_config_id="model-1", workflow_version="stage-agent-v10", config_snapshot={
            "agent": {"strategy": "compact", "secret": "credential-value"}, "workspace": {"id": "workspace-1"}})
    message = SimpleNamespace(id="message-1", run_id="run-1", conversation_id="conversation-1",
                              role="user", content="private user requirement")
    return settings, owner, run, message


def test_challenge_is_bounded_instance_bound_and_single_use(data):
    settings, owner, _, _ = data
    registry = auth.ChallengeRegistry(max_pending=1)
    challenge = registry.issue("v10r_" + "a" * 32)
    with pytest.raises(auth.OwnerAuthenticationError):
        registry.issue("v10r_" + "b" * 32)
    response = auth.make_challenge_response(settings, owner, challenge)
    assert registry.consume(settings, {**response, "reply_to": "v10r_" + "b" * 32}) is None
    assert registry.consume(settings, response) == owner
    assert registry.consume(settings, response) is None
    other = registry.issue("v10r_" + "b" * 32)
    assert auth.verify_challenge_response(settings, response, challenge=other) is None
    assert auth.verify_challenge_response(SimpleNamespace(instance_id="other"), response, challenge=challenge) is None


def test_owner_nonce_is_fresh_and_replayed_origin_is_rejected(data):
    settings, owner, run, message = data
    other = auth.new_owner_identity(owner["pid"], owner["backend_started"], "local")
    assert owner["owner_nonce"] != other["owner_nonce"]
    origin = auth.make_run_origin(settings, owner, run, message)
    assert auth.verify_run_origin(settings, origin, owner=owner, run=run, message=message)
    assert not auth.verify_run_origin(settings, origin, owner=other, run=run, message=message)
    assert not auth.verify_run_origin(settings, origin, owner=owner, run=run,
        message=SimpleNamespace(**{**vars(message), "run_id": "foreign"}))
    assert "private user requirement" not in str(origin)
    assert "credential-value" not in str(origin)


def test_fingerprint_binds_frozen_inputs_but_excludes_origin(data):
    settings, owner, run, message = data
    origin = auth.make_run_origin(settings, owner, run, message)
    run.config_snapshot = {**run.config_snapshot, auth.ORIGIN_FIELD: origin}
    assert auth.verify_run_origin(settings, origin, owner=owner, run=run, message=message)
    run.config_snapshot = {**run.config_snapshot, "action_contract": {"version": 2}}
    assert not auth.verify_run_origin(settings, origin, owner=owner, run=run, message=message)
    run.config_snapshot.pop("action_contract")
    message.content += " changed"
    assert not auth.verify_run_origin(settings, origin, owner=owner, run=run, message=message)


def test_fingerprint_accepts_only_authenticated_seal_state_transition(data, monkeypatch):
    from omniharness import m4_invalid_audit
    from omniharness import v10_sealed_saver as seal_module

    settings, owner, run, message = data
    monkeypatch.setattr(m4_invalid_audit, "_key", lambda *_: b"isolated-test-key")
    monkeypatch.setattr(seal_module, "_key", lambda *_: b"isolated-test-key")
    seal = seal_module.create_seal_origin(settings, run.id, run.workspace_id)
    run.config_snapshot = {**run.config_snapshot,
        "agent": {**run.config_snapshot["agent"], "plugin_versions": seal_module.SEALED_PLUGIN_VERSIONS},
        "v10_checkpoint_seal_origin": seal, "action_contract": {"version": 1}}
    origin = auth.make_run_origin(settings, owner, run, message)
    key = seal_module.hmac.new(b"isolated-test-key", b"omniharness/v10/checkpoint-seal/v1",
                               seal_module.hashlib.sha256).digest()
    changed = {**seal, "state": "started"}
    changed["mac"] = seal_module._tag(key, "origin", {name: changed[name] for name in
        ("version", "run_id", "workspace_id", "nonce", "state")})
    run.config_snapshot = {**run.config_snapshot, "v10_checkpoint_seal_origin": changed}
    assert auth.verify_run_origin(settings, origin, owner=owner, run=run, message=message)
    run.config_snapshot = {**run.config_snapshot, "v10_checkpoint_seal_origin": {**changed, "mac": "0" * 64}}
    assert not auth.verify_run_origin(settings, origin, owner=owner, run=run, message=message)
    changed["nonce"] = "1" * 64
    changed["mac"] = seal_module._tag(key, "origin", {name: changed[name] for name in
        ("version", "run_id", "workspace_id", "nonce", "state")})
    run.config_snapshot = {**run.config_snapshot, "v10_checkpoint_seal_origin": changed}
    assert not auth.verify_run_origin(settings, origin, owner=owner, run=run, message=message)


def test_authenticated_events_are_consumed_once_and_forgery_does_not_fill_capacity(data):
    settings, owner, run, message = data
    registry = auth.OwnerEventRegistry(owner, settings, max_events=1)
    origin = auth.make_run_origin(settings, owner, run, message)
    assert not registry.consume({**origin, "mac": "0" * 64}, run=run, message=message)
    assert registry.consume(origin, run=run, message=message)
    assert not registry.consume(origin, run=run, message=message)
    second = auth.make_run_origin(settings, owner, run, message)
    assert not registry.consume(second, run=run, message=message)


def test_authentic_mismatch_is_consumed_and_cannot_be_replayed_after_rollback(data):
    settings, owner, run, message = data
    registry = auth.OwnerEventRegistry(owner, settings)
    origin = auth.make_run_origin(settings, owner, run, message)
    message.content = "changed"
    assert not registry.consume(origin, run=run, message=message)
    message.content = "private user requirement"
    assert not registry.consume(origin, run=run, message=message)


def test_concurrent_replay_has_at_most_one_winner(data):
    settings, owner, run, message = data
    registry = auth.OwnerEventRegistry(owner, settings)
    origin = auth.make_run_origin(settings, owner, run, message)
    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(lambda _: registry.consume(origin, run=run, message=message), range(32)))
    assert sum(result) == 1


def test_strict_shapes_and_limits_fail_closed(data):
    settings, owner, run, message = data
    with pytest.raises(auth.OwnerAuthenticationError):
        auth.new_challenge("bad-channel")
    with pytest.raises(auth.OwnerAuthenticationError):
        auth.new_owner_identity(True, owner["backend_started"], "local")
    with pytest.raises(auth.OwnerAuthenticationError):
        auth.new_owner_identity(42, "2026-01-01T00:00:00", "local")
    origin = auth.make_run_origin(settings, owner, run, message)
    assert not auth.verify_run_origin(settings, {**origin, "extra": "x"}, owner=owner, run=run, message=message)
    assert not auth.verify_run_origin(settings, {**origin, "mac": 1}, owner=owner, run=run, message=message)
    message.content = "x" * 50001
    with pytest.raises(auth.OwnerAuthenticationError):
        auth.run_fingerprint(settings, run, message)
    message.content = "valid"
    run.config_snapshot = {"too_large": "x" * (2 * 1024 * 1024)}
    with pytest.raises(auth.OwnerAuthenticationError):
        auth.run_fingerprint(settings, run, message)


def test_domains_are_distinct_and_auth_does_not_log_credentials(data, caplog):
    settings, owner, run, message = data
    challenge = auth.new_challenge("v10r_" + "c" * 32)
    response = auth.make_challenge_response(settings, owner, challenge)
    origin = auth.make_run_origin(settings, owner, run, message)
    assert not auth.verify_run_origin(settings, {**origin, "mac": response["mac"]},
                                      owner=owner, run=run, message=message)
    assert auth.verify_challenge_response(settings, {**response, "mac": origin["mac"]}, challenge=challenge) is None
    assert "private user requirement" not in caplog.text
    assert "credential-value" not in caplog.text
    assert response["mac"] not in caplog.text and origin["mac"] not in caplog.text
