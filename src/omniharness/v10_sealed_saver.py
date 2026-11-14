"""Isolated v10 checkpoint sealing candidate; not connected to the Worker.

Checkpoint writes and an authenticated inventory are committed together. This
is not a proof that all effect paths are closed and does not authorize a model
or tool call. Unknown owner epochs and pre-existing unsealed runs fail closed.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import secrets
import sys
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from importlib.metadata import version
from uuid import UUID

from langgraph.checkpoint.base import WRITES_IDX_MAP, get_serializable_checkpoint_metadata
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.types import Send
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel
from sqlalchemy import select, text

from .checkpoint_fencing import SEALED_V10_FENCE_VERSION, BoundAsyncPostgresSaver, FenceTicket, _schema
from .m4_contract import M4_V10_PLUGIN_VERSIONS
from .m4_invalid_audit import _key

_VERSIONS = {"langgraph-checkpoint-postgres": "3.2.0", "langgraph-checkpoint": "4.3.1",
             "langgraph": "1.2.20", "psycopg": "3.3.8"}
_NODE = "v10-checkpoint-seal-v1"
_HEAD = "v10_checkpoint_seal_head"
_ORIGIN = "v10_checkpoint_seal_origin"
_MAX_ROWS = 10000
_MAX_BYTES = 32 * 1024 * 1024
_MAX_SEALS = 4096
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_MAX_INPUT_OBJECTS = 100000
SEALED_PLUGIN_VERSIONS = {**M4_V10_PLUGIN_VERSIONS, "builtin-checkpoint-seal": "2"}


class SealedSaverUnavailable(RuntimeError):
    """The sealed protocol or its current owner cannot be established."""


class SealedOwnerUnavailable(SealedSaverUnavailable):
    """The database no longer confirms the original Worker owner."""


def _encoded(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise SealedSaverUnavailable("Sealing input is not canonical JSON") from None


def _tag(key, label, value):
    return hmac.new(key, label.encode("ascii") + b"\0" + _encoded(value), hashlib.sha256).hexdigest()


def _origin_binding(key, origin):
    return _tag(key, "origin-identity", {name: origin[name] for name in
                ("version", "run_id", "workspace_id", "nonce")})


def _bounded_input(value):
    """Reject oversized/unknown object graphs before deepcopy or serialization."""
    pending, seen, size, objects = [value], set(), 0, 0
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        objects += 1
        size += sys.getsizeof(item)
        if isinstance(item, memoryview):
            size += item.nbytes
        if objects > _MAX_INPUT_OBJECTS or size > _MAX_INPUT_BYTES:
            raise SealedSaverUnavailable("Checkpoint input exceeds sealing bounds")
        if item is None or isinstance(item, (str, bytes, bytearray, memoryview, int, float, bool, date, datetime,
                                               Decimal, UUID)):
            continue
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (tuple, list, set, frozenset)):
            pending.extend(item)
        elif isinstance(item, BaseModel):
            pending.append(vars(item))
            extra = getattr(item, "__pydantic_extra__", None)
            if extra:
                pending.append(extra)
        elif isinstance(item, _DeltaSnapshot):
            pending.append(item.value)
        elif type(item) is Send:
            pending.extend((item.node, item.arg))
        elif is_dataclass(item) and not isinstance(item, type):
            pending.extend(getattr(item, field.name) for field in fields(item))
        else:
            raise SealedSaverUnavailable(f"Unsupported checkpoint input type: {type(item).__module__}.{type(item).__qualname__}")


def create_seal_origin(settings, run_id, workspace_id):
    """Host-only creation helper; must be frozen atomically at Run creation."""
    if not isinstance(run_id, str) or not run_id or not isinstance(workspace_id, str) or not workspace_id:
        raise SealedSaverUnavailable("Origin scope is unavailable")
    key = hmac.new(_key(settings, "stage-agent-v10"), b"omniharness/v10/checkpoint-seal/v1",
                   hashlib.sha256).digest()
    value = {"version": 1, "run_id": run_id, "workspace_id": workspace_id,
             "nonce": secrets.token_hex(32), "state": "unused"}
    return {**value, "mac": _tag(key, "origin", value)}


def classify_frozen_v10(settings, run_id, workspace_id, snapshot):
    """Classify only authenticated sealed or exact legacy v10 configurations."""
    if not isinstance(snapshot, dict):
        raise SealedSaverUnavailable("Frozen v10 configuration is unavailable")
    plugins = (snapshot.get("agent") or {}).get("plugin_versions")
    origin = snapshot.get(_ORIGIN)
    if plugins == M4_V10_PLUGIN_VERSIONS and origin is None:
        return "stage-agent-v10"
    if plugins != SEALED_PLUGIN_VERSIONS or not isinstance(origin, dict):
        raise SealedSaverUnavailable("Frozen v10 writer protocol is unknown")
    key = hmac.new(_key(settings, "stage-agent-v10"), b"omniharness/v10/checkpoint-seal/v1",
                   hashlib.sha256).digest()
    value = {k: origin.get(k) for k in ("version", "run_id", "workspace_id", "nonce", "state")}
    signature = origin.get("mac")
    if (value["version"] != 1 or value["run_id"] != run_id or value["workspace_id"] != workspace_id
            or value["state"] not in {"unused", "started"} or not isinstance(value["nonce"], str)
            or len(value["nonce"]) != 64 or not isinstance(signature, str)
            or not hmac.compare_digest(signature, _tag(key, "origin", value))):
        raise SealedSaverUnavailable("Frozen v10 seal origin differs")
    return SEALED_V10_FENCE_VERSION


def verify_pinned_seal_inventory(db, ticket, expected_root_id, settings):
    """Verify the current seal chain on the owner transaction; no dispatch grant.

    The fence is pinned before the Run is locked. This only authenticates the
    sealed checkpoint inventory, not the graph, actions, permissions or effects.
    """
    from .models import OperationRecord, Run
    from .v10_closure import pin_checkpoint_watermark

    if any(version(package) != required for package, required in _VERSIONS.items()):
        raise SealedSaverUnavailable("Unsupported checkpoint implementation version")
    watermarks = pin_checkpoint_watermark(db, ticket, expected_root_id)
    run = db.scalar(select(Run).where(Run.id == ticket.thread_id).with_for_update())
    if (not run or run.workflow_version != "stage-agent-v10" or run.status != "running"
            or not run.workspace_id or not isinstance(run.config_snapshot, dict)
            or (run.config_snapshot.get("agent") or {}).get("plugin_versions") != SEALED_PLUGIN_VERSIONS):
        raise SealedSaverUnavailable("Sealed Run scope is unavailable")
    schema = _schema(ticket.schema)
    business_schema = db.execute(text("SELECT current_schema()")).scalar_one()
    business_schema = _schema(business_schema)
    key = hmac.new(_key(settings, "stage-agent-v10"), b"omniharness/v10/checkpoint-seal/v1",
                   hashlib.sha256).digest()
    origin = run.config_snapshot.get(_ORIGIN)
    if not isinstance(origin, dict):
        raise SealedSaverUnavailable("Run creation seal origin is unavailable")
    value = {k: origin.get(k) for k in ("version", "run_id", "workspace_id", "nonce", "state")}
    signature = origin.get("mac")
    if (value["version"] != 1 or value["run_id"] != run.id or value["workspace_id"] != run.workspace_id
            or value["state"] != "started" or not isinstance(value["nonce"], str) or len(value["nonce"]) != 64
            or not isinstance(signature, str) or not hmac.compare_digest(signature, _tag(key, "origin", value))):
        raise SealedSaverUnavailable("Run creation seal origin differs")
    digest = hmac.new(key, b"v10-checkpoint-inventory-v1\0", hashlib.sha256)
    count, total, counts = 0, 0, {}
    queries = [
        ("checkpoints", "checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata",
         "checkpoint_ns, checkpoint_id", "octet_length(checkpoint::text) + octet_length(metadata::text)"),
        ("checkpoint_blobs", "checkpoint_ns, channel, version, type, blob", "checkpoint_ns, channel, version",
         "COALESCE(octet_length(blob), 0)"),
        ("checkpoint_writes", "checkpoint_ns, checkpoint_id, task_id, task_path, idx, channel, type, blob",
         "checkpoint_ns, checkpoint_id, task_id, idx", "COALESCE(octet_length(blob), 0)"),
    ]
    for table, columns, order, size_expr in queries:
        bounds = db.execute(text(f"SELECT count(*) AS count, COALESCE(sum({size_expr}),0) AS bytes "
                                 f"FROM {schema}.{table} WHERE thread_id=:thread"),
                            {"thread": ticket.thread_id}).mappings().one()
        if count + bounds["count"] > _MAX_ROWS or total + bounds["bytes"] > _MAX_BYTES:
            raise SealedSaverUnavailable("Checkpoint inventory exceeds sealing bounds")
        rows = db.execute(text(f"SELECT {columns} FROM {schema}.{table} WHERE thread_id=:thread ORDER BY {order}"),
                          {"thread": ticket.thread_id}).mappings()
        table_count = 0
        for row in rows:
            parts = []
            for item in row.values():
                if isinstance(item, (bytes, bytearray, memoryview)):
                    raw = bytes(item)
                    parts.append({"bytes_sha256": hashlib.sha256(raw).hexdigest(), "length": len(raw)})
                    total += len(raw)
                else:
                    parts.append(item)
            encoded = _encoded([table, parts])
            total += len(encoded)
            count += 1
            table_count += 1
            if count > _MAX_ROWS or total > _MAX_BYTES:
                raise SealedSaverUnavailable("Checkpoint inventory exceeds sealing bounds")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        counts[table] = table_count
    inventory = {"digest": digest.hexdigest(), "counts": counts}
    rows = list(db.scalars(select(OperationRecord).where(OperationRecord.run_id == run.id,
        OperationRecord.node_id == _NODE).limit(_MAX_SEALS + 1)))
    head = (run.outcome or {}).get(_HEAD)
    if not rows or len(rows) > _MAX_SEALS or not isinstance(head, dict):
        raise SealedSaverUnavailable("Checkpoint seal head or chain is missing")
    try:
        ordered = sorted(rows, key=lambda row: row.result["seal"]["sequence"])
        previous = None
        for index, row in enumerate(ordered, 1):
            seal, signature = row.result["seal"], row.result["mac"]
            if (row.status != "completed" or row.workflow_version != run.workflow_version
                    or seal["version"] != 2 or seal["origin_binding"] != _origin_binding(key, origin)
                    or seal["sequence"] != index or seal["previous"] != previous
                    or seal["run_id"] != run.id or seal["workspace_id"] != run.workspace_id
                    or seal["epoch"] != ticket.epoch or seal["checkpoint_schema"] != schema
                    or seal["business_schema"] != business_schema
                    or row.operation_id != _tag(key, "event", {"run_id": run.id, **seal["identity"]})
                    or row.parameters_hash != seal["input_digest"]
                    or not hmac.compare_digest(signature, _tag(key, "seal", seal))):
                raise ValueError
            previous = signature
        if head != {"sequence": len(ordered), "mac": previous,
                    "inventory": ordered[-1].result["seal"]["inventory"], "epoch": ticket.epoch} or head["inventory"] != inventory:
            raise ValueError
    except (KeyError, TypeError, ValueError, IndexError):
        raise SealedSaverUnavailable("Checkpoint seal chain or inventory differs") from None
    return watermarks, head


class V10SealedSaver(BoundAsyncPostgresSaver):
    """V10-only bounded saver whose writes and inventory seal share a transaction.

    It accepts only a separately frozen plugin mapping and cannot adopt an
    unsealed checkpoint. Cross-owner recovery and global closure are not
    implemented; the Worker selects it only for the separately frozen plugin.
    """

    def __init__(self, conn, ticket: FenceTicket, *, business_schema: str, settings):
        if not isinstance(ticket, FenceTicket) or ticket.workflow_version != SEALED_V10_FENCE_VERSION:
            raise SealedSaverUnavailable("V10 fence required")
        if any(version(package) != required for package, required in _VERSIONS.items()):
            raise SealedSaverUnavailable("Unsupported checkpoint implementation version")
        self.business_schema = _schema(business_schema)
        self.checkpoint_schema = _schema(ticket.schema)
        self._seal_key = hmac.new(_key(settings, "stage-agent-v10"),
                                  b"omniharness/v10/checkpoint-seal/v1", hashlib.sha256).digest()
        super().__init__(conn, ticket)
        self._phase_token = object()
        self._register_phase_writer()
        self._writes_closed = False
        self._active_writes = 0
        self._write_idle = asyncio.Event()
        self._write_idle.set()

    def _register_phase_writer(self):
        from .v10_saver_guard import register_execution_writer
        register_execution_writer(self.ticket, self._phase_token, self)

    async def close_writes(self):
        """Irreversibly close writes after any current saver transaction ends.

        This is only a saver-local barrier, not proof that the graph, scheduler,
        owner, or external side effects are drained. A pending call that is
        still preprocessing must recheck the flag once it acquires this lock.
        """
        async with self.lock:
            from .v10_saver_guard import close_execution_writer
            close_execution_writer(self.ticket, self._phase_token)
            self._writes_closed = True
        await asyncio.wait_for(self._write_idle.wait(), timeout=10)

    def assert_write_drain(self, ticket):
        """Check this exact saver generation is closed and has no pending writes.

        This is a necessary saver condition only, never scheduler, graph or
        owner authorization. The host must call on the saver event loop.
        """
        from .v10_saver_guard import assert_closed_execution_writer
        asyncio.get_running_loop()
        if (ticket != self.ticket or not self._writes_closed or self._active_writes != 0
                or not self._write_idle.is_set() or self.lock.locked()
                or self.conn.closed or self.conn.broken):
            raise SealedSaverUnavailable("Saver write drain is unavailable")
        assert_closed_execution_writer(ticket, self._phase_token)

    async def retire_after_context(self):
        """Drain this generation before allowing same-owner wait/resume."""
        from .v10_saver_guard import (
            begin_retire_execution_writer,
            close_execution_writer,
            finish_retire_execution_writer,
        )
        waiting = False
        async with self.lock:
            if self._writes_closed:
                return
            try:
                async with self.conn.transaction():
                    async with self.conn.cursor() as cur:
                        run = await self._owner_and_run(cur, retirement=True)
                        waiting = run["status"] in {"awaiting_user", "awaiting_approval", "paused", "queued"}
                begin_retire_execution_writer(self.ticket, self._phase_token)
                self._writes_closed = True
            except BaseException:
                close_execution_writer(self.ticket, self._phase_token)
                self._writes_closed = True
                raise
        try:
            await asyncio.wait_for(self._write_idle.wait(), timeout=10)
            finish_retire_execution_writer(self.ticket, self._phase_token, waiting=waiting)
        except BaseException:
            close_execution_writer(self.ticket, self._phase_token)
            raise

    async def _owner_and_run(self, cur, *, retirement=False):
        # Lock order: fence first, then the Run. The checkpoint trigger takes
        # FOR SHARE on this same row; FOR UPDATE also serializes all writers.
        query = sql.SQL("""
            SELECT f.epoch, f.workflow_version, f.owner_pid, f.owner_started,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity a
                     JOIN pg_catalog.pg_locks l ON l.pid=a.pid
                     WHERE a.pid=f.owner_pid AND a.backend_start=f.owner_started
                       AND l.locktype='advisory' AND l.mode='ExclusiveLock' AND l.granted
                       AND l.objsubid=1 AND l.classid::bigint=(f.lock_key >> 32)
                       AND l.objid::bigint=(f.lock_key & 4294967295)
                       AND l.database=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())) AS live
            FROM {}.fences f WHERE f.thread_id=%s FOR UPDATE
        """).format(sql.Identifier(self.checkpoint_schema))
        await cur.execute(query, (self.ticket.thread_id,))
        fence = await cur.fetchone()
        if (not fence or fence["epoch"] != self.ticket.epoch or fence["workflow_version"] != self.ticket.workflow_version
                or fence["owner_pid"] != self.ticket.owner_pid or fence["owner_started"] != self.ticket.owner_started
                or fence["live"] is not True):
            raise SealedOwnerUnavailable("Checkpoint owner fence is unavailable")
        await cur.execute(sql.SQL("SELECT id, workspace_id, workflow_version, status, config_snapshot, outcome "
                                  "FROM {}.runs WHERE id=%s FOR UPDATE").format(sql.Identifier(self.business_schema)),
                          (self.ticket.thread_id,))
        run = await cur.fetchone()
        agent = (run["config_snapshot"] or {}).get("agent", {}) if run else {}
        if (not run or run["workflow_version"] != "stage-agent-v10" or not run["workspace_id"]
                or run["status"] not in ({"running", "pause_requested", "cancel_requested", "awaiting_user",
                                           "awaiting_approval", "paused", "queued", "completed", "failed",
                                           "cancelled", "pending_review"} if retirement else
                                          {"running", "pause_requested", "cancel_requested"})
                or agent.get("plugin_versions") != SEALED_PLUGIN_VERSIONS):
            raise SealedSaverUnavailable("Sealed saver is not frozen for this Run")
        origin = (run["config_snapshot"] or {}).get(_ORIGIN)
        if not isinstance(origin, dict):
            raise SealedSaverUnavailable("Run creation seal origin is unavailable")
        value = {k: origin.get(k) for k in ("version", "run_id", "workspace_id", "nonce", "state")}
        signature = origin.get("mac")
        if (value["version"] != 1 or value["run_id"] != run["id"] or value["workspace_id"] != run["workspace_id"]
                or value["state"] not in {"unused", "started"}
                or not isinstance(value["nonce"], str) or len(value["nonce"]) != 64
                or not isinstance(signature, str)
                or not hmac.compare_digest(signature, _tag(self._seal_key, "origin", value))):
            raise SealedSaverUnavailable("Run creation seal origin differs")
        return run

    async def _pristine(self, cur, run):
        """Only a host-issued unused origin and an untouched Run can start."""
        origin = run["config_snapshot"][_ORIGIN]
        if origin["state"] != "unused":
            raise SealedSaverUnavailable("Started Run cannot be treated as pristine")
        schema = sql.Identifier(self.business_schema)
        for table in ("agent_runs", "tool_operations", "operation_records"):
            await cur.execute(sql.SQL("SELECT 1 FROM {}.{} WHERE run_id=%s LIMIT 1").format(
                schema, sql.Identifier(table)), (run["id"],))
            if await cur.fetchone():
                raise SealedSaverUnavailable("Run already contains execution records")
        await cur.execute(sql.SQL("SELECT model_calls, tool_calls, revisions FROM {}.run_budgets WHERE run_id=%s")
                          .format(schema), (run["id"],))
        budget = await cur.fetchone()
        if not budget or any(budget[key] != 0 for key in ("model_calls", "tool_calls", "revisions")):
            raise SealedSaverUnavailable("Run budget is not pristine")

    async def _inventory(self, cur):
        digest = hmac.new(self._seal_key, b"v10-checkpoint-inventory-v1\0", hashlib.sha256)
        count, total = 0, 0
        queries = [
            ("checkpoints", "checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata",
             "checkpoint_ns, checkpoint_id", "octet_length(checkpoint::text) + octet_length(metadata::text)"),
            ("checkpoint_blobs", "checkpoint_ns, channel, version, type, blob", "checkpoint_ns, channel, version",
             "COALESCE(octet_length(blob), 0)"),
            ("checkpoint_writes", "checkpoint_ns, checkpoint_id, task_id, task_path, idx, channel, type, blob",
             "checkpoint_ns, checkpoint_id, task_id, idx", "COALESCE(octet_length(blob), 0)"),
        ]
        counts = {}
        for table, columns, order, size_expr in queries:
            await cur.execute(sql.SQL("SELECT count(*) AS count, COALESCE(sum(" + size_expr + "),0) AS bytes "
                "FROM {}.{} WHERE thread_id=%s").format(sql.Identifier(self.checkpoint_schema), sql.Identifier(table)),
                (self.ticket.thread_id,))
            bounds = await cur.fetchone()
            if count + bounds["count"] > _MAX_ROWS or total + bounds["bytes"] > _MAX_BYTES:
                raise SealedSaverUnavailable("Checkpoint inventory exceeds sealing bounds")
            # Table/column/order are constant trusted literals, never user input.
            query = sql.SQL("SELECT " + columns + " FROM {}.{} WHERE thread_id=%s ORDER BY " + order).format(
                sql.Identifier(self.checkpoint_schema), sql.Identifier(table))
            await cur.execute(query, (self.ticket.thread_id,))
            count_for_table = 0
            while rows := await cur.fetchmany(128):
                for row in rows:
                    parts = []
                    for value in row.values():
                        if isinstance(value, (bytes, bytearray, memoryview)):
                            raw = bytes(value)
                            parts.append({"bytes_sha256": hashlib.sha256(raw).hexdigest(), "length": len(raw)})
                            total += len(raw)
                        else:
                            parts.append(value)
                    encoded = _encoded([table, parts])
                    total += len(encoded)
                    count += 1
                    count_for_table += 1
                    if count > _MAX_ROWS or total > _MAX_BYTES:
                        raise SealedSaverUnavailable("Checkpoint inventory exceeds sealing bounds")
                    digest.update(len(encoded).to_bytes(8, "big"))
                    digest.update(encoded)
            counts[table] = count_for_table
        return {"digest": digest.hexdigest(), "counts": counts}

    async def _chain(self, cur, run, inventory):
        await cur.execute(sql.SQL("SELECT operation_id, parameters_hash, result, status, workflow_version "
                                  "FROM {}.operation_records WHERE run_id=%s AND node_id=%s LIMIT %s").format(
                                      sql.Identifier(self.business_schema)),
                          (self.ticket.thread_id, _NODE, _MAX_SEALS + 1))
        rows = await cur.fetchall()
        if len(rows) > _MAX_SEALS:
            raise SealedSaverUnavailable("Checkpoint seal chain exceeds bounds")
        head = (run["outcome"] or {}).get(_HEAD)
        if not rows and head is None:
            if any(inventory["counts"].values()):
                raise SealedSaverUnavailable("Existing checkpoint has no authenticated seal")
            await self._pristine(cur, run)
            return None, []
        if run["config_snapshot"][_ORIGIN]["state"] != "started":
            raise SealedSaverUnavailable("Checkpoint origin state differs")
        if not rows or not isinstance(head, dict):
            raise SealedSaverUnavailable("Checkpoint seal head or chain is missing")
        try:
            ordered = sorted(rows, key=lambda row: row["result"]["seal"]["sequence"])
            previous = None
            for index, row in enumerate(ordered, 1):
                result = row["result"]
                seal, signature = result["seal"], result["mac"]
                if (row["status"] != "completed" or row["workflow_version"] != "stage-agent-v10"
                        or seal["version"] != 2 or seal["origin_binding"] != _origin_binding(
                            self._seal_key, run["config_snapshot"][_ORIGIN])
                        or seal["sequence"] != index or seal["previous"] != previous
                        or seal["run_id"] != run["id"] or seal["workspace_id"] != run["workspace_id"]
                        or seal["epoch"] != self.ticket.epoch or seal["checkpoint_schema"] != self.checkpoint_schema
                        or seal["business_schema"] != self.business_schema
                        or row["operation_id"] != _tag(self._seal_key, "event", {"run_id": run["id"],
                                                                               **seal["identity"]})
                        or row["parameters_hash"] != seal["input_digest"]
                        or not hmac.compare_digest(signature, _tag(self._seal_key, "seal", seal))):
                    raise ValueError
                previous = signature
            if (head != {"sequence": len(ordered), "mac": previous,
                         "inventory": ordered[-1]["result"]["seal"]["inventory"], "epoch": self.ticket.epoch}
                    or head["inventory"] != inventory):
                raise ValueError
        except (KeyError, TypeError, ValueError, IndexError):
            raise SealedSaverUnavailable("Checkpoint seal chain or inventory differs") from None
        return head, ordered

    async def _write(self, config, **kwargs):
        if self._writes_closed:
            raise SealedSaverUnavailable("Checkpoint writes are closed")
        # Registration happens before any await, covering serialization work
        # that has not yet acquired the saver lock.
        self._active_writes += 1
        self._write_idle.clear()
        try:
            return await self._write_inner(config, **kwargs)
        finally:
            self._active_writes -= 1
            if self._active_writes == 0:
                self._write_idle.set()

    def _assert_phase_writer(self, namespace):
        from .v10_saver_guard import assert_execution_writer
        assert_execution_writer(self.ticket, self._phase_token)

    async def _check_phase_transaction(self, cur, run, namespace):
        """Execution writes have no separate correction-phase ledger."""
        return None

    async def _write_inner(self, config, *, kind, checkpoint=None, metadata=None, new_versions=None, writes=None,
                           task_id=None, task_path=""):
        if self._writes_closed:
            raise SealedSaverUnavailable("Checkpoint writes are closed")
        self._bound(config)
        conf = config.get("configurable", {})
        namespace, checkpoint_id = conf.get("checkpoint_ns"), conf.get("checkpoint_id")
        if (not isinstance(namespace, str) or len(namespace) > 2048 or "\x00" in namespace
                or (checkpoint_id is not None and (not isinstance(checkpoint_id, str)
                    or len(checkpoint_id) > 200 or "\x00" in checkpoint_id))
                or (task_id is not None and (not isinstance(task_id, str) or len(task_id) > 512 or "\x00" in task_id))
                or not isinstance(task_path, str) or len(task_path) > 2048 or "\x00" in task_path):
            raise SealedSaverUnavailable("Invalid checkpoint identity")
        self._assert_phase_writer(namespace)
        # Freeze before the first await; mutable caller data cannot change while
        # serialization or the connection lock is pending.
        _bounded_input({"checkpoint": checkpoint, "metadata": metadata, "new_versions": new_versions,
                        "writes": writes, "task_id": task_id, "task_path": task_path})
        for source in (config.get("metadata"), config.get("configurable")):
            if source is not None and (not isinstance(source, Mapping) or len(source) > 2048):
                raise SealedSaverUnavailable("Checkpoint config exceeds sealing bounds")
            if source:
                _bounded_input({key: value for key, value in source.items()
                                if isinstance(key, str) and not key.startswith("__")
                                and isinstance(value, (str, int, float, bool, type(None)))})
        serial_metadata = get_serializable_checkpoint_metadata(config, metadata) if kind == "checkpoint" else None
        _bounded_input(serial_metadata)
        frozen = copy.deepcopy({"checkpoint": checkpoint, "metadata": serial_metadata,
                                "new_versions": new_versions, "writes": writes, "task_id": task_id,
                                "task_path": task_path})
        checkpoint, metadata, new_versions, writes = (frozen["checkpoint"], frozen["metadata"],
                                                       frozen["new_versions"], frozen["writes"])
        if kind == "checkpoint":
            if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("id"), str):
                raise SealedSaverUnavailable("Invalid checkpoint")
            target_id = checkpoint["id"]
            copy_checkpoint = checkpoint.copy()
            copy_checkpoint["channel_values"] = copy_checkpoint["channel_values"].copy()
            blob_values = {}
            for key, value in checkpoint["channel_values"].items():
                if isinstance(value, _DeltaSnapshot):
                    blob_values[key] = copy_checkpoint["channel_values"].pop(key)
                    copy_checkpoint["channel_values"][key] = True
                elif value is None or isinstance(value, (str, int, float, bool)):
                    continue
                else:
                    blob_values[key] = copy_checkpoint["channel_values"].pop(key)
            blob_versions = {k: v for k, v in new_versions.items() if k in blob_values}
            dumped = await asyncio.to_thread(self._dump_blobs, self.ticket.thread_id, namespace, blob_values,
                                              blob_versions) if blob_versions else []
            serial_metadata = metadata
            payload = {"checkpoint": copy_checkpoint, "metadata": serial_metadata,
                       "blobs": [[*item[:-1], hashlib.sha256(item[-1] or b"").hexdigest()] for item in dumped]}
            identity = {"kind": kind, "namespace_mac": _tag(self._seal_key, "namespace", namespace),
                        "checkpoint_id": target_id}
        else:
            if not checkpoint_id or not isinstance(task_id, str) or not task_id or not isinstance(writes, list):
                raise SealedSaverUnavailable("Invalid checkpoint writes")
            target_id = checkpoint_id
            dumped = await asyncio.to_thread(self._dump_writes, self.ticket.thread_id, namespace,
                                              checkpoint_id, task_id, task_path, writes)
            payload = {"writes": [[*item[:-1], hashlib.sha256(item[-1] or b"").hexdigest()] for item in dumped]}
            identity = {"kind": kind, "namespace_mac": _tag(self._seal_key, "namespace", namespace),
                        "checkpoint_id": target_id, "task_mac": _tag(self._seal_key, "task", task_id),
                        "indices": [item[5] for item in dumped]}
        input_digest = _tag(self._seal_key, "input", {"parent": checkpoint_id, "payload": payload})
        event_id = _tag(self._seal_key, "event", {"run_id": self.ticket.thread_id, **identity})
        async with self.lock:
            # _write can await serialization before this lock. The barrier may
            # have closed writes while that preprocessing was still in flight.
            if self._writes_closed:
                raise SealedSaverUnavailable("Checkpoint writes are closed")
            self._assert_phase_writer(namespace)
            async with self.conn.transaction():
                async with self.conn.cursor() as cur:
                    run = await self._owner_and_run(cur)
                    await self._check_phase_transaction(cur, run, namespace)
                    before = await self._inventory(cur)
                    head, chain = await self._chain(cur, run, before)
                    existing = next((row for row in chain if row["operation_id"] == event_id), None)
                    if existing is not None:
                        if (existing["parameters_hash"] != input_digest or chain[-1]["operation_id"] != event_id):
                            raise SealedSaverUnavailable("Checkpoint invocation conflicts with existing seal")
                        return ({"configurable": {"thread_id": self.ticket.thread_id, "checkpoint_ns": namespace,
                                                  "checkpoint_id": target_id}} if kind == "checkpoint" else None)
                    if len(chain) >= _MAX_SEALS:
                        raise SealedSaverUnavailable("Checkpoint seal chain exceeds bounds")
                    if checkpoint_id:
                        await cur.execute(sql.SQL("SELECT 1 FROM {}.checkpoints WHERE thread_id=%s "
                            "AND checkpoint_ns=%s AND checkpoint_id=%s").format(sql.Identifier(self.checkpoint_schema)),
                            (self.ticket.thread_id, namespace, checkpoint_id))
                        if await cur.fetchone() is None:
                            raise SealedSaverUnavailable("Checkpoint parent is unavailable")
                    if kind == "checkpoint":
                        if dumped:
                            await cur.executemany(self.UPSERT_CHECKPOINT_BLOBS_SQL, dumped)
                        await cur.execute(self.UPSERT_CHECKPOINTS_SQL, (self.ticket.thread_id, namespace, target_id,
                            checkpoint_id, Jsonb(copy_checkpoint), Jsonb(serial_metadata)))
                    else:
                        query = (self.UPSERT_CHECKPOINT_WRITES_SQL if all(item[0] in WRITES_IDX_MAP for item in writes)
                                 else self.INSERT_CHECKPOINT_WRITES_SQL)
                        await cur.executemany(query, dumped)
                    after = await self._inventory(cur)
                    seal = {"version": 2, "sequence": (head["sequence"] if head else 0) + 1,
                            "previous": head["mac"] if head else None, "run_id": run["id"],
                            "workspace_id": run["workspace_id"], "epoch": self.ticket.epoch,
                            "origin_binding": _origin_binding(self._seal_key, run["config_snapshot"][_ORIGIN]),
                            "checkpoint_schema": self.checkpoint_schema, "business_schema": self.business_schema,
                            "identity": identity, "input_digest": input_digest, "inventory": after}
                    signature = _tag(self._seal_key, "seal", seal)
                    insert = sql.SQL("INSERT INTO {}.operation_records "
                        "(operation_id,run_id,node_id,parameters_hash,status,attempts,workflow_version,result,created_at,finished_at) "
                        "VALUES (%s,%s,%s,%s,'completed',1,'stage-agent-v10',%s,now(),now())").format(
                            sql.Identifier(self.business_schema))
                    await cur.execute(insert, (event_id, run["id"], _NODE, input_digest, Jsonb({"seal": seal, "mac": signature})))
                    outcome = dict(run["outcome"] or {})
                    outcome[_HEAD] = {"sequence": seal["sequence"], "mac": signature,
                                      "inventory": after, "epoch": self.ticket.epoch}
                    snapshot = dict(run["config_snapshot"])
                    if head is None:
                        origin = dict(snapshot[_ORIGIN])
                        origin["state"] = "started"
                        origin["mac"] = _tag(self._seal_key, "origin", {k: origin[k] for k in
                            ("version", "run_id", "workspace_id", "nonce", "state")})
                        snapshot[_ORIGIN] = origin
                    await cur.execute(sql.SQL("UPDATE {}.runs SET outcome=%s, config_snapshot=%s WHERE id=%s").format(
                        sql.Identifier(self.business_schema)), (Jsonb(outcome), Jsonb(snapshot), run["id"]))
                    # Recheck before transaction exit; any later owner change
                    # still forces a new epoch and separate reconciliation.
                    run = await self._owner_and_run(cur)
                    await self._check_phase_transaction(cur, run, namespace)
        if kind == "checkpoint":
            return {"configurable": {"thread_id": self.ticket.thread_id, "checkpoint_ns": namespace,
                                     "checkpoint_id": target_id}}
        return None

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self._write(config, kind="checkpoint", checkpoint=checkpoint, metadata=metadata,
                                 new_versions=new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await self._write(config, kind="writes", writes=list(writes), task_id=task_id, task_path=task_path)
