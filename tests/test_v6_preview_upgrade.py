"""Isolated preview grant upgrade; these tests never contact Docker or a model."""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict

import pytest
from conftest import create_chat

from omniharness.models import OperationRecord, Run, ToolOperation, Workspace, WorkspaceLease, WorkspacePolicy
from omniharness.services import parameters_digest
from omniharness.workspace_io import WorkspaceGrant
from scripts import v6_preview_deploy as deploy

IDENTITY = {
    "path": r"G:\OmniHarness-V6-Preview-Fixture",
    "volume_serial": 14,
    "file_id": 91,
    "directory": True,
    "links": 1,
    "filesystem": "NTFS",
}
PATH = r"G:\OmniHarness-V6-Preview-Fixture"


@pytest.fixture
def fixture_workspace(environment):
    _, _, factory, _ = environment
    identity = deepcopy(IDENTITY)
    grant = asdict(
        WorkspaceGrant(
            workspace_id="fixture",
            instance_id="omni-v6-preview",
            root=PATH,
            read_allow=(".",),
            write_allow=(),
            excludes=("node_modules",),
            expected_identity=identity,
        )
    )
    # JSON serialization in the actual database converts tuples to arrays.
    grant = json.loads(json.dumps(grant))
    with factory.begin() as db:
        db.add(
            Workspace(
                id="fixture",
                name="Preview fixture",
                host_path=PATH,
                enabled=True,
                physical_key=parameters_digest(
                    {k: identity.get(k) for k in ("volume_serial", "device", "file_id")}
                ),
                policy_version=1,
                probe={"identity": identity},
            )
        )
        db.add(
            WorkspacePolicy(
                workspace_id="fixture",
                version=1,
                grant=grant,
                tool_policy={"network": "deny", "snapshot_writeback": False},
            )
        )
    return factory


def test_policy_upgrade_appends_grant_without_changing_history(fixture_workspace):
    factory = fixture_workspace
    with factory.begin() as db:
        before = deepcopy(db.get(WorkspacePolicy, ("fixture", 1)).grant)
        result = deploy.fixture_policy_upgrade(db, IDENTITY, "omni-v6-preview")
        assert result == {"workspace_id": "fixture", "old_version": 1, "new_version": 2}
    with factory() as db:
        old = db.get(WorkspacePolicy, ("fixture", 1))
        new = db.get(WorkspacePolicy, ("fixture", 2))
        assert old.grant == before
        assert new.grant == {**before, "version": 2, "write_allow": ["."]}
        assert new.tool_policy == old.tool_policy
        assert db.get(Workspace, "fixture").policy_version == 2


def test_existing_v2_is_verified_and_reused_without_writes(fixture_workspace):
    with fixture_workspace.begin() as db:
        deploy.fixture_policy_upgrade(db, IDENTITY, "omni-v6-preview")
    with fixture_workspace.begin() as db:
        before = deepcopy(db.get(WorkspacePolicy, ("fixture", 2)).grant)
        result = deploy.fixture_policy_upgrade(db, IDENTITY, "omni-v6-preview")
        assert result == {"workspace_id": "fixture", "old_version": 1, "new_version": 2}
        assert not db.new and not db.dirty and not db.deleted
        assert db.get(WorkspacePolicy, ("fixture", 2)).grant == before
    with fixture_workspace.begin() as db:
        policy = db.get(WorkspacePolicy, ("fixture", 2))
        policy.grant = {**policy.grant, "write_allow": ["other"]}
    with fixture_workspace.begin() as db:
        with pytest.raises(ValueError, match="v2 grant changed"):
            deploy.fixture_policy_upgrade(db, IDENTITY, "omni-v6-preview")



@pytest.mark.parametrize("alteration", ["instance", "identity", "path", "grant", "duplicate"])
def test_policy_fails_closed_on_identity_or_grant_change(fixture_workspace, alteration):
    factory = fixture_workspace
    identity = deepcopy(IDENTITY)
    with factory.begin() as db:
        row = db.get(Workspace, "fixture")
        policy = db.get(WorkspacePolicy, ("fixture", 1))
        if alteration == "identity":
            identity["file_id"] += 1
        if alteration == "path":
            row.host_path = r"G:\another"
        if alteration == "grant":
            policy.grant = {**policy.grant, "write_allow": ["."]}
        if alteration == "duplicate":
            db.add(WorkspacePolicy(workspace_id="fixture", version=2, grant=policy.grant, tool_policy={}))
        db.flush()
        with pytest.raises(ValueError):
            deploy.fixture_policy_upgrade(
                db, identity, "other" if alteration == "instance" else "omni-v6-preview"
            )
        assert row.policy_version == 1


def test_active_and_unsettled_operations_block(fixture_workspace, environment):
    _, _, factory, client = environment
    _, _, pending = create_chat(client)
    run_id = pending["run_id"]
    with factory.begin() as db:
        with pytest.raises(ValueError, match="active or queued"):
            deploy.fixture_policy_upgrade(db, IDENTITY, "omni-v6-preview")
        run = db.get(Run, run_id)
        run.status = "cancelled"
        db.add(WorkspaceLease(workspace_id="fixture", run_id=run_id))
    with factory.begin() as db:
        with pytest.raises(ValueError, match="lease"):
            deploy.fixture_policy_upgrade(db, IDENTITY, "omni-v6-preview")
        db.delete(db.get(WorkspaceLease, "fixture"))
        db.add(
            ToolOperation(
                id="tool",
                run_id=run_id,
                task_id="task",
                tool_call_id="call",
                tool_name="workspace_patch",
                tool_version="v1",
                parameters={},
                binding_fingerprint="a" * 64,
                status="uncertain",
                slot_claimed=False,
            )
        )
    with factory.begin() as db:
        with pytest.raises(ValueError, match="unsettled tool"):
            deploy.fixture_policy_upgrade(db, IDENTITY, "omni-v6-preview")
        db.delete(db.get(ToolOperation, "tool"))
        db.add(
            OperationRecord(
                operation_id="operation",
                run_id=run_id,
                node_id="agent:execute:tool_calls",
                parameters_hash="b" * 64,
                status="uncertain",
            )
        )
    with factory.begin() as db:
        with pytest.raises(ValueError, match="tool ledger"):
            deploy.fixture_policy_upgrade(db, IDENTITY, "omni-v6-preview")
        assert db.get(WorkspacePolicy, ("fixture", 2)) is None


def test_transaction_rolls_back_both_policy_and_pointer(fixture_workspace):
    factory = fixture_workspace
    with pytest.raises(RuntimeError):
        with factory.begin() as db:
            deploy.fixture_policy_upgrade(db, IDENTITY, "omni-v6-preview")
            raise RuntimeError("simulated transaction failure")
    with factory() as db:
        assert db.get(Workspace, "fixture").policy_version == 1
        assert db.get(WorkspacePolicy, ("fixture", 2)) is None


def test_audit_does_not_write_policy(fixture_workspace):
    with fixture_workspace.begin() as db:
        deploy.fixture_policy_upgrade(db, IDENTITY, "omni-v6-preview", apply=False)
    with fixture_workspace() as db:
        assert db.get(Workspace, "fixture").policy_version == 1
        assert db.get(WorkspacePolicy, ("fixture", 2)) is None


def test_offline_guard_fails_closed(monkeypatch):
    monkeypatch.setattr(deploy, "_assert_project_root", lambda: None)
    monkeypatch.setattr(
        deploy,
        "compose",
        lambda *args, **kwargs: b"\n".join(
            [
                json.dumps({"Service": "api", "State": "running"}).encode(),
                json.dumps({"Service": "worker", "State": "exited"}).encode(),
                json.dumps({"Service": "postgres", "State": "running"}).encode(),
            ]
        ),
    )
    monkeypatch.setattr(deploy, "_broker_offline", lambda: pytest.fail("should reject API first"))
    with pytest.raises(ValueError, match="stopped"):
        deploy._preview_offline()


def test_quiesce_does_not_stop_worker_if_a_run_appears(monkeypatch, tmp_path):
    calls = []
    checks = iter([None, None, ValueError("new queued task")])

    def audit(**kwargs):
        outcome = next(checks)
        if outcome:
            raise outcome

    monkeypatch.setattr(deploy, "audit_preview", audit)
    monkeypatch.setattr(deploy, "_backup_preview_data", lambda: tmp_path)
    monkeypatch.setattr(deploy, "compose", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(deploy, "stop_preview_broker", lambda: pytest.fail("must not stop broker"))
    with pytest.raises(ValueError, match="queued"):
        deploy.quiesce_preview()
    assert calls == [("stop", "--timeout", "30", "api"), ("start", "api")]


@pytest.mark.parametrize("broker_available", [True, False])
def test_quiesce_failure_only_restarts_with_confirmed_broker(monkeypatch, tmp_path, capsys, broker_available):
    calls = []
    monkeypatch.setattr(deploy, "audit_preview", lambda **kwargs: None)
    monkeypatch.setattr(deploy, "_backup_preview_data", lambda: tmp_path)

    def compose(*args, **kwargs):
        calls.append(args)
        if args == ("stop", "--timeout", "30", "worker"):
            raise RuntimeError("worker stop failed")

    def broker():
        if not broker_available:
            raise RuntimeError("broker state unknown")

    monkeypatch.setattr(deploy, "compose", compose)
    monkeypatch.setattr(deploy, "_broker_online", broker)
    with pytest.raises(RuntimeError, match="worker stop failed"):
        deploy.quiesce_preview()
    if broker_available:
        assert ("start", "api", "worker") in calls
    else:
        assert ("start", "api", "worker") not in calls
        assert "resume-quiesce-broker" in capsys.readouterr().out


def test_broker_stop_is_exactly_scoped(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy, "_assert_project_root", lambda: None)
    monkeypatch.setattr(deploy, "_broker_offline", lambda: calls.append("offline"))
    monkeypatch.setattr(
        deploy,
        "compose",
        lambda *args, **kwargs: b"\n".join(
            [
                json.dumps({"Service": "api", "State": "exited"}).encode(),
                json.dumps({"Service": "worker", "State": "exited"}).encode(),
            ]
        ),
    )
    monkeypatch.setattr(deploy, "wsl", lambda *args, **kwargs: calls.append(args) or b"")
    deploy.stop_preview_broker()
    code = calls[0][2]
    assert "v6-preview.broker.json" in code and "'19102'" in code
    assert "pidfd_send_signal" in code and "19098" not in code and "19092" not in code
    assert calls[1] == "offline"


def test_profile_updates_only_two_pinned_images(monkeypatch):
    monkeypatch.setattr(deploy, "_image_id", lambda image: None)
    old = b"POSTGRES_PASSWORD=private\nOMNI_V6_IMAGE=old\nOMNI_V6_TOOL_IMAGE=oldtool\nOTHER=preserved\n"
    new = deploy._replace_profile(old, "sha256:" + "a" * 64, "sha256:" + "b" * 64)
    assert b"POSTGRES_PASSWORD=private\n" in new
    assert b"OTHER=preserved\n" in new
    assert b"OMNI_V6_IMAGE=sha256:" + b"a" * 64 in new
    with pytest.raises(ValueError, match="duplicate"):
        deploy._replace_profile(old + b"OMNI_V6_IMAGE=duplicate\n", "a", "b")


def test_profile_enables_interactive_and_rejects_duplicate(monkeypatch):
    monkeypatch.setattr(deploy, "_image_id", lambda _: None)
    raw = b"OMNI_V6_IMAGE=old\nOMNI_V6_TOOL_IMAGE=tool\n"
    result = deploy._replace_profile(raw, "app", "tool")
    assert deploy._interactive_profile(result)
    assert result.count(b"OMNI_INTERACTIVE_PREVIEW=true\n") == 1
    with pytest.raises(ValueError, match="duplicate"):
        deploy._replace_profile(
            raw + b"OMNI_INTERACTIVE_PREVIEW=false\nOMNI_INTERACTIVE_PREVIEW=true\n", "app", "tool"
        )


def test_effective_compose_profile_rejects_env_override(monkeypatch):
    profile = (
        b"OMNI_V6_IMAGE=expected\nOMNI_V6_TOOL_IMAGE=expected-tool\nOMNI_INTERACTIVE_PREVIEW=true\n"
        b"OMNI_V6_POSTGRES_PASSWORD=private\nOMNI_V6_DATA_DIR=/data-source\n"
        b"OMNI_V6_SECRET_DIR=/secret-source\nOMNI_V6_CONTROLLER_CONFIG=/controller-source\n"
    )

    def volume(kind, source, target, read_only=False):
        return {"type": kind, "source": source, "target": target, "read_only": read_only}

    def port(published, target):
        return {"host_ip": "127.0.0.1", "published": str(published), "target": target, "protocol": "tcp"}

    config = {
        "name": deploy.PROJECT,
        "networks": {"default": {"name": deploy.PROJECT + "_default"}},
        "volumes": {"preview-postgres": {"name": deploy.PROJECT + "_preview-postgres"}},
        "services": {
            name: {
                "image": "expected",
                "command": ["omniharness", {"api": "serve", "worker": "worker", "init": "bootstrap"}[name]],
                "user": "1000:1000",
                "init": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "extra_hosts": ["host.docker.internal=host-gateway"],
                "networks": {"default": None},
                "environment": {
                    "OMNI_INSTANCE_ID": deploy.PROJECT,
                    "OMNI_V6_PREVIEW": "true",
                    "OMNI_SANDBOX_IMAGE": "expected-tool",
                    "OMNI_DATABASE_URL": "postgresql+psycopg://omniharness:private@postgres:5432/omniharness",
                    "OMNI_DATA_DIR": "/data",
                    "OMNI_SECRET_DIR": "/secrets",
                    "OMNI_CHECKPOINT_SCHEMA": "omniharness_v6_checkpoints",
                    "OMNI_WORKSPACE_SERVICE_KEY_FILE": "/secrets/workspace-service.key",
                    "OMNI_SANDBOX_KEY_FILE": "/secrets/controller.key",
                    "OMNI_WORKSPACE_BROKER_URL": "http://host.docker.internal:19102",
                    "OMNI_SANDBOX_ENDPOINT": "http://sandbox-controller:8080",
                    "OMNI_ALLOWED_ORIGINS": "http://localhost:8788",
                    "OMNI_ENABLE_AGENT": "true",
                    "OMNI_ENABLE_TOOLS": "true",
                    "OMNI_SANDBOX_SNAPSHOT_ROOT": deploy.LINUX_ROOT + "/snapshots",
                    "OMNI_INTERACTIVE_PREVIEW": "true",
                },
                "volumes": [
                    volume("bind", "/data-source", "/data"),
                    volume("bind", "/secret-source", "/secrets", name != "init"),
                ],
                "ports": [port(8788, 8787)] if name == "api" else [],
            }
            for name in ("api", "worker", "init")
        },
    }
    config["services"]["postgres"] = {
        "image": "docker.m.daocloud.io/library/postgres@sha256:665dc7b37ac52a17e298511414c21d23ffa4e6a6ef6225b3e5d59c7afb35e994",
        "networks": {"default": None},
        "volumes": [volume("volume", "preview-postgres", "/var/lib/postgresql/data")],
        "ports": [],
        "environment": {
            "POSTGRES_DB": "omniharness",
            "POSTGRES_USER": "omniharness",
            "POSTGRES_PASSWORD": "private",
        },
    }
    config["services"]["sandbox-controller"] = {
        "image": "docker.m.daocloud.io/opensandbox/server@sha256:25bebdcf1cf7724eb923faecbdbbf82d4038c6ab35c736151574726d14006f30",
        "networks": {"default": None},
        "command": ["--config", "/config.toml"],
        "ports": [port(19100, 8080)],
        "volumes": [
            volume("bind", "/var/run/docker.sock", "/var/run/docker.sock"),
            volume("bind", "/controller-source", "/config.toml", True),
            volume("bind", deploy.LINUX_ROOT + "/controller", "/var/lib/opensandbox"),
            volume("bind", deploy.LINUX_ROOT + "/snapshots", deploy.LINUX_ROOT + "/snapshots", True),
        ],
    }
    monkeypatch.setattr(deploy, "compose", lambda *args, **kwargs: json.dumps(config).encode())
    deploy._verify_compose_profile(profile)
    config["services"]["worker"]["environment"]["OMNI_INTERACTIVE_PREVIEW"] = "false"
    with pytest.raises(ValueError, match="differs"):
        deploy._verify_compose_profile(profile)
    config["services"]["worker"]["environment"]["OMNI_INTERACTIVE_PREVIEW"] = "true"
    config["services"]["api"]["environment"]["OMNI_SANDBOX_IMAGE"] = "wrong-tool"
    with pytest.raises(ValueError, match="differs"):
        deploy._verify_compose_profile(profile)
    config["services"]["api"]["environment"]["OMNI_SANDBOX_IMAGE"] = "expected-tool"
    legacy = profile.replace(b"OMNI_INTERACTIVE_PREVIEW=true\n", b"")
    for name in ("api", "worker", "init"):
        config["services"][name]["environment"]["OMNI_INTERACTIVE_PREVIEW"] = "false"
    deploy._verify_compose_profile(legacy)
    config["services"]["api"]["volumes"][0]["source"] = "/other-project"
    with pytest.raises(ValueError, match="differs"):
        deploy._verify_compose_profile(legacy)
    config["services"]["api"]["volumes"][0]["source"] = "/data-source"
    config["services"]["sandbox-controller"]["command"] = ["unexpected"]
    with pytest.raises(ValueError, match="differ"):
        deploy._verify_compose_profile(legacy)
    config["services"]["sandbox-controller"]["command"] = ["--config", "/config.toml"]
    config["services"]["postgres"]["image"] = "untrusted"
    with pytest.raises(ValueError, match="differ"):
        deploy._verify_compose_profile(legacy)
    config["services"]["postgres"]["image"] = (
        "docker.m.daocloud.io/library/postgres@sha256:665dc7b37ac52a17e298511414c21d23ffa4e6a6ef6225b3e5d59c7afb35e994"
    )
    config["volumes"]["preview-postgres"]["name"] = "production_database"
    with pytest.raises(ValueError, match="differs"):
        deploy._verify_compose_profile(legacy)
    config["volumes"]["preview-postgres"]["name"] = deploy.PROJECT + "_preview-postgres"
    config["volumes"]["preview-postgres"]["external"] = True
    with pytest.raises(ValueError, match="differs"):
        deploy._verify_compose_profile(legacy)
    config["volumes"]["preview-postgres"].pop("external")
    config["networks"]["default"]["external"] = True
    with pytest.raises(ValueError, match="differs"):
        deploy._verify_compose_profile(legacy)


def test_successful_migration_requires_matching_proof_and_revision(monkeypatch, tmp_path):
    private = tmp_path / "preview"
    secret = private / "secrets"
    secret.mkdir(parents=True)
    directory = secret / ("upgrade-" + "a" * 16)
    directory.mkdir()
    original = b"OMNI_V6_IMAGE=old\nOMNI_V6_TOOL_IMAGE=oldtool\n"
    monkeypatch.setattr(deploy, "PRIVATE", private)
    monkeypatch.setattr(deploy, "_preview_offline", lambda: None)
    monkeypatch.setattr(deploy, "_read_verified_backup", lambda name: (directory, original))
    monkeypatch.setattr(deploy, "_verify_live_preview", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "_image_id", lambda _: None)
    monkeypatch.setattr(deploy, "_verify_compose_profile", lambda _: None)
    monkeypatch.setattr(deploy, "compose", lambda *a, **kw: b"")
    state = {"heads": ["0010"], "revisions": ["0010"]}
    monkeypatch.setattr(deploy, "_migration_revision_state", lambda: state)
    profile = deploy._replace_profile(original, "app", "tool")
    (secret / "compose.env").write_bytes(profile)
    with pytest.raises(ValueError, match="proof"):
        deploy._verify_interactive_start()
    deploy.migrate_upgrade(directory.name)
    assert not (secret / "migration-blocked.json").exists()
    deploy._verify_interactive_start()
    state = {"heads": ["0010"], "revisions": ["0009"]}
    with pytest.raises(ValueError, match="revision changed"):
        deploy._verify_interactive_start()
    (secret / "compose.env").write_bytes(profile + b"OTHER=changed\n")
    with pytest.raises(ValueError, match="proof"):
        deploy._verify_interactive_start()


def test_failed_migration_restores_profile_and_blocks_start(monkeypatch, tmp_path):
    private = tmp_path / "preview"
    secret = private / "secrets"
    secret.mkdir(parents=True)
    directory = secret / ("upgrade-" + "b" * 16)
    directory.mkdir()
    original = b"OMNI_V6_IMAGE=old\nOMNI_V6_TOOL_IMAGE=oldtool\n"
    monkeypatch.setattr(deploy, "PRIVATE", private)
    monkeypatch.setattr(deploy, "_preview_offline", lambda: None)
    monkeypatch.setattr(deploy, "_read_verified_backup", lambda name: (directory, original))
    monkeypatch.setattr(deploy, "_verify_live_preview", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "_image_id", lambda _: None)
    monkeypatch.setattr(deploy, "_verify_compose_profile", lambda _: None)

    def broken(*args, **kwargs):
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(deploy, "compose", broken)
    profile = deploy._replace_profile(original, "app", "tool")
    (secret / "compose.env").write_bytes(profile)
    with pytest.raises(RuntimeError, match="simulated"):
        deploy.migrate_upgrade(directory.name)
    assert (secret / "compose.env").read_bytes() == original
    assert (secret / "migration-blocked.json").exists()
    with pytest.raises(ValueError, match="recovery"):
        deploy._verify_interactive_start()


def test_offline_backup_captures_data_and_rejects_tamper(monkeypatch, tmp_path):
    import io
    import tarfile
    import types

    private = tmp_path / "preview"
    (private / "secrets").mkdir(parents=True)
    (private / "data" / "artifacts").mkdir(parents=True)
    (private / "data" / "artifacts" / "evidence.txt").write_bytes(b"evidence")
    (private / "native" / "recovery" / "op").mkdir(parents=True)
    (private / "native" / "recovery" / "op" / "before").write_bytes(b"original")
    (private / "native" / "snapshots" / "empty").mkdir(parents=True)
    original = b"OMNI_V6_IMAGE=old\nOMNI_V6_TOOL_IMAGE=oldtool\n"
    (private / "secrets" / "compose.env").write_bytes(original)
    monkeypatch.setattr(deploy, "PRIVATE", private)
    monkeypatch.setattr(deploy, "_preview_offline", lambda: None)
    monkeypatch.setattr(deploy, "_verify_compose_profile", lambda _: None)
    monkeypatch.setattr(deploy, "_verify_live_preview", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "_linux_snapshot_inventory", lambda: [b"stable"])
    monkeypatch.setattr(deploy, "wsl", lambda *a, **k: b"1024\t/snapshots")

    def fake_compose(*args, **kwargs):
        if "psql" in args:
            return b"1024"
        if "pg_dump" in args:
            return b"pg archive"
        if "pg_restore" in args:
            return b"archive listing"
        raise AssertionError("unexpected compose call")

    monkeypatch.setattr(deploy, "compose", fake_compose)

    def fake_tar(*args, **kwargs):
        with tarfile.open(fileobj=kwargs["stdout"], mode="w:") as tar:
            directory = tarfile.TarInfo("snapshots")
            directory.type = tarfile.DIRTYPE
            tar.addfile(directory)
            data = b"snapshot"
            item = tarfile.TarInfo("snapshots/file")
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(deploy.subprocess, "run", fake_tar)
    backup = deploy._backup_preview_data(include_data=True)
    (backup / "policy-result.json").write_text("{}")
    _, restored_profile = deploy._read_verified_backup(backup.name)
    assert restored_profile == original
    entries = json.loads((backup / "data-manifest.json").read_text())
    assert entries["data/artifacts/evidence.txt"]["size"] == 8
    assert entries["native/recovery/op/before"]["size"] == 8
    assert "native/snapshots/empty/" in entries
    with (backup / "preview-data.zip").open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValueError, match="data backup verification"):
        deploy._read_verified_backup(backup.name)


def test_preflight_backup_without_offline_data_not_valid_for_upgrade(monkeypatch, tmp_path):
    private = tmp_path / "preview"
    secret = private / "secrets"
    directory = secret / ("upgrade-" + "f" * 16)
    directory.mkdir(parents=True)
    original = b"profile"
    dump = b"dump"
    (directory / "compose.env").write_bytes(original)
    (directory / "database.dump").write_bytes(dump)
    (directory / "policy-result.json").write_text("{}")
    (directory / "backup.json").write_text(
        json.dumps(
            {
                "instance": deploy.PROJECT,
                "profile_sha256": hashlib.sha256(original).hexdigest(),
                "database_sha256": hashlib.sha256(dump).hexdigest(),
            }
        )
    )
    monkeypatch.setattr(deploy, "PRIVATE", private)
    assert deploy._read_verified_backup(directory.name, require_policy=False)[1] == original
    with pytest.raises(ValueError, match="data backup verification"):
        deploy._read_verified_backup(directory.name)


def test_backup_rejects_insufficient_space(monkeypatch, tmp_path):
    import types

    private = tmp_path / "preview"
    (private / "secrets").mkdir(parents=True)
    (private / "secrets" / "compose.env").write_bytes(b"profile")
    monkeypatch.setattr(deploy, "PRIVATE", private)
    monkeypatch.setattr(deploy, "_verify_compose_profile", lambda _: None)
    monkeypatch.setattr(deploy, "_verify_live_preview", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "compose", lambda *a, **k: b"100")
    monkeypatch.setattr(deploy.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=1))
    with pytest.raises(ValueError, match="space"):
        deploy._backup_preview_data()
    assert not list((private / "secrets").glob("upgrade-*"))


def test_compose_clears_inherited_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy, "_assert_compose_locations", lambda: None)
    monkeypatch.setattr(deploy, "mapped", lambda _: "/test/profile")
    monkeypatch.setattr(deploy, "wsl", lambda *a, **k: calls.append(a) or b"")
    deploy.compose("config", "--quiet")
    assert calls[0][:2] == ("env", "-i")
    assert "docker" in calls[0] and "compose" in calls[0]


def test_backup_rejects_reparse_ancestor(monkeypatch, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    private = tmp_path / "preview"
    parent = private / "native"
    (parent / "snapshots").mkdir(parents=True)
    monkeypatch.setattr(deploy, "PRIVATE", private)
    original = Path.lstat

    def fake(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == parent:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value

    monkeypatch.setattr(Path, "lstat", fake)
    with pytest.raises(ValueError, match="unsafe"):
        deploy._regular_tree(parent / "snapshots")


def test_snapshot_tar_rejects_symlink_and_escape(tmp_path):
    import tarfile

    for name, kind in [("snapshots/link", "link"), ("snapshots/../escape", "file")]:
        p = tmp_path / (kind + ".tar")
        with tarfile.open(p, "w:") as archive:
            item = tarfile.TarInfo(name)
            if kind == "link":
                item.type = tarfile.SYMTYPE
                item.linkname = "/etc/passwd"
            archive.addfile(item)
        with pytest.raises(ValueError, match="unsafe"):
            deploy._check_tar(p)


def test_declared_compose_paths_reject_override_and_duplicate(monkeypatch, tmp_path):
    private = tmp_path / "preview"
    secret = private / "secrets"
    data = private / "data"
    secret.mkdir(parents=True)
    data.mkdir()
    (secret / "controller.env").write_text("")
    (private / "controller.toml").write_text("")
    monkeypatch.setattr(deploy, "PRIVATE", private)
    monkeypatch.setattr(deploy, "_assert_project_root", lambda: None)
    monkeypatch.setattr(deploy, "mapped", lambda path: "/fixed/" + path.name)
    profile = (
        b"OMNI_V6_DATA_DIR=/fixed/data\nOMNI_V6_SECRET_DIR=/fixed/secrets\n"
        b"OMNI_V6_CONTROLLER_ENV=/fixed/controller.env\nOMNI_V6_CONTROLLER_CONFIG=/fixed/controller.toml\n"
    )
    target = secret / "compose.env"
    target.write_bytes(profile)
    deploy._assert_compose_locations()
    target.write_bytes(profile.replace(b"/fixed/data", b"/another/project"))
    with pytest.raises(ValueError, match="fixed isolated"):
        deploy._assert_compose_locations()
    target.write_bytes(profile + b"OMNI_V6_DATA_DIR=/other\n")
    with pytest.raises(ValueError, match="duplicated"):
        deploy._assert_compose_locations()


def test_backup_rejects_file_changed_during_capture(monkeypatch, tmp_path):
    private = tmp_path / "preview"
    (private / "data").mkdir(parents=True)
    (private / "secrets").mkdir()
    target = private / "data" / "artifact.txt"
    target.write_text("before")
    monkeypatch.setattr(deploy, "PRIVATE", private)
    old = target.lstat()
    target.write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        deploy._stable_stat(target, old)


def test_quiesce_api_stop_error_restores_only_api_if_safe(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(deploy, "audit_preview", lambda **kwargs: None)
    monkeypatch.setattr(deploy, "_backup_preview_data", lambda: tmp_path)
    monkeypatch.setattr(deploy, "_broker_online", lambda: None)

    def fake(*args, **kwargs):
        calls.append(args)
        if args == ("stop", "--timeout", "30", "api"):
            raise TimeoutError("after stop")
        if args == ("ps", "--all", "--format", "json"):
            return b"\n".join(
                json.dumps(x).encode()
                for x in [{"Service": "api", "State": "exited"}, {"Service": "worker", "State": "running"}]
            )
        return b""

    monkeypatch.setattr(deploy, "compose", fake)
    with pytest.raises(TimeoutError):
        deploy.quiesce_preview()
    assert ("start", "api") in calls
    assert ("stop", "--timeout", "30", "worker") not in calls


def test_quiesce_api_stop_error_does_not_start_with_unknown_broker(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(deploy, "audit_preview", lambda **kwargs: None)
    monkeypatch.setattr(deploy, "_backup_preview_data", lambda: tmp_path)

    def offline():
        raise RuntimeError("unknown broker")

    monkeypatch.setattr(deploy, "_broker_online", offline)

    def fake(*args, **kwargs):
        calls.append(args)
        if args == ("stop", "--timeout", "30", "api"):
            raise TimeoutError("after stop")
        if args == ("ps", "--all", "--format", "json"):
            return b"\n".join(
                json.dumps(x).encode()
                for x in [{"Service": "api", "State": "exited"}, {"Service": "worker", "State": "running"}]
            )
        return b""

    monkeypatch.setattr(deploy, "compose", fake)
    with pytest.raises(TimeoutError):
        deploy.quiesce_preview()
    assert ("start", "api") not in calls


def test_quiesce_uses_distinct_pre_and_post_api_audits(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(deploy, "audit_preview", lambda **kwargs: calls.append(("audit", kwargs)))
    monkeypatch.setattr(deploy, "_backup_preview_data", lambda: tmp_path)
    monkeypatch.setattr(deploy, "compose", lambda *a, **k: calls.append(a) or b"")
    monkeypatch.setattr(deploy, "stop_preview_broker", lambda: calls.append(("broker-stop",)))
    monkeypatch.setattr(deploy, "_preview_offline", lambda: calls.append(("offline",)))
    deploy.quiesce_preview()
    assert [v[1] for v in calls if v[0] == "audit"] == [{}, {}, {"api_stopped": True}]
    assert calls.index(("stop", "--timeout", "30", "api")) < calls.index(("audit", {"api_stopped": True}))
    assert calls.index(("audit", {"api_stopped": True})) < calls.index(("stop", "--timeout", "30", "worker"))


def test_running_container_inspect_rejects_postgres_volume_drift(monkeypatch):
    ids = {
        name: ("a" + str(index)) * 32
        for index, name in enumerate(("api", "worker", "postgres", "sandbox-controller"), 1)
    }
    profile = (
        b"OMNI_V6_IMAGE=app\nOMNI_V6_TOOL_IMAGE=tool\nOMNI_V6_POSTGRES_PASSWORD=private\n"
        b"OMNI_V6_DATA_DIR=/data-source\nOMNI_V6_SECRET_DIR=/secret-source\n"
        b"OMNI_V6_CONTROLLER_CONFIG=/controller-source\n"
    )

    def mount(kind, source, target, rw):
        return {
            "Type": kind,
            "Name": source if kind == "volume" else None,
            "Source": source,
            "Destination": target,
            "RW": rw,
        }

    expected = {
        "api": [
            mount("bind", "/data-source", "/data", True),
            mount("bind", "/secret-source", "/secrets", False),
        ],
        "worker": [
            mount("bind", "/data-source", "/data", True),
            mount("bind", "/secret-source", "/secrets", False),
        ],
        "postgres": [mount("volume", deploy.PROJECT + "_preview-postgres", "/var/lib/postgresql/data", True)],
        "sandbox-controller": [
            mount("bind", "/var/run/docker.sock", "/var/run/docker.sock", True),
            mount("bind", "/controller-source", "/config.toml", False),
            mount("bind", deploy.LINUX_ROOT + "/controller", "/var/lib/opensandbox", True),
            mount("bind", deploy.LINUX_ROOT + "/snapshots", deploy.LINUX_ROOT + "/snapshots", False),
        ],
    }
    app_env = {
        "OMNI_DATABASE_URL": "postgresql+psycopg://omniharness:private@postgres:5432/omniharness",
        "OMNI_INSTANCE_ID": deploy.PROJECT,
        "OMNI_V6_PREVIEW": "true",
        "OMNI_INTERACTIVE_PREVIEW": "false",
        "OMNI_ENABLE_AGENT": "true",
        "OMNI_ENABLE_TOOLS": "true",
        "OMNI_ALLOWED_ORIGINS": "http://localhost:8788",
        "OMNI_DATA_DIR": "/data",
        "OMNI_SECRET_DIR": "/secrets",
        "OMNI_WORKSPACE_SERVICE_KEY_FILE": "/secrets/workspace-service.key",
        "OMNI_SANDBOX_KEY_FILE": "/secrets/controller.key",
        "OMNI_WORKSPACE_BROKER_URL": "http://host.docker.internal:19102",
        "OMNI_SANDBOX_ENDPOINT": "http://sandbox-controller:8080",
        "OMNI_SANDBOX_IMAGE": "tool",
        "OMNI_SANDBOX_SNAPSHOT_ROOT": deploy.LINUX_ROOT + "/snapshots",
        "OMNI_CHECKPOINT_SCHEMA": "omniharness_v6_checkpoints",
    }
    envs = {
        "api": [f"{k}={v}" for k, v in app_env.items()],
        "worker": [f"{k}={v}" for k, v in app_env.items()],
        "postgres": ["POSTGRES_DB=omniharness", "POSTGRES_USER=omniharness", "POSTGRES_PASSWORD=private"],
        "sandbox-controller": [],
    }

    def fake_compose(*args, **kwargs):
        return ids[args[-1]].encode()

    def fake_wsl(*args, **kwargs):
        if args[:3] == ("docker", "image", "inspect"):
            return b"sha256:checked"
        if args[:3] == ("docker", "volume", "inspect"):
            return json.dumps(
                [
                    {
                        "Driver": "local",
                        "Options": None,
                        "Labels": {
                            "com.docker.compose.project": deploy.PROJECT,
                            "com.docker.compose.volume": "preview-postgres",
                        },
                    }
                ]
            ).encode()
        if args[:3] == ("docker", "network", "inspect"):
            return json.dumps(
                [
                    {
                        "Driver": "bridge",
                        "Options": {},
                        "Labels": {
                            "com.docker.compose.project": deploy.PROJECT,
                            "com.docker.compose.network": "default",
                        },
                    }
                ]
            ).encode()
        if args[:2] == ("docker", "inspect"):
            name = next(name for name, ident in ids.items() if ident == args[2])
            return json.dumps(
                [
                    {
                        "Image": "sha256:checked",
                        "Config": {
                            "Env": envs[name],
                            "Labels": {
                                "com.docker.compose.project": deploy.PROJECT,
                                "com.docker.compose.service": name,
                            },
                        },
                        "State": {"Status": "running"},
                        "HostConfig": {"Privileged": False, "NetworkMode": deploy.PROJECT + "_default"},
                        "NetworkSettings": {"Networks": {deploy.PROJECT + "_default": {}}},
                        "Mounts": expected[name],
                    }
                ]
            ).encode()
        raise AssertionError("unexpected WSL call")

    monkeypatch.setattr(deploy, "compose", fake_compose)
    monkeypatch.setattr(deploy, "wsl", fake_wsl)
    deploy._verify_live_preview(profile)
    envs["api"].remove("OMNI_INTERACTIVE_PREVIEW=false")
    deploy._verify_live_preview(profile)  # The old application defaults missing flag to false.
    with pytest.raises(ValueError, match="environment differs"):
        deploy._verify_live_preview(profile + b"OMNI_INTERACTIVE_PREVIEW=true\n")
    envs["api"].append("OMNI_INTERACTIVE_PREVIEW=false")
    expected["postgres"][0]["Name"] = "production_volume"
    with pytest.raises(ValueError, match="mounts differ"):
        deploy._verify_live_preview(profile)
    expected["postgres"][0]["Name"] = deploy.PROJECT + "_preview-postgres"
    envs["worker"].append("OMNI_DATABASE_URL=sensitive-canary")
    with pytest.raises(ValueError, match="environment differs") as error:
        deploy._verify_live_preview(profile)
    assert "sensitive-canary" not in str(error.value)
    envs["worker"].pop()
    envs["postgres"][-1] = "POSTGRES_PASSWORD=sensitive-canary"
    with pytest.raises(ValueError, match="environment differs") as error:
        deploy._verify_live_preview(profile)
    assert "sensitive-canary" not in str(error.value)
