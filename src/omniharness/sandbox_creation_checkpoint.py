"""Paired creation snapshots and offline recovery checks, never live restore.

An application rollback must retain the independent live journals. These tools
do not repair mismatches, authorize external dispatch, or release resources.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .sandbox_creation_journal import CreationBinding, CreationJournal
from .sandbox_creation_witness import WitnessedCreationJournal

FILES = {"journal.sqlite", "witness.sqlite", "manifest.json"}
QUERY = ("SELECT instance_id,operation_id,run_id,fingerprint,service_fingerprint,state,sandbox_id "
         "FROM sandbox_creation_journal ORDER BY instance_id,operation_id")


class CreationCheckpointError(ValueError):
    pass


def _identity(service_id, engine_id):
    if (not isinstance(service_id, str) or not re.fullmatch(r"[a-f0-9]{64}", service_id)
            or not isinstance(engine_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", engine_id)):
        raise CreationCheckpointError("Invalid checkpoint service/engine identity")


def _directory(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True) or not path.is_dir():
        raise CreationCheckpointError("Checkpoint directories must be absolute and without links")
    if os.name == "posix":
        info = path.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise CreationCheckpointError("Checkpoint directory must be private to its owner")
        for parent in path.parents:
            info = parent.stat()
            if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise CreationCheckpointError("Checkpoint ancestors must not be shared-writable")
    return path


def _regular(path):
    if path != path.resolve(strict=True):
        raise CreationCheckpointError("Checkpoint files must not be links")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CreationCheckpointError("Checkpoint files must be regular and singly linked")
    if os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise CreationCheckpointError("Checkpoint file must be private to its owner")


def _new_directory(path, *, excluded=()):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve():
        raise CreationCheckpointError("Checkpoint target must be absolute and without links")
    if any(root == path or root in path.parents or path in root.parents for root in excluded):
        raise CreationCheckpointError("Checkpoint target must be separate from its source stores")
    _directory(path.parent)
    path.mkdir(mode=0o700, exist_ok=False)
    return _directory(path)


def _write_new(path, data):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _copy_new(source, destination):
    with source.open("rb") as original:
        with os.fdopen(os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as target:
            for block in iter(lambda: original.read(1048576), b""):
                target.write(block)
            target.flush()
            os.fsync(target.fileno())


def _sync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _hash(path):
    _regular(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _records(db):
    cursor = db.execute(QUERY)
    while rows := cursor.fetchmany(512):
        for row in rows:
            CreationBinding(row[0], row[2], row[1], row[3], row[4])
            if not ((row[5] == "DISPATCHED" and row[6] is None) or
                    (row[5] == "CREATED" and isinstance(row[6], str)
                     and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row[6]))):
                raise CreationCheckpointError("Invalid creation history in checkpoint")
            yield row


def _summary(db):
    digest = hashlib.sha256()
    counts = {"DISPATCHED": 0, "CREATED": 0}
    for row in _records(db):
        digest.update(json.dumps(row, separators=(",", ":"), ensure_ascii=True).encode() + b"\n")
        counts[row[5]] += 1
    return {"records": sum(counts.values()), "states": counts, "sha256": digest.hexdigest()}


@contextmanager
def _snapshot(path, identity):
    _regular(path)
    # Immutable backup files have no WAL or writers; never create journal files
    # while inspecting an offline checkpoint.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise CreationCheckpointError("SQLite checkpoint integrity failed")
        if db.execute("SELECT id FROM journal_identity").fetchall() != [(identity,)]:
            raise CreationCheckpointError("Checkpoint store identity differs")
        yield db


def _copy_database(journal, destination):
    _write_new(destination, b"")
    # A backup on the connection holding BEGIN IMMEDIATE waits on itself.
    # Independent read connections observe the stable last commit while the
    # paired write locks prevent any legitimate writer changing either source.
    with closing(sqlite3.connect(journal.path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
    # Windows FlushFileBuffers requires a writable handle even when no bytes
    # are changed; Linux accepts this mode too.
    with destination.open("r+b") as stream:
        os.fsync(stream.fileno())


def capture_checkpoint(pair, directory, *, service_id, engine_id):
    """Copy both histories under paired locks; publish the manifest last."""
    _identity(service_id, engine_id)
    directory = _new_directory(directory, excluded=(pair.journal.path.resolve().parent,
                                                    pair.witness.path.resolve().parent))
    with pair._pair() as (primary, _witness):
        expected = _summary(primary)
        _copy_database(pair.journal, directory / "journal.sqlite")
        _copy_database(pair.witness, directory / "witness.sqlite")
        for name, store in (("journal.sqlite", pair.journal), ("witness.sqlite", pair.witness)):
            with _snapshot(directory / name, store.journal_id) as db:
                if _summary(db) != expected:
                    raise CreationCheckpointError("Exported history differs from its locked source")
        manifest = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                    "service_id": service_id, "engine_id": engine_id,
                    "journal_id": pair.journal.journal_id, "witness_id": pair.witness.journal_id,
                    "history": expected,
                    "files": {name: _hash(directory / name) for name in sorted(FILES - {"manifest.json"})}}
        _write_new(directory / "manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
        _sync_directory(directory)
    return manifest


def verify_checkpoint(directory):
    """Read only: check exact files, identities, checksums and both histories."""
    directory = _directory(directory)
    if {path.name for path in directory.iterdir()} != FILES:
        raise CreationCheckpointError("Checkpoint files are incomplete or include unexpected files")
    path = directory / "manifest.json"
    _regular(path)
    if path.stat().st_size > 32768:
        raise CreationCheckpointError("Checkpoint manifest is too large")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or set(manifest) != {"schema_version", "created_at", "service_id", "engine_id", "journal_id",
                                 "witness_id", "history", "files"}):
        raise CreationCheckpointError("Unsupported checkpoint manifest")
    _identity(manifest["service_id"], manifest["engine_id"])
    ids = [manifest["journal_id"], manifest["witness_id"]]
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in ids) or ids[0] == ids[1]:
        raise CreationCheckpointError("Invalid checkpoint store identities")
    if manifest["files"] != {name: _hash(directory / name) for name in sorted(FILES - {"manifest.json"})}:
        raise CreationCheckpointError("Checkpoint file checksums differ")
    for name, identity in zip(("journal.sqlite", "witness.sqlite"), ids, strict=True):
        with _snapshot(directory / name, identity) as db:
            if _summary(db) != manifest["history"]:
                raise CreationCheckpointError("Checkpoint creation histories differ")
    return manifest


def verify_retained_history(pair, directory, *, service_id, engine_id):
    """Require live barriers to retain every backup record, allowing progress.

    This is necessary for a business-data restore, not sufficient authorization
    to restore that data or proof that an external operation has stopped.
    """
    manifest = verify_checkpoint(directory)
    if (manifest["service_id"] != service_id or manifest["engine_id"] != engine_id
            or manifest["journal_id"] != pair.journal.journal_id
            or manifest["witness_id"] != pair.witness.journal_id):
        raise CreationCheckpointError("Retained storage belongs to another service or engine")
    with pair._pair() as (primary, _witness):
        with _snapshot(Path(directory) / "journal.sqlite", manifest["journal_id"]) as backup:
            for row in _records(backup):
                current = primary.execute(QUERY.split(" ORDER BY")[0] + " WHERE instance_id=? AND operation_id=?",
                                          row[:2]).fetchone()
                if (current is None or current[:5] != row[:5]
                        or (row[5] == "CREATED" and current[5:] != row[5:])
                        or (row[5] == "DISPATCHED" and current[5] not in {"DISPATCHED", "CREATED"})):
                    raise CreationCheckpointError("Retained history lost or changed a checkpoint barrier")
        current = _summary(primary)
    return {"history_retained": True, "backup_records": manifest["history"]["records"],
            "live_records": current["records"], "live_history_sha256": current["sha256"],
            "live_stores_restored": False, "external_operations_verified": False}


def rehearse_checkpoint(directory, target):
    """Restore into NEW offline stores and prove no prior claim is reusable."""
    manifest = verify_checkpoint(directory)
    directory = Path(directory)
    target = _new_directory(target, excluded=(directory,))
    journals = []
    for name, filename, identity in (("primary", "journal.sqlite", manifest["journal_id"]),
                                      ("witness", "witness.sqlite", manifest["witness_id"])):
        folder = target / name
        folder.mkdir(mode=0o700)
        destination = folder / filename
        _copy_new(directory / filename, destination)
        if _hash(destination) != manifest["files"][filename]:
            raise CreationCheckpointError("Offline restore differs from its checkpoint")
        journals.append(CreationJournal(destination, identity))
    pair = WitnessedCreationJournal(*journals)
    pair.verify_consistency()
    checked = 0
    # Reuse the journal's transactional claim primitive inside ONE paired
    # transaction; a full pair scan per record would make maintenance O(N²).
    with pair._pair() as (primary, witness):
        with _snapshot(directory / "journal.sqlite", manifest["journal_id"]) as db:
            for row in _records(db):
                binding = CreationBinding(row[0], row[2], row[1], row[3], row[4])
                if (pair.journal._claim_in_transaction(primary, binding)
                        or pair.witness._claim_in_transaction(witness, binding)):
                    raise CreationCheckpointError("Restored history authorized a repeated dispatch")
                checked += 1
    report = {"offline_restore_verified": True, "duplicate_claims_refused": checked,
              "unresolved_dispatches": manifest["history"]["states"]["DISPATCHED"],
              "live_stores_restored": False, "external_requests_sent": 0, "slots_released": 0}
    _write_new(target / "rehearsal.json", json.dumps(report, indent=2).encode("utf-8"))
    _sync_directory(target)
    return report
