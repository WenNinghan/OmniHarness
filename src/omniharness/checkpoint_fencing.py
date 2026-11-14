"""Opt-in PostgreSQL checkpoint fencing; not connected to product execution.

Only trusted host code may initialize or use this component.  It is not a
sandbox against a hostile plugin holding the same database privileges.  A
Worker's advisory-lock session MUST never release and reacquire the same lock:
after any lock loss it must close and a fresh backend must acquire a new epoch.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
CHECKPOINTER_VERSION = "3.2.0"
DEFAULT_SCHEMA = "omniharness_checkpoints"
SEALED_V10_FENCE_VERSION = "stage-agent-v10-sealed-v2"
WORKER_LOCK_KEY = 7243301201
_SCHEMA = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_THREAD = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_VERSION = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_DATA_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
_TRIGGER = "omni_checkpoint_owner_fence"


class FencingError(RuntimeError):
    """A safe static error, never containing database URLs or credentials."""


@dataclass(frozen=True)
class FenceTicket:
    thread_id: str
    epoch: int
    workflow_version: str
    owner_pid: int
    owner_started: datetime
    schema: str


def _schema(value: str) -> str:
    if not isinstance(value, str) or not _SCHEMA.fullmatch(value):
        raise ValueError("Invalid checkpoint schema name")
    return value


def _identity(value: str, pattern: re.Pattern, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def _conninfo(database_url: str) -> str:
    try:
        url = make_url(database_url)
    except (ArgumentError, TypeError, ValueError):
        raise ValueError("Invalid PostgreSQL connection URL") from None
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("A PostgreSQL connection is required")
    # search_path and session options must be controlled by this component.
    if "options" in url.query:
        raise ValueError("Connection URL options are not supported")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


async def _connect(database_url: str):
    try:
        return await psycopg.AsyncConnection.connect(_conninfo(database_url), autocommit=True,
                                                      prepare_threshold=0, row_factory=dict_row)
    except psycopg.Error:
        raise FencingError("PostgreSQL checkpoint connection failed") from None


def _function_body(schema: str) -> str:
    # schema was restricted to [a-z0-9_] before interpolation.
    return f"""
DECLARE
    target text; expected bigint; actual text; selected_thread text; selected_schema text;
    owner_backend integer; owner_start timestamptz; owner_key bigint;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.thread_id IS DISTINCT FROM NEW.thread_id THEN
        RAISE EXCEPTION 'checkpoint thread cannot change' USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'DELETE' THEN target := OLD.thread_id; ELSE target := NEW.thread_id; END IF;
    SELECT epoch, owner_pid, owner_started, lock_key
      INTO expected, owner_backend, owner_start, owner_key
      FROM {schema}.fences WHERE thread_id = target FOR SHARE;
    actual := current_setting('omni.owner_epoch', true);
    selected_thread := current_setting('omni.thread_id', true);
    selected_schema := current_setting('omni.checkpoint_schema', true);
    IF expected IS NULL OR actual IS DISTINCT FROM expected::text
       OR selected_thread IS DISTINCT FROM target OR selected_schema IS DISTINCT FROM TG_TABLE_SCHEMA THEN
        RAISE EXCEPTION 'checkpoint fencing rejected' USING ERRCODE = 'P0001';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_locks l
        JOIN pg_catalog.pg_stat_activity a ON a.pid=l.pid AND a.backend_start=owner_start
        WHERE l.locktype='advisory' AND l.mode='ExclusiveLock' AND l.granted AND l.pid=owner_backend
          AND l.classid::bigint=(owner_key >> 32) AND l.objid::bigint=(owner_key & 4294967295)
          AND l.objsubid=1
          AND l.database=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
    ) THEN
        RAISE EXCEPTION 'checkpoint owner lock lost' USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END
"""


async def _set_path(conn, schema: str):
    await conn.execute("SELECT set_config('search_path', %s, false)", (f"{schema},pg_catalog",))


async def _verify(conn, schema: str, *, legacy: bool = False) -> None:
    """Fail closed on unknown version, changed trigger/function or missing tables."""
    try:
        async with conn.cursor() as cur:
            await cur.execute(sql.SQL("SELECT version, stage FROM {}.component_metadata WHERE id=1")
                              .format(sql.Identifier(schema)))
            row = await cur.fetchone()
            expected_version = LEGACY_SCHEMA_VERSION if legacy else SCHEMA_VERSION
            if not row or row["version"] != expected_version or row["stage"] != "ready":
                raise FencingError("Checkpoint fencing schema is not ready or compatible")
            if version("langgraph-checkpoint-postgres") != CHECKPOINTER_VERSION:
                raise FencingError("Checkpoint saver library version is incompatible")
            await cur.execute(sql.SQL("SELECT array_agg(v ORDER BY v) AS versions FROM {}.checkpoint_migrations")
                              .format(sql.Identifier(schema)))
            migration = await cur.fetchone()
            if not migration or migration["versions"] != list(range(len(AsyncPostgresSaver.MIGRATIONS))):
                raise FencingError("Checkpoint saver schema version is incompatible")
            await cur.execute("""
                SELECT p.prosrc FROM pg_catalog.pg_proc p
                JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
                WHERE n.nspname=%s AND p.proname='fence_checkpoint' AND p.pronargs=0
            """, (schema,))
            body = await cur.fetchone()
            if not body or body["prosrc"] != _function_body(schema):
                raise FencingError("Checkpoint fencing function is missing or changed")
            await cur.execute("""
                SELECT c.relname, t.tgenabled, t.tgtype, t.tgqual IS NULL AS unconditional,
                       p.proname, pn.nspname AS fn_schema
                FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
                JOIN pg_catalog.pg_namespace pn ON pn.oid=p.pronamespace
                WHERE NOT t.tgisinternal AND n.nspname=%s AND t.tgname=%s
            """, (schema, _TRIGGER))
            triggers = await cur.fetchall()
            by_table = {item["relname"]: item for item in triggers}
            if set(by_table) != set(_DATA_TABLES) or any(
                item["tgenabled"] not in ({"O", "A"} if legacy else {"A"})
                or item["tgtype"] != 31 or not item["unconditional"]
                or item["proname"] != "fence_checkpoint" or item["fn_schema"] != schema
                for item in by_table.values()
            ):
                raise FencingError("Checkpoint fencing triggers are missing or changed")
    except psycopg.Error:
        raise FencingError("Cannot verify checkpoint fencing schema") from None


async def initialize_checkpoint_schema(database_url: str, *, schema: str = DEFAULT_SCHEMA) -> None:
    """Explicit one-time versioned bootstrap. Never alters an unowned schema.

    This must run with migrations stopped and before opening any Agent saver.
    An incomplete owned installation may be safely resumed. Existing schemas
    without our marker, and already-ready installations with changed triggers,
    are rejected rather than overwritten.
    """
    schema = _schema(schema)
    if version("langgraph-checkpoint-postgres") != CHECKPOINTER_VERSION:
        raise FencingError("Checkpoint saver library version is incompatible")
    # A stable 63-bit per-schema lock serializes two initializer processes.
    lock_key = int.from_bytes(hashlib.sha256(("omni-init:" + schema).encode()).digest()[:8], "big") & (2**63 - 1)
    async with await _connect(database_url) as conn:
        # A blocking pg_advisory_lock statement holds a virtual transaction
        # while waiting. The official setup includes CREATE INDEX CONCURRENTLY,
        # which may wait for that transaction and form a database deadlock.
        # Short autocommit try-lock statements avoid holding a waiting snapshot.
        deadline = asyncio.get_running_loop().time() + 60
        while True:
            cursor = await conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,))
            if (await cursor.fetchone())["acquired"]:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise FencingError("Checkpoint initialization lock timed out")
            await asyncio.sleep(0.1)
        try:
            cur = await conn.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s", (schema,))
            exists = await cur.fetchone()
            if not exists:
                async with conn.transaction():
                    await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                    await conn.execute(sql.SQL("""
                        CREATE TABLE {}.component_metadata (
                            id SMALLINT PRIMARY KEY CHECK (id=1), version INTEGER NOT NULL,
                            stage TEXT NOT NULL CHECK (stage IN ('initializing', 'ready'))
                        )
                    """).format(sql.Identifier(schema)))
                    await conn.execute(sql.SQL("INSERT INTO {}.component_metadata VALUES (1, %s, 'initializing')")
                                       .format(sql.Identifier(schema)), (SCHEMA_VERSION,))
                    await conn.execute(sql.SQL("""
                        CREATE TABLE {}.fences (
                            thread_id TEXT PRIMARY KEY, epoch BIGINT NOT NULL CHECK (epoch > 0),
                            owner_pid INTEGER NOT NULL, owner_started TIMESTAMPTZ NOT NULL,
                            lock_key BIGINT NOT NULL, workflow_version TEXT NOT NULL
                        )
                    """).format(sql.Identifier(schema)))
            else:
                try:
                    cur = await conn.execute(sql.SQL("SELECT version, stage FROM {}.component_metadata WHERE id=1")
                                             .format(sql.Identifier(schema)))
                    marker = await cur.fetchone()
                except psycopg.Error:
                    raise FencingError("Existing checkpoint schema is not owned by this component") from None
                if not marker or marker["version"] != SCHEMA_VERSION:
                    raise FencingError("Existing checkpoint schema is not compatible")
                if marker["stage"] == "ready":
                    await _verify(conn, schema)
                    return
                if marker["stage"] != "initializing":
                    raise FencingError("Existing checkpoint schema has an invalid state")
            await _set_path(conn, schema)
            await AsyncPostgresSaver(conn).setup()
            async with conn.transaction():
                await conn.execute(sql.SQL("CREATE FUNCTION {}.fence_checkpoint() RETURNS trigger LANGUAGE plpgsql AS {}")
                                   .format(sql.Identifier(schema), sql.Literal(_function_body(schema))))
                for table in _DATA_TABLES:
                    await conn.execute(sql.SQL("CREATE TRIGGER {} BEFORE INSERT OR UPDATE OR DELETE ON {}.{} "
                                               "FOR EACH ROW EXECUTE FUNCTION {}.fence_checkpoint()")
                                       .format(sql.Identifier(_TRIGGER), sql.Identifier(schema),
                                               sql.Identifier(table), sql.Identifier(schema)))
                    await conn.execute(sql.SQL("ALTER TABLE {}.{} ENABLE ALWAYS TRIGGER {}")
                                       .format(sql.Identifier(schema), sql.Identifier(table),
                                               sql.Identifier(_TRIGGER)))
                await conn.execute(sql.SQL("UPDATE {}.component_metadata SET stage='ready' WHERE id=1")
                                   .format(sql.Identifier(schema)))
            await _verify(conn, schema)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


async def upgrade_legacy_checkpoint_schema(database_url: str, *, schema: str = DEFAULT_SCHEMA) -> None:
    """Explicit, atomic v1→v2 upgrade while all Workers are stopped.

    V1 accepted ordinary triggers, which can be disabled by a replica session.
    Do not silently modify a ready schema. Acquire both the initialization and
    Worker locks, verify the exact legacy definitions, then enable ALWAYS and
    advance metadata in one transaction. Existing checkpoint data is retained.
    """
    schema = _schema(schema)
    if version("langgraph-checkpoint-postgres") != CHECKPOINTER_VERSION:
        raise FencingError("Checkpoint saver library version is incompatible")
    init_key = int.from_bytes(hashlib.sha256(("omni-init:" + schema).encode()).digest()[:8], "big") & (2**63 - 1)
    async with await _connect(database_url) as conn:
        try:
            async with conn.transaction():
                for key in (WORKER_LOCK_KEY, init_key):
                    cursor = await conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS acquired", (key,))
                    if not (await cursor.fetchone())["acquired"]:
                        raise FencingError("Stop Workers and checkpoint initialization before upgrading")
                await _verify(conn, schema, legacy=True)
                for table in _DATA_TABLES:
                    await conn.execute(sql.SQL("ALTER TABLE {}.{} ENABLE ALWAYS TRIGGER {}")
                                       .format(sql.Identifier(schema), sql.Identifier(table),
                                               sql.Identifier(_TRIGGER)))
                await conn.execute(sql.SQL("UPDATE {}.component_metadata SET version=%s WHERE id=1")
                                   .format(sql.Identifier(schema)), (SCHEMA_VERSION,))
                await _verify(conn, schema)
        except psycopg.Error:
            raise FencingError("Checkpoint schema upgrade failed; transaction rolled back") from None


def register_fence(owner, thread_id: str, workflow_version: str, *, schema: str = DEFAULT_SCHEMA) -> FenceTicket:
    """Register a new Run epoch in the still-held WorkerLock's own transaction.

    The caller serializes access to owner and keeps it open until the saver is
    closed. Ownership loss must permanently dispose the owner connection.
    """
    schema = _schema(schema)
    thread_id = _identity(thread_id, _THREAD, "thread identity")
    workflow_version = _identity(workflow_version, _VERSION, "workflow version")
    if (owner is None or owner.lost or owner.connection is None
            or owner.engine.dialect.name != "postgresql"):
        raise FencingError("A live PostgreSQL WorkerLock is required")
    # The only interpolated identifier has already passed _schema's strict regex.
    try:
        with owner.session(None) as db:
            version = db.execute(text(f"SELECT version, stage FROM {schema}.component_metadata WHERE id=1")).one_or_none()
            if not version or version.version != SCHEMA_VERSION or version.stage != "ready":
                raise FencingError("Checkpoint fencing schema is not ready or compatible")
            identity = db.execute(text("""
                SELECT a.pid, a.backend_start FROM pg_catalog.pg_stat_activity a
                WHERE a.pid=pg_backend_pid() AND EXISTS (
                    SELECT 1 FROM pg_catalog.pg_locks l WHERE l.pid=a.pid AND l.locktype='advisory'
                    AND l.mode='ExclusiveLock' AND l.granted AND l.objsubid=1
                    AND l.classid::bigint=(:key >> 32) AND l.objid::bigint=(:key & 4294967295)
                    AND l.database=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
                )
            """), {"key": WORKER_LOCK_KEY}).one_or_none()
            if not identity:
                raise FencingError("Worker advisory lock is not held")
            frozen_v10 = None
            actual_workflow = workflow_version
            if workflow_version == "stage-agent-v10":
                from .v10_sealed_saver import classify_frozen_v10
                frozen_v10 = db.execute(text("SELECT workspace_id, workflow_version, config_snapshot FROM runs "
                                             "WHERE id=:run"), {"run": thread_id}).mappings().one_or_none()
                if not frozen_v10 or frozen_v10["workflow_version"] != workflow_version:
                    raise FencingError("Frozen v10 Run is unavailable")
                actual_workflow = classify_frozen_v10(owner.settings, thread_id, frozen_v10["workspace_id"],
                                                      frozen_v10["config_snapshot"])
            registered = db.execute(text(f"""
                INSERT INTO {schema}.fences(thread_id,epoch,owner_pid,owner_started,lock_key,workflow_version)
                VALUES (:thread,1,:pid,:started,:key,:workflow)
                ON CONFLICT (thread_id) DO UPDATE SET epoch={schema}.fences.epoch+1,
                    owner_pid=EXCLUDED.owner_pid, owner_started=EXCLUDED.owner_started, lock_key=EXCLUDED.lock_key
                WHERE {schema}.fences.workflow_version=EXCLUDED.workflow_version
                RETURNING epoch
            """), {"thread": thread_id, "pid": identity.pid, "started": identity.backend_start,
                   "key": WORKER_LOCK_KEY, "workflow": actual_workflow}).scalar_one_or_none()
            if registered is None:
                raise FencingError("Checkpoint workflow version is incompatible")
            if frozen_v10 is not None:
                locked = db.execute(text("SELECT workspace_id, workflow_version, config_snapshot FROM runs "
                                         "WHERE id=:run FOR UPDATE"), {"run": thread_id}).mappings().one_or_none()
                if not locked or dict(locked) != dict(frozen_v10):
                    raise FencingError("Frozen v10 Run changed during fence registration")
            return FenceTicket(thread_id, registered, actual_workflow, identity.pid, identity.backend_start, schema)
    except FencingError:
        raise
    except Exception:
        raise FencingError("Checkpoint owner could not be registered") from None


class BoundAsyncPostgresSaver(AsyncPostgresSaver):
    """Official saver limited to one Run; operations requiring deletion are disabled."""

    def __init__(self, conn, ticket: FenceTicket):
        super().__init__(conn)
        self.ticket = ticket

    async def setup(self):
        raise FencingError("Checkpoint setup requires the separate initialization workflow")

    def _bound(self, config):
        if not isinstance(config, dict) or config.get("configurable", {}).get("thread_id") != self.ticket.thread_id:
            raise FencingError("Checkpoint thread does not match the owner ticket")

    async def aget_tuple(self, config):
        self._bound(config)
        return await super().aget_tuple(config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        self._bound(config)
        if before is not None:
            self._bound(before)
        async for item in super().alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def aget_delta_channel_history(self, *, config, channels):
        self._bound(config)
        return await super().aget_delta_channel_history(config=config, channels=channels)

    async def aput(self, config, checkpoint, metadata, new_versions):
        self._bound(config)
        from .v10_saver_guard import begin_ordinary_write, end_ordinary_write
        guard = begin_ordinary_write(self.ticket)
        try:
            return await super().aput(config, checkpoint, metadata, new_versions)
        finally:
            end_ordinary_write(guard)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        self._bound(config)
        from .v10_saver_guard import begin_ordinary_write, end_ordinary_write
        guard = begin_ordinary_write(self.ticket)
        try:
            return await super().aput_writes(config, writes, task_id, task_path)
        finally:
            end_ordinary_write(guard)

    async def adelete_thread(self, thread_id):
        raise FencingError("Checkpoint deletion requires a separate maintenance workflow")

    async def aprune(self, thread_ids, *, strategy="keep_latest"):
        raise FencingError("Checkpoint pruning requires a separate maintenance workflow")

    async def acopy_thread(self, source_thread_id, target_thread_id):
        raise FencingError("Checkpoint copying requires a separate maintenance workflow")

    async def adelete_for_runs(self, run_ids):
        raise FencingError("Checkpoint deletion requires a separate maintenance workflow")


@asynccontextmanager
async def open_fenced_saver(database_url: str, ticket: FenceTicket):
    """Open one non-reconnecting saver session bound to one Run and epoch.

    On Windows the event loop must use WindowsSelectorEventLoopPolicy.  This
    context does not hold the owner's lock, protect external operations, or
    fence a separately created business SQLAlchemy Session.
    """
    if not isinstance(ticket, FenceTicket) or ticket.epoch < 1:
        raise FencingError("Invalid checkpoint ticket")
    schema = _schema(ticket.schema)
    _identity(ticket.thread_id, _THREAD, "thread identity")
    try:
        async with await _connect(database_url) as conn:
            await _verify(conn, schema)
            await _set_path(conn, schema)
            await conn.execute("SELECT set_config('omni.owner_epoch', %s, false)", (str(ticket.epoch),))
            await conn.execute("SELECT set_config('omni.thread_id', %s, false)", (ticket.thread_id,))
            await conn.execute("SELECT set_config('omni.checkpoint_schema', %s, false)", (schema,))
            cur = await conn.execute(sql.SQL("""
                SELECT 1 FROM {}.fences f JOIN pg_catalog.pg_stat_activity a
                  ON a.pid=f.owner_pid AND a.backend_start=f.owner_started
                JOIN pg_catalog.pg_locks l ON l.pid=a.pid AND l.locktype='advisory'
                  AND l.mode='ExclusiveLock' AND l.granted AND l.objsubid=1
                  AND l.classid::bigint=(f.lock_key >> 32) AND l.objid::bigint=(f.lock_key & 4294967295)
                  AND l.database=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
                WHERE f.thread_id=%s AND f.epoch=%s AND f.workflow_version=%s
                  AND f.owner_pid=%s AND f.owner_started=%s AND f.lock_key=%s
            """).format(sql.Identifier(schema)), (ticket.thread_id, ticket.epoch, ticket.workflow_version,
                                                    ticket.owner_pid, ticket.owner_started, WORKER_LOCK_KEY))
            if await cur.fetchone() is None:
                raise FencingError("Checkpoint owner is no longer valid")
            bound = BoundAsyncPostgresSaver(conn, ticket)
            try:
                yield bound
            finally:
                if ticket.workflow_version in {"stage-agent-v10", SEALED_V10_FENCE_VERSION}:
                    from .v10_saver_guard import current_execution_writer
                    saver = current_execution_writer(ticket)
                    if saver is not None and saver.conn is conn:
                        prior_error = sys.exc_info()[0] is not None
                        try:
                            await saver.retire_after_context()
                        except Exception as exc:
                            # The saver closes its phase before raising. A
                            # broken connection, confirmed owner loss or prior
                            # failure must not be replaced by a cleanup error.
                            from .v10_sealed_saver import SealedOwnerUnavailable
                            if not (prior_error or conn.closed or conn.broken
                                    or isinstance(exc, SealedOwnerUnavailable)):
                                raise
    finally:
        if ticket.workflow_version in {"stage-agent-v10", SEALED_V10_FENCE_VERSION}:
            from .v10_saver_guard import revoke_resume
            revoke_resume(ticket)
