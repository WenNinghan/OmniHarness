"""Independent durable witness for creation, with conservative partial writes.

The two journals must have separately protected storage and recovery policies.
This detects a single-sided restore; it cannot detect a coordinated rollback
of both stores and the Docker marker. Never auto-repair a mismatching pair.
"""
from __future__ import annotations

from contextlib import closing, contextmanager

from .sandbox_creation_journal import CreationBindingError, CreationJournal


class WitnessedCreationJournal:
    def __init__(self, journal: CreationJournal, witness: CreationJournal):
        left, right = journal.path.resolve(), witness.path.resolve()
        if (left == right or left.parent == right.parent or left.parent in right.parents
                or right.parent in left.parents or journal.journal_id == witness.journal_id):
            raise CreationBindingError("Creation witness must have independent storage and identity")
        self.journal, self.witness = journal, witness

    @staticmethod
    def _verify_pair(primary, witness):
        query = ("SELECT instance_id,operation_id,run_id,fingerprint,service_fingerprint,state,sandbox_id "
                 "FROM sandbox_creation_journal ORDER BY instance_id,operation_id")
        left, right = primary.execute(query), witness.execute(query)
        while True:
            rows = left.fetchmany(512)
            if rows != right.fetchmany(512):
                raise CreationBindingError("Creation journal and independent witness differ")
            if not rows:
                return

    @contextmanager
    def _pair(self):
        # Every process takes the primary lock first, retaining it through both
        # commits. Concurrent clients cannot observe an ordinary half-commit.
        # A process/storage failure can leave a mismatch: never auto-repair it.
        with closing(self.journal._connect()) as primary:
            primary.execute("BEGIN IMMEDIATE")
            try:
                with closing(self.witness._connect()) as witness:
                    witness.execute("BEGIN IMMEDIATE")
                    try:
                        self._verify_pair(primary, witness)
                        yield primary, witness
                        witness.commit()
                        primary.commit()
                    except BaseException:
                        witness.rollback()
                        raise
            except BaseException:
                primary.rollback()
                raise

    def verify_consistency(self):
        with self._pair():
            pass

    def lookup(self, binding):
        with self._pair() as (primary, _witness):
            return self.journal._fetch(primary, binding)

    def claim_dispatch(self, binding) -> bool:
        with self._pair() as (primary, witness):
            if self.journal._fetch(primary, binding) is not None:
                return False
            self.witness._claim_in_transaction(witness, binding)
            self.journal._claim_in_transaction(primary, binding)
            return True

    def record_created(self, binding, sandbox_id):
        with self._pair() as (primary, witness):
            self.witness._created_in_transaction(witness, binding, sandbox_id)
            return self.journal._created_in_transaction(primary, binding, sandbox_id)
