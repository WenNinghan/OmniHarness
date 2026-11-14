"""Paired backup/recovery checks never access Docker, models or live databases."""
from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from omniharness import sandbox_creation_checkpoint as checkpoint
from omniharness.sandbox_creation_journal import CreationBinding, CreationBindingError, CreationJournal
from omniharness.sandbox_creation_witness import WitnessedCreationJournal

SERVICE = "d" * 64
ENGINE = "test-engine"


@pytest.fixture
def pair(tmp_path):
    journals = []
    for name, identity in (("primary", "a" * 64), ("witness", "b" * 64)):
        directory = tmp_path / name
        directory.mkdir(mode=0o700)
        journals.append(CreationJournal.initialize(directory / "state.sqlite", identity))
    return WitnessedCreationJournal(*journals)


def binding(index=1):
    return CreationBinding("test", "run-1", f"operation-{index}", "b" * 64, "c" * 64)


def capture(pair, target):
    return checkpoint.capture_checkpoint(pair, target, service_id=SERVICE, engine_id=ENGINE)


def retained(pair, directory, **kwargs):
    return checkpoint.verify_retained_history(pair, directory, service_id=kwargs.get("service_id", SERVICE),
                                             engine_id=kwargs.get("engine_id", ENGINE))


def test_checkpoint_restores_complete_history_without_enabling_replay(pair, tmp_path):
    pair.claim_dispatch(binding())
    pair.claim_dispatch(binding(2))
    pair.record_created(binding(2), "sandbox-2")
    before = pair.lookup(binding(2))
    directory = tmp_path / "backup"
    manifest = capture(pair, directory)
    assert checkpoint.verify_checkpoint(directory) == manifest
    assert manifest["history"]["states"] == {"DISPATCHED": 1, "CREATED": 1}
    files = {p.name: p.read_bytes() for p in directory.iterdir()}
    report = checkpoint.rehearse_checkpoint(directory, tmp_path / "rehearsal")
    assert report == {"offline_restore_verified": True, "duplicate_claims_refused": 2,
                      "unresolved_dispatches": 1, "live_stores_restored": False,
                      "external_requests_sent": 0, "slots_released": 0}
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == files
    assert pair.lookup(binding(2)) == before and pair.lookup(binding()).sandbox_id is None
    assert not pair.claim_dispatch(binding())


def test_empty_checkpoint_is_valid_but_does_not_claim_prior_coverage(pair, tmp_path):
    manifest = capture(pair, tmp_path / "backup")
    report = checkpoint.rehearse_checkpoint(tmp_path / "backup", tmp_path / "rehearsal")
    assert manifest["history"]["records"] == report["duplicate_claims_refused"] == 0


def test_business_restore_must_keep_newer_live_history(pair, tmp_path):
    pair.claim_dispatch(binding())
    directory = tmp_path / "backup"
    capture(pair, directory)
    pair.record_created(binding(), "sandbox-1")
    pair.claim_dispatch(binding(2))
    report = retained(pair, directory)
    assert report["backup_records"] == 1 and report["live_records"] == 2
    assert report["history_retained"] and not report["live_stores_restored"]
    assert pair.lookup(binding()).sandbox_id == "sandbox-1"
    assert not pair.claim_dispatch(binding(2))


@pytest.mark.parametrize("case", ["lost_record", "downgraded", "sandbox_changed", "binding_changed"])
def test_coordinated_old_or_changed_live_stores_do_not_satisfy_checkpoint(pair, tmp_path, case):
    pair.claim_dispatch(binding())
    pair.record_created(binding(), "sandbox-1")
    directory = tmp_path / "backup"
    capture(pair, directory)
    # Both stores remain consistent, so pair comparison alone cannot detect it.
    # The separately saved checkpoint supplies the additional history bound.
    for store in (pair.journal, pair.witness):
        with closing(store._connect()) as db:
            if case == "lost_record":
                db.execute("DELETE FROM sandbox_creation_journal")
            elif case == "downgraded":
                db.execute("UPDATE sandbox_creation_journal SET state='DISPATCHED',sandbox_id=NULL")
            elif case == "sandbox_changed":
                db.execute("UPDATE sandbox_creation_journal SET sandbox_id='different'")
            else:
                db.execute("UPDATE sandbox_creation_journal SET run_id='different'")
    with pytest.raises(checkpoint.CreationCheckpointError, match="lost or changed"):
        retained(pair, directory)


@pytest.mark.parametrize("changed", [{"service_id": "f" * 64}, {"engine_id": "other"}])
def test_foreign_service_or_engine_cannot_approve_retained_history(pair, tmp_path, changed):
    capture(pair, tmp_path / "backup")
    with pytest.raises(checkpoint.CreationCheckpointError, match="another service or engine"):
        retained(pair, tmp_path / "backup", **changed)


def test_replaced_store_identity_and_single_sided_change_are_rejected(pair, tmp_path):
    capture(pair, tmp_path / "backup")
    pair.journal.claim_dispatch(binding())
    with pytest.raises(CreationBindingError, match="witness differ"):
        retained(pair, tmp_path / "backup")
    with pytest.raises(CreationBindingError, match="witness differ"):
        capture(pair, tmp_path / "inconsistent")
    assert not (tmp_path / "inconsistent/manifest.json").exists()


@pytest.mark.parametrize("kind", ["tampered", "missing", "extra", "wal", "identity", "history", "schema"])
def test_incomplete_or_modified_checkpoint_is_refused(pair, tmp_path, kind):
    directory = tmp_path / "backup"
    capture(pair, directory)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if kind == "tampered":
        (directory / "journal.sqlite").write_bytes(b"changed")
    elif kind == "missing":
        (directory / "witness.sqlite").unlink()
    elif kind in {"extra", "wal"}:
        (directory / ("notes.txt" if kind == "extra" else "journal.sqlite-wal")).write_text("unexpected")
    else:
        if kind == "identity":
            manifest["journal_id"] = "e" * 64
        elif kind == "history":
            manifest["history"]["records"] = 999
        else:
            manifest["schema_version"] = 2
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises((checkpoint.CreationCheckpointError, sqlite3.Error)):
        checkpoint.verify_checkpoint(directory)


def test_backup_does_not_overwrite_targets_or_write_inside_live_store(pair, tmp_path):
    target = tmp_path / "existing"
    target.mkdir(mode=0o700)
    (target / "keep").write_text("owned data")
    with pytest.raises(FileExistsError):
        capture(pair, target)
    assert (target / "keep").read_text() == "owned data"
    with pytest.raises(checkpoint.CreationCheckpointError, match="separate"):
        capture(pair, pair.journal.path.parent / "nested")
    with pytest.raises(checkpoint.CreationCheckpointError, match="absolute"):
        capture(pair, Path("relative-backup"))


def test_restore_cannot_overwrite_checkpoint_or_an_existing_directory(pair, tmp_path):
    target = tmp_path / "backup"
    manifest = capture(pair, target)
    with pytest.raises(checkpoint.CreationCheckpointError, match="separate"):
        checkpoint.rehearse_checkpoint(target, target / "nested")
    (tmp_path / "existing").mkdir(mode=0o700)
    with pytest.raises(FileExistsError):
        checkpoint.rehearse_checkpoint(target, tmp_path / "existing")
    assert checkpoint.verify_checkpoint(target) == manifest


def test_partial_export_has_no_valid_manifest_and_leaves_live_barrier(pair, tmp_path, monkeypatch):
    pair.claim_dispatch(binding())
    original = checkpoint._copy_database

    def interrupted(store, destination):
        if store is pair.witness:
            raise OSError("simulated storage failure")
        return original(store, destination)

    monkeypatch.setattr(checkpoint, "_copy_database", interrupted)
    target = tmp_path / "partial"
    with pytest.raises(OSError, match="storage failure"):
        capture(pair, target)
    assert (target / "journal.sqlite").is_file() and not (target / "manifest.json").exists()
    assert not pair.claim_dispatch(binding())
    with pytest.raises(checkpoint.CreationCheckpointError, match="incomplete"):
        checkpoint.verify_checkpoint(target)


def test_concurrent_claims_and_checkpoints_never_capture_a_half_pair(pair, tmp_path):
    def claim(index):
        if pair.claim_dispatch(binding(index)):
            pair.record_created(binding(index), f"sandbox-{index}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        work = [pool.submit(claim, index) for index in range(24)]
        snapshots = [pool.submit(capture, pair, tmp_path / f"backup-{index}") for index in range(4)]
        for future in work:
            future.result(timeout=30)
        for index, future in enumerate(snapshots):
            assert checkpoint.verify_checkpoint(tmp_path / f"backup-{index}") == future.result(timeout=30)
            assert retained(pair, tmp_path / f"backup-{index}")["live_records"] == 24


def test_hashes_alone_do_not_accept_an_invalid_sqlite_database(pair, tmp_path):
    directory = tmp_path / "backup"
    capture(pair, directory)
    (directory / "journal.sqlite").write_bytes(b"not a sqlite database")
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["files"]["journal.sqlite"] = checkpoint._hash(directory / "journal.sqlite")
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(sqlite3.DatabaseError):
        checkpoint.verify_checkpoint(directory)


def test_rebinding_a_known_operation_is_still_rejected_after_rehearsal(pair, tmp_path):
    pair.claim_dispatch(binding())
    capture(pair, tmp_path / "backup")
    checkpoint.rehearse_checkpoint(tmp_path / "backup", tmp_path / "offline")
    primary = CreationJournal(tmp_path / "offline/primary/journal.sqlite", pair.journal.journal_id)
    with pytest.raises(CreationBindingError, match="binding differs"):
        primary.claim_dispatch(replace(binding(), run_id="another-run"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX storage ownership/mode check")
def test_shared_backup_directory_is_refused(pair, tmp_path):
    directory = tmp_path / "backup"
    capture(pair, directory)
    directory.chmod(0o755)
    with pytest.raises(checkpoint.CreationCheckpointError, match="private"):
        checkpoint.verify_checkpoint(directory)
