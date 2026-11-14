"""Candidate journal tests; no production storage or Docker is used."""
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from omniharness.sandbox_creation_journal import CreationBinding, CreationBindingError, CreationJournal

JOURNAL_ID = "f" * 64


@pytest.fixture
def binding():
    return CreationBinding("isolated", "run-1", "a" * 64, "b" * 64, "c" * 64)


def test_concurrent_claim_has_exactly_one_dispatch_owner(tmp_path, binding):
    path = tmp_path / "journal.sqlite"
    CreationJournal.initialize(path, JOURNAL_ID)

    def claim(_):
        return CreationJournal(path, JOURNAL_ID).claim_dispatch(binding)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(16)))
    assert results.count(True) == 1
    assert CreationJournal(path, JOURNAL_ID).lookup(binding).state == "DISPATCHED"


def test_process_death_after_dispatch_keeps_unknown_and_prevents_retry(tmp_path, binding):
    path = tmp_path / "journal.sqlite"
    CreationJournal.initialize(path, JOURNAL_ID)
    script = """import os,sys
from omniharness.sandbox_creation_journal import CreationBinding,CreationJournal
b=CreationBinding('isolated','run-1','a'*64,'b'*64,'c'*64)
assert CreationJournal(sys.argv[1],'f'*64).claim_dispatch(b)
os._exit(37)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], env=os.environ.copy(), timeout=20)
    assert result.returncode == 37
    journal = CreationJournal(path, JOURNAL_ID)
    assert journal.lookup(binding).state == "DISPATCHED"
    assert journal.lookup(binding).sandbox_id is None
    assert journal.claim_dispatch(binding) is False


@pytest.mark.parametrize("field,value", [("run_id", "other"), ("fingerprint", "d" * 64),
                                          ("service_fingerprint", "e" * 64)])
def test_changed_binding_cannot_reuse_or_update_record(tmp_path, binding, field, value):
    journal = CreationJournal.initialize(tmp_path / "journal.sqlite", JOURNAL_ID)
    assert journal.claim_dispatch(binding)
    other = replace(binding, **{field: value})
    for action in (lambda: journal.lookup(other), lambda: journal.claim_dispatch(other),
                   lambda: journal.record_created(other, "sandbox-1")):
        with pytest.raises(CreationBindingError):
            action()
    assert journal.lookup(binding).state == "DISPATCHED"


def test_created_receipt_is_immutable_and_survives_restart(tmp_path, binding):
    path = tmp_path / "journal.sqlite"
    journal = CreationJournal.initialize(path, JOURNAL_ID)
    with pytest.raises(CreationBindingError):
        journal.record_created(binding, "sandbox-1")
    assert journal.claim_dispatch(binding)
    receipt = journal.record_created(binding, "sandbox-1")
    reopened = CreationJournal(path, JOURNAL_ID)
    assert reopened.lookup(binding) == receipt
    assert reopened.record_created(binding, "sandbox-1") == receipt
    assert reopened.claim_dispatch(binding) is False
    with pytest.raises(CreationBindingError):
        reopened.record_created(binding, "sandbox-2")
    assert reopened.lookup(binding) == receipt


def test_invalid_identity_never_becomes_a_receipt(tmp_path, binding):
    journal = CreationJournal.initialize(tmp_path / "journal.sqlite", JOURNAL_ID)
    assert journal.claim_dispatch(binding)
    for identity in ("", "a/b", "x" * 65, None):
        with pytest.raises(CreationBindingError):
            journal.record_created(binding, identity)
    assert journal.lookup(binding).state == "DISPATCHED"


def test_missing_or_replaced_journal_never_reinitializes_implicitly(tmp_path, binding):
    path = tmp_path / "journal.sqlite"
    journal = CreationJournal.initialize(path, JOURNAL_ID)
    assert journal.claim_dispatch(binding)
    path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        journal.claim_dispatch(binding)
    assert not path.exists()
    CreationJournal.initialize(path, "e" * 64)
    with pytest.raises(CreationBindingError, match="identity changed"):
        journal.claim_dispatch(binding)
    with pytest.raises(FileExistsError):
        CreationJournal.initialize(path, JOURNAL_ID)
