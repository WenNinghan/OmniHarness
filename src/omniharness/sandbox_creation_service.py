"""Standalone creation/reclamation boundary (Linux/WSL, explicit provisioning).

Run with python -m omniharness.sandbox_creation_service --help. No model,
workload command, migration, automatic repair, or slot release occurs here.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from opensandbox.manager import SandboxManager
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .db import make_engine, make_session_factory
from .sandbox import SandboxConfig
from .sandbox_creation_api import _digest, create_candidate_app
from .sandbox_creation_authorization import authorize_creation
from .sandbox_creation_client import creation_service_binding
from .sandbox_creation_fence import DockerCreationFence
from .sandbox_creation_journal import CreationJournal
from .sandbox_creation_reclaimer import reclaim_created
from .sandbox_creation_witness import WitnessedCreationJournal
from .settings import Settings
from .workspace_client import read_service_key


class CreationServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OMNI_CREATION_", extra="ignore")
    journal_path: Path
    journal_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    witness_path: Path
    witness_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    engine_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    marker_image: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    socket_path: str = "/var/run/docker.sock"
    host: str = "127.0.0.1"
    port: int = Field(default=19083, ge=1, le=65535)


def service_identity(settings, storage):
    # The pinned identity commits to the *stores*, engine, and tool profile.
    # It is not a secret, and no API/controller key is included.
    return _digest({"version": 1, "instance_id": settings.instance_id,
        "journal_id": storage.journal_id, "witness_id": storage.witness_id,
        "engine_id": storage.engine_id, "marker_image": storage.marker_image,
        "controller": settings.sandbox_endpoint, "image": settings.sandbox_image,
        "snapshot_root": settings.sandbox_snapshot_root,
        "max_command_seconds": settings.sandbox_max_command_seconds})


def validate_storage(settings, storage, *, initializing=False):
    if os.name != "posix":
        raise ValueError("Creation service requires Linux/WSL storage permissions")
    paths = [storage.journal_path, storage.witness_path]
    for path in paths:
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("Creation storage requires absolute non-symlink paths")
        if not path.parent.is_dir():
            raise ValueError("Provision protected storage directories explicitly")
        for directory in (path.parent, *path.parent.parents):
            info = directory.stat()
            if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError("Creation storage ancestors must not be shared-writable")
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise ValueError("Creation storage directory must be private to the service user")
        if initializing:
            if path.exists():
                raise ValueError("Initialization cannot replace a creation store")
        else:
            info = path.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                raise ValueError("Creation storage file must be private and singly linked")
    left, right = (path.resolve() for path in paths)
    if (left.parent == right.parent or left.parent in right.parents or right.parent in left.parents
            or storage.journal_id == storage.witness_id):
        raise ValueError("Creation witness must be independently provisioned")
    protected_roots = [settings.data_dir.resolve(), settings.secret_dir.resolve(),
                       Path(settings.sandbox_snapshot_root).resolve()]
    if any(root == right or root in right.parents for root in protected_roots):
        raise ValueError("Witness must be outside application backup and sandbox storage")


def build_creation_service(settings, storage):
    if not settings.database_url.startswith("postgresql+psycopg://"):
        raise ValueError("Creation service authorization requires PostgreSQL row locks")
    validate_storage(settings, storage)
    expected = creation_service_binding(settings)
    if expected is None or expected["service_id"] != service_identity(settings, storage):
        raise ValueError("Configured creation service identity differs from its storage")
    config = SandboxConfig(endpoint=settings.sandbox_endpoint, api_key=read_service_key(settings.sandbox_key_file),
        image=settings.sandbox_image, snapshot_root=settings.sandbox_snapshot_root,
        instance_id=settings.instance_id, max_command_seconds=settings.sandbox_max_command_seconds)
    journal = WitnessedCreationJournal(CreationJournal(storage.journal_path, storage.journal_id),
                                      CreationJournal(storage.witness_path, storage.witness_id))
    journal.verify_consistency()
    fence = DockerCreationFence(socket_path=storage.socket_path, engine_id=storage.engine_id,
                                image=storage.marker_image)
    engine = make_engine(settings)
    factory = make_session_factory(engine)

    def authorize(request, claim):
        validate_storage(settings, storage)
        return authorize_creation(factory, config, request, claim, service_binding=expected)

    async def reclaim(record):
        validate_storage(settings, storage)
        manager = await SandboxManager.create(connection_config=config.connection())
        try:
            return await reclaim_created(record, manager, fence.verify_absence)
        finally:
            await manager.close()

    app = create_candidate_app(config, journal, read_service_key(settings.sandbox_creation_key_file),
        authorize=authorize, fence=fence, service_id=expected["service_id"], reclaimer=reclaim)
    # Keep the owner for service shutdown; this does not start a Worker.
    app.state.creation_engine = engine
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["identity", "initialize", "serve", "checkpoint",
                                          "verify-checkpoint", "verify-retained", "rehearse-checkpoint"])
    parser.add_argument("--directory", type=Path, help="New checkpoint or existing verified checkpoint directory")
    parser.add_argument("--target", type=Path, help="New offline rehearsal directory; never a live restore")
    args = parser.parse_args()
    if args.action in {"checkpoint", "verify-checkpoint", "verify-retained", "rehearse-checkpoint"}:
        if args.directory is None:
            parser.error("--directory is required")
        if args.action == "rehearse-checkpoint" and args.target is None:
            parser.error("--target is required for an offline rehearsal")
    elif args.directory is not None or args.target is not None:
        parser.error("Checkpoint paths are only valid for checkpoint actions")
    if args.target is not None and args.action != "rehearse-checkpoint":
        parser.error("--target is only valid for an offline rehearsal")
    from .sandbox_creation_checkpoint import (
        capture_checkpoint,
        rehearse_checkpoint,
        verify_checkpoint,
        verify_retained_history,
    )
    # Offline inspection never loads live settings/keys or opens live stores.
    if args.action in {"verify-checkpoint", "rehearse-checkpoint"}:
        report = (verify_checkpoint(args.directory) if args.action == "verify-checkpoint"
                  else rehearse_checkpoint(args.directory, args.target))
        print(json.dumps(report))
        return
    settings, storage = Settings(), CreationServiceSettings()
    if args.action == "identity":
        print(service_identity(settings, storage))
    elif args.action == "initialize":
        validate_storage(settings, storage, initializing=True)
        CreationJournal.initialize(storage.journal_path, storage.journal_id)
        CreationJournal.initialize(storage.witness_path, storage.witness_id)
        print("Creation stores initialized. No services or workloads were started.")
    elif args.action in {"checkpoint", "verify-retained"}:
        validate_storage(settings, storage)
        expected = creation_service_binding(settings)
        if expected is None or expected["service_id"] != service_identity(settings, storage):
            raise ValueError("Checkpoint configuration differs from its service identity")
        pair = WitnessedCreationJournal(CreationJournal(storage.journal_path, storage.journal_id),
                                        CreationJournal(storage.witness_path, storage.witness_id))
        action = capture_checkpoint if args.action == "checkpoint" else verify_retained_history
        print(json.dumps(action(pair, args.directory, service_id=expected["service_id"], engine_id=storage.engine_id)))
    else:
        import uvicorn
        app = build_creation_service(settings, storage)
        try:
            uvicorn.run(app, host=storage.host, port=storage.port, access_log=False)
        finally:
            app.state.creation_engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Creation service refused startup; check protected storage, identity and configuration.", file=sys.stderr)
        sys.exit(1)
