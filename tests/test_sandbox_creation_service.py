import json
from types import SimpleNamespace

import pytest

from omniharness import sandbox_creation_service as service
from omniharness.sandbox_creation_checkpoint import capture_checkpoint
from omniharness.sandbox_creation_journal import CreationBinding, CreationJournal
from omniharness.sandbox_creation_service import (
    CreationServiceSettings,
    build_creation_service,
    service_identity,
)
from omniharness.sandbox_creation_witness import WitnessedCreationJournal


def configuration(tmp_path):
    settings = SimpleNamespace(instance_id="test", sandbox_endpoint="http://controller:8080",
        sandbox_image="sha256:" + "a" * 64, sandbox_snapshot_root="/snapshots",
        sandbox_max_command_seconds=600, database_url="sqlite:///unused.sqlite")
    storage = CreationServiceSettings(journal_path=tmp_path / "journal/creation.sqlite", journal_id="b" * 64,
        witness_path=tmp_path / "witness/witness.sqlite", witness_id="c" * 64,
        engine_id="test-engine", marker_image="sha256:" + "d" * 64)
    return settings, storage


def test_identity_commits_to_store_engine_and_tool_profile(tmp_path):
    settings, storage = configuration(tmp_path)
    identity = service_identity(settings, storage)
    assert len(identity) == 64
    for change in ({"witness_id": "e" * 64}, {"journal_id": "f" * 64}, {"engine_id": "other"}):
        assert service_identity(settings, storage.model_copy(update=change)) != identity
    settings.sandbox_image = "sha256:" + "0" * 64
    assert service_identity(settings, storage) != identity


def test_standalone_service_cannot_use_unlocked_sqlite_authorization(tmp_path):
    settings, storage = configuration(tmp_path)
    with pytest.raises(ValueError, match="PostgreSQL row locks"):
        build_creation_service(settings, storage)
    assert not storage.journal_path.exists() and not storage.witness_path.exists()


@pytest.mark.parametrize("action", ["verify-checkpoint", "rehearse-checkpoint"])
def test_offline_checkpoint_commands_do_not_load_live_configuration(tmp_path, monkeypatch, capsys, action):
    stores = []
    for name, identity in (("primary", "a" * 64), ("witness", "b" * 64)):
        directory = tmp_path / name
        directory.mkdir(mode=0o700)
        stores.append(CreationJournal.initialize(directory / "creation.sqlite", identity))
    pair = WitnessedCreationJournal(*stores)
    pair.claim_dispatch(CreationBinding("test", "run-1", "op-1", "c" * 64, "d" * 64))
    capture_checkpoint(pair, tmp_path / "backup", service_id="e" * 64, engine_id="test-engine")
    argv = ["service", action, "--directory", str(tmp_path / "backup")]
    if action == "rehearse-checkpoint":
        argv += ["--target", str(tmp_path / "offline")]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(service, "Settings", lambda: pytest.fail("Offline command loaded live settings"))
    monkeypatch.setattr(service, "CreationServiceSettings", lambda: pytest.fail("Offline command loaded live stores"))
    service.main()
    result = json.loads(capsys.readouterr().out)
    if action == "verify-checkpoint":
        assert result["history"]["records"] == 1
    else:
        assert result["duplicate_claims_refused"] == 1 and result["external_requests_sent"] == 0


@pytest.mark.parametrize("argv", [["checkpoint"], ["rehearse-checkpoint", "--directory", "/unused"],
    ["identity", "--directory", "/unused"], ["verify-checkpoint", "--directory", "/unused", "--target", "/unused"]])
def test_checkpoint_commands_require_explicit_and_appropriate_paths(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["service", *argv])
    monkeypatch.setattr(service, "Settings", lambda: pytest.fail("Invalid CLI must not load settings"))
    with pytest.raises(SystemExit) as error:
        service.main()
    assert error.value.code == 2


def test_live_checkpoint_refuses_a_different_configured_service_before_opening_stores(tmp_path, monkeypatch):
    settings, storage = configuration(tmp_path)
    monkeypatch.setattr("sys.argv", ["service", "checkpoint", "--directory", str(tmp_path / "backup")])
    monkeypatch.setattr(service, "Settings", lambda: settings)
    monkeypatch.setattr(service, "CreationServiceSettings", lambda: storage)
    monkeypatch.setattr(service, "validate_storage", lambda *_: None)
    monkeypatch.setattr(service, "creation_service_binding", lambda _: {"service_id": "0" * 64})
    with pytest.raises(ValueError, match="differs from its service identity"):
        service.main()
    assert not (tmp_path / "backup").exists()
