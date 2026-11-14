import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from omniharness.sandbox_creation_journal import CreationBinding, CreationBindingError, CreationJournal
from omniharness.sandbox_creation_supervisor import CreationAlreadyDispatched, CreationSupervisor
from omniharness.sandbox_creation_witness import WitnessedCreationJournal


@pytest.fixture
def stores(tmp_path):
    for name in ("primary", "independent"):
        (tmp_path / name).mkdir()
    primary = CreationJournal.initialize(tmp_path / "primary/journal.sqlite", "a" * 64)
    witness = CreationJournal.initialize(tmp_path / "independent/witness.sqlite", "b" * 64)
    return primary, witness


def backup(journal, path):
    with sqlite3.connect(journal.path) as source, sqlite3.connect(path) as target:
        source.backup(target)
    return CreationJournal(path, journal.journal_id)


@pytest.mark.parametrize("restore_side", [0, 1])
def test_one_sided_restore_never_authorizes_replay_or_reclamation(stores, restore_side):
    primary, witness = stores
    binding = CreationBinding("test", "run", "op", "c" * 64, "d" * 64)
    old = backup(stores[restore_side], stores[restore_side].path.with_name("old.sqlite"))
    journal = WitnessedCreationJournal(primary, witness)
    assert journal.claim_dispatch(binding)
    journal.record_created(binding, "sandbox-1")
    restored = WitnessedCreationJournal(old, witness) if restore_side == 0 else WitnessedCreationJournal(primary, old)
    with pytest.raises(CreationBindingError):
        restored.lookup(binding)
    with pytest.raises(CreationBindingError):
        restored.claim_dispatch(binding)


def test_partial_claim_or_receipt_cannot_be_repaired_implicitly(stores):
    primary, witness = stores
    binding = CreationBinding("test", "run", "op", "c" * 64, "d" * 64)
    journal = WitnessedCreationJournal(primary, witness)
    assert witness.claim_dispatch(binding)
    with pytest.raises(CreationBindingError):
        journal.claim_dispatch(binding)
    assert primary.lookup(binding) is None
    assert primary.claim_dispatch(binding)
    witness.record_created(binding, "sandbox-1")
    with pytest.raises(CreationBindingError):
        journal.record_created(binding, "sandbox-1")
    assert primary.lookup(binding).state == "DISPATCHED"


@pytest.mark.asyncio
async def test_docker_prune_does_not_erase_witness_dispatch_right(stores):
    journal = WitnessedCreationJournal(*stores)
    binding = CreationBinding("test", "run", "op", "c" * 64, "d" * 64)
    markers, calls = set(), []

    async def create():
        markers.add("marker")
        calls.append("create")
        return "sandbox-1"

    await CreationSupervisor(journal).create(binding, create)
    markers.clear()  # Model Docker prune independently of both durable stores.
    with pytest.raises(CreationAlreadyDispatched):
        await CreationSupervisor(journal).create(binding, create)
    assert calls == ["create"]


def test_same_directory_is_not_independent(tmp_path):
    first = CreationJournal.initialize(tmp_path / "a.sqlite", "a" * 64)
    second = CreationJournal.initialize(tmp_path / "b.sqlite", "b" * 64)
    with pytest.raises(CreationBindingError):
        WitnessedCreationJournal(first, second)


def test_restoring_one_store_blocks_a_new_operation_too(stores):
    primary, witness = stores
    old = backup(primary, primary.path.with_name("before.sqlite"))
    old_binding = CreationBinding("test", "run", "old-op", "c" * 64, "d" * 64)
    journal = WitnessedCreationJournal(primary, witness)
    assert journal.claim_dispatch(old_binding)
    journal.record_created(old_binding, "sandbox-old")
    restored = WitnessedCreationJournal(old, witness)
    new_binding = CreationBinding("test", "different-run", "new-op", "e" * 64, "f" * 64)
    with pytest.raises(CreationBindingError):
        restored.claim_dispatch(new_binding)
    assert old.lookup(new_binding) is None and witness.lookup(new_binding) is None


def test_concurrent_pair_clients_never_see_an_ordinary_half_commit(stores):
    def claim(index):
        pair = WitnessedCreationJournal(*stores)
        binding = CreationBinding("test", "run", "same-op", "c" * 64, "d" * 64)
        return pair.claim_dispatch(binding)

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(claim, range(24))).count(True) == 1
    WitnessedCreationJournal(*stores).verify_consistency()


def test_process_death_between_commits_blocks_all_new_dispatch(stores):
    primary, witness = stores
    program = '''import os,sys
from omniharness.sandbox_creation_journal import CreationBinding,CreationJournal
from omniharness.sandbox_creation_witness import WitnessedCreationJournal
from pathlib import Path
left=CreationJournal(Path(sys.argv[1]),'a'*64); right=CreationJournal(Path(sys.argv[2]),'b'*64)
connect=left._connect
class CrashAtCommit:
 def __init__(self,db): self.db=db
 def __getattr__(self,name): return getattr(self.db,name)
 def commit(self): os._exit(47)
left._connect=lambda:CrashAtCommit(connect())
WitnessedCreationJournal(left,right).claim_dispatch(CreationBinding('test','run','old-op','c'*64,'d'*64))
'''
    result = subprocess.run([sys.executable, "-c", program, str(primary.path), str(witness.path)],
                            env=os.environ.copy(), timeout=20, capture_output=True)
    assert result.returncode == 47
    old_binding = CreationBinding("test", "run", "old-op", "c" * 64, "d" * 64)
    assert primary.lookup(old_binding) is None and witness.lookup(old_binding).state == "DISPATCHED"
    pair = WitnessedCreationJournal(primary, witness)
    with pytest.raises(CreationBindingError):
        pair.verify_consistency()
    with pytest.raises(CreationBindingError):
        pair.claim_dispatch(CreationBinding("test", "run", "new-op", "e" * 64, "f" * 64))
