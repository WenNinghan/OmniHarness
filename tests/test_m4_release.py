"""Release orchestration unit tests: never contact or stop production services."""
import hashlib
import io
import json
import tarfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import m4_production_deploy as release

IMAGE = "sha256:" + "a" * 64
OLD = "sha256:" + "b" * 64
OLD_TOOL = "sha256:95ece64f0e9b4196a12fcc4c5098664bc5af993bd0ddd14e9957f29bf0857d9e"


def inventory(revision="0009"):
    return {"revision": revision, "tables": {
        "public.model_configs": {"count": 25, "sha256": "models", "legacy_sha256": "models-old"},
        "public.approvals": {"count": 3, "sha256": "approvals", "legacy_sha256": "approvals"},
        "omniharness_checkpoints.checkpoints": {"count": 4, "sha256": "checkpoints", "legacy_sha256": "checkpoints"},
    }, "active_runs": 0, "unsafe_tools": 0, "uncertain_operations": 0,
    "output_limits": [{"max_output_tokens": 100000, "count": 25}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "PRIVATE", tmp_path / ".omniharness/m4-production")
    monkeypatch.setattr(release, "SECRET", tmp_path / ".omniharness/secrets")
    monkeypatch.setattr(release, "private_directory", lambda path: path.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(release, "mapped", lambda path: str(path))
    release.PRIVATE.mkdir(parents=True)
    release.SECRET.mkdir(parents=True)
    owners = {name: {"id": name, "name": "omniharness-" + name + "-1", "state": "running",
                     "image": OLD if name != "postgres" else IMAGE} for name in ("api", "worker", "postgres", "sandbox-controller")}
    monkeypatch.setattr(release, "ownership", lambda: deepcopy(owners))
    monkeypatch.setattr(release, "inventory", lambda: inventory())
    monkeypatch.setattr(release, "stopped_inventory", lambda _: inventory())
    monkeypatch.setattr(release, "owned_sandboxes", lambda *_: [])
    monkeypatch.setattr(release, "image_id", lambda image: image)
    return SimpleNamespace(root=tmp_path, owners=owners, monkeypatch=monkeypatch)


def test_same_schema_checks_every_table_and_column_without_legacy_projection():
    before = inventory()
    assert release.migration_preserved(before, deepcopy(before))
    for table in before["tables"]:
        after = deepcopy(before)
        after["tables"][table]["sha256"] = "changed"
        assert not release.migration_preserved(before, after)
    after = deepcopy(before)
    after["tables"]["new-unexpected-table"] = {}
    assert not release.migration_preserved(before, after)


def test_creation_storage_blocks_old_release_tool_before_stopping_services(env):
    value = {**inventory(), "external_creation_storage": True}
    calls = []
    env.monkeypatch.setattr(release, "inventory", lambda: value)
    env.monkeypatch.setattr(release, "docker", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="Independent creation storage"):
        release.quiesce()
    with pytest.raises(ValueError, match="Independent creation storage"):
        release.prepare(IMAGE)
    assert calls == []


def test_0008_migration_keeps_existing_legacy_hashes_and_limits():
    before, after = inventory("0008"), inventory()
    after["tables"]["public.model_configs"]["sha256"] = "new-context-column"
    assert release.migration_preserved(before, after)
    after["tables"]["public.model_configs"]["legacy_sha256"] = "changed-price"
    assert not release.migration_preserved(before, after)
    after = inventory()
    after["tables"]["public.unexpected_seed"] = {"count": 1}
    assert not release.migration_preserved(before, after)


def test_0009_to_0010_requires_exact_old_data_and_only_empty_clarifications():
    before = inventory()
    before["tables"]["public.alembic_version"] = {"count": 1, "sha256": "old"}
    after = deepcopy(before)
    after["revision"] = "0010"
    after["tables"]["public.alembic_version"]["sha256"] = "new"
    after["tables"]["public.clarifications"] = {"count": 0, "sha256": "empty"}
    assert release.migration_preserved(before, after)
    changed = deepcopy(after)
    changed["tables"]["public.model_configs"]["sha256"] = "lost-data"
    assert not release.migration_preserved(before, changed)
    changed = deepcopy(after)
    changed["tables"]["public.clarifications"]["count"] = 1
    assert not release.migration_preserved(before, changed)
    changed = deepcopy(after)
    changed["tables"]["public.extra"] = {"count": 0}
    assert not release.migration_preserved(before, changed)
    assert release.migration_preserved(after, deepcopy(after))


def test_0010_backup_requires_complete_stopped_m4_state(env):
    directory, manifest = backup_fixture(env)
    manifest["inventory"]["revision"] = "0010"
    manifest["complete_m4"] = False
    release.write_json(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="complete stopped"):
        release.validated_backup(directory)


@pytest.mark.parametrize("key", ["active_runs", "unsafe_tools", "uncertain_operations"])
def test_unknown_or_active_operations_block_backup(key):
    value = inventory()
    value[key] = 1
    with pytest.raises(ValueError, match="prevent"):
        release.require_quiet(value)


def test_quiescence_closes_ingress_then_rechecks_before_stopping_tool_services(env):
    steps = []
    env.monkeypatch.setattr(release, "docker", lambda *args: steps.append(args))
    env.monkeypatch.setattr(release, "broker_stop", lambda: steps.append(("broker-stop",)))
    env.monkeypatch.setattr(release, "stopped_inventory", lambda _: steps.append(("recheck",)) or inventory())
    release.quiesce()
    assert steps == [("stop", "--time", "30", "omniharness-api-1", "omniharness-worker-1"),
                     ("recheck",), ("broker-stop",), ("stop", "--time", "30", "omniharness-sandbox-controller-1")]


def test_racing_request_aborts_quiescence_without_backup_or_restart(env):
    steps = []
    env.monkeypatch.setattr(release, "docker", lambda *args: steps.append(args))
    env.monkeypatch.setattr(release, "broker_stop", lambda: pytest.fail("must not continue"))
    env.monkeypatch.setattr(release, "stopped_inventory", lambda _: {**inventory(), "active_runs": 1})
    with pytest.raises(ValueError):
        release.quiesce()
    assert len(steps) == 1 and steps[0][0] == "stop"


def test_existing_owned_sandbox_blocks_even_if_business_ledger_is_terminal(env):
    env.monkeypatch.setattr(release, "owned_sandboxes", lambda *_: ["leftover"])
    env.monkeypatch.setattr(release, "docker", lambda *args: pytest.fail("must not stop anything"))
    with pytest.raises(ValueError, match="sandbox containers"):
        release.quiesce()


def test_m4_backup_rejects_live_services_before_dump(env):
    env.monkeypatch.setattr(release, "broker_processes", lambda: [])
    env.monkeypatch.setattr(release, "docker", lambda *args: pytest.fail("must not dump"))
    with pytest.raises(ValueError, match="requires API"):
        release.backup(stopped=True)


def tar_fixture(path, *, name="record.json", kind=None):
    with tarfile.open(path, "w") as archive:
        item = tarfile.TarInfo(name)
        if kind is not None:
            item.type, item.linkname = kind, "private"
            archive.addfile(item)
        else:
            data = b"saved recovery material"
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))


@pytest.mark.parametrize("name,kind", [("../escape", None), ("/absolute", None),
    ("alias", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE), ("socket", tarfile.FIFOTYPE)])
def test_archive_rejects_traversal_links_and_special_files(tmp_path, name, kind):
    path = tmp_path / "unsafe.tar"
    tar_fixture(path, name=name, kind=kind)
    with pytest.raises(ValueError, match="unsafe"):
        release.archive_inventory(path)


def backup_fixture(env):
    directory = release.PRIVATE / "backups/fixture"
    directory.mkdir(parents=True)
    (directory / "database.pgdump").write_bytes(b"PGDMPfixture")
    for folder in ("data", "secrets", "native", "helper-src", "controller-secrets", "config"):
        (directory / folder).mkdir()
        (directory / folder / "fixture").write_text(folder)
    (directory / "config/deployment.env").write_text("OMNI_SANDBOX_IMAGE=" + OLD_TOOL + "\n")
    linux = {}
    for name in ("controller", "snapshots"):
        tar_fixture(directory / (name + ".tar"))
        linux[name] = release.archive_inventory(directory / (name + ".tar"))
    manifest = {"schema_version": 2, "source": str(release.ROOT), "project": release.PROJECT,
                "inventory": inventory(), "complete_m4": True, "stopped": True,
                "owners": env.owners, "linux_state": linux, "files": release.file_manifest(directory)}
    release.write_json(directory / "manifest.json", manifest)
    return directory, manifest


def test_complete_backup_revalidates_every_file_and_archive(env):
    directory, manifest = backup_fixture(env)
    assert release.validated_backup(directory) == manifest
    (directory / "native/fixture").write_text("changed recovery record")
    with pytest.raises(ValueError, match="contents"):
        release.validated_backup(directory)


def test_0009_incomplete_legacy_backup_cannot_be_used(env):
    directory, manifest = backup_fixture(env)
    manifest["complete_m4"] = False
    release.write_json(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="complete stopped"):
        release.validated_backup(directory)


@pytest.mark.parametrize("fail", [False, True])
def test_backup_verify_only_resumes_unchanged_services_after_success(env, fail):
    steps = []
    env.monkeypatch.setattr(release, "quiesce", lambda: steps.append("stop") or env.owners)
    env.monkeypatch.setattr(release, "backup", lambda **kwargs: steps.append(("backup", kwargs)) or Path("backup"))
    def verify(*_):
        steps.append("verify")
        if fail:
            raise ValueError("restore failed")
    env.monkeypatch.setattr(release, "verify", verify)
    env.monkeypatch.setattr(release, "resume_owned", lambda _: steps.append("resume-old"))
    if fail:
        with pytest.raises(ValueError):
            release.backup_verify(IMAGE)
    else:
        release.backup_verify(IMAGE)
    assert steps == ["stop", ("backup", {"stopped": True}), "verify", *([] if fail else ["resume-old"])]


def test_same_schema_verify_restores_db_and_checks_head_without_migration(env):
    directory, manifest = backup_fixture(env)
    env.monkeypatch.setattr(release, "verify_files", lambda *_: directory)
    env.monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1 if "inspect" in args[0] else 0))
    commands = []
    def docker(*args, **kwargs):
        commands.append((args, kwargs))
        if args[-1] == release.INVENTORY:
            return json.dumps(inventory()).encode()
        if args[-1] == release.VERIFY_KEYS:
            return b'{"credentials_readable":1}'
        if "ScriptDirectory" in str(args[-1]):
            return b"0009\n"
        return b""
    env.monkeypatch.setattr(release, "docker", docker)
    report = release.verify(directory, IMAGE)
    assert report["mode"] == "same-schema-update" and report["complete_m4_files_restored"]
    assert not any(args[-2:] == ("omniharness", "migrate") for args, _ in commands)
    assert any("pg_restore" in args and kwargs["data"].startswith(b"PGDMP") for args, kwargs in commands)
    assert report["manifest_sha256"] == hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()


def test_update_requires_matching_verified_image_and_stops_before_fresh_backup(env):
    directory, _ = backup_fixture(env)
    verified = {"image": IMAGE, "inventory": inventory(), "manifest_sha256": hashlib.sha256(
        (directory / "manifest.json").read_bytes()).hexdigest()}
    release.write_json(directory / "verification.json", verified)
    steps = []
    env.monkeypatch.setattr(release, "prepared_state", lambda _: release.PRIVATE)
    env.monkeypatch.setattr(release, "quiesce", lambda: steps.append("stop"))
    env.monkeypatch.setattr(release, "backup", lambda **kwargs: steps.append("fresh-backup") or directory)
    env.monkeypatch.setattr(release, "verify", lambda *args: steps.append("restore-verify"))
    env.monkeypatch.setattr(release, "activate_prepared", lambda _: steps.append("activate"))
    env.monkeypatch.setattr(release, "broker_start", lambda: steps.append("broker"))
    env.monkeypatch.setattr(release, "compose", lambda *args: steps.append("start-fixed-image"))
    with pytest.raises(ValueError, match="Choose update"):
        release.cutover(IMAGE, directory)
    assert not steps
    release.cutover(IMAGE, directory, same_schema=True)
    assert steps == ["stop", "fresh-backup", "restore-verify", "activate", "broker", "start-fixed-image"]


def test_migration_failure_never_starts_broker_or_application(env):
    directory, _ = backup_fixture(env)
    verified = {"image": IMAGE, "source_revision": "0009", "target_revision": "0010",
                "inventory": inventory("0010"), "manifest_sha256": hashlib.sha256(
                    (directory / "manifest.json").read_bytes()).hexdigest()}
    release.write_json(directory / "verification.json", verified)
    env.monkeypatch.setattr(release, "prepared_state", lambda _: release.PRIVATE)
    env.monkeypatch.setattr(release, "quiesce", lambda: None)
    env.monkeypatch.setattr(release, "backup", lambda **kwargs: directory)
    env.monkeypatch.setattr(release, "verify", lambda *args: None)
    env.monkeypatch.setattr(release, "activate_prepared", lambda _: None)
    env.monkeypatch.setattr(release, "broker_start", lambda: pytest.fail("must not start broker"))
    def fail_migration(*args):
        assert args[-2:] == ("omniharness", "migrate")
        raise RuntimeError("migration failed")
    env.monkeypatch.setattr(release, "compose", fail_migration)
    with pytest.raises(RuntimeError, match="migration failed"):
        release.cutover(IMAGE, directory)


def test_migration_inventory_mismatch_blocks_start(env):
    directory, _ = backup_fixture(env)
    verified = {"image": IMAGE, "source_revision": "0009", "target_revision": "0010",
                "inventory": inventory("0010"), "manifest_sha256": hashlib.sha256(
                    (directory / "manifest.json").read_bytes()).hexdigest()}
    release.write_json(directory / "verification.json", verified)
    for name, callback in (("prepared_state", lambda _: release.PRIVATE), ("quiesce", lambda: None),
                           ("backup", lambda **kwargs: directory), ("verify", lambda *args: None),
                           ("activate_prepared", lambda _: None)):
        env.monkeypatch.setattr(release, name, callback)
    env.monkeypatch.setattr(release, "broker_start", lambda: pytest.fail("must not start broker"))
    env.monkeypatch.setattr(release, "compose", lambda *args: json.dumps(inventory("0009")).encode()
                        if args[-1] == release.INVENTORY else b"")
    with pytest.raises(ValueError, match="Live migration differs"):
        release.cutover(IMAGE, directory)


def test_prepare_0009_stages_configuration_without_touching_live_helpers(env):
    (release.ROOT / "deploy").mkdir()
    (release.ROOT / "deploy/opensandbox.toml").write_text("__M4_EXECD_IMAGE__ __M4_EGRESS_IMAGE__")
    source = release.PRIVATE / "releases" / IMAGE.split(":")[1] / "src"
    source.mkdir(parents=True)
    for name in ("broker.json", "controller.toml", "prepared.json"):
        (release.PRIVATE / name).write_text("unchanged-live")
    env.monkeypatch.setattr(release, "mounted_paths", lambda _: {"data": release.ROOT / ".omniharness/data", "secrets": release.SECRET})
    env.monkeypatch.setattr(release, "active_database", lambda _: "omniharness")
    calls = []
    env.monkeypatch.setattr(release, "wsl", lambda *args: calls.append(args))
    env.monkeypatch.setattr(release, "compose", lambda *args, **kwargs: calls.append((args, kwargs)))
    release.prepare(IMAGE)
    for name in ("broker.json", "controller.toml", "prepared.json"):
        assert (release.PRIVATE / name).read_text() == "unchanged-live"
    staged = release.prepared_state(IMAGE)
    assert staged != release.PRIVATE and (staged / "candidate.env").exists()
    assert "OMNI_SANDBOX_IMAGE=sha256:79950f88eb5387086a7089357e293055b8beb906da9cb728961b6e88d30cc64c" in (
        staged / "candidate.env").read_text()
    assert not any("pip" in str(args) or "venv" in args for args in calls)


def test_m4_rollback_retains_new_database_and_state_and_uses_the_backup_image(env):
    directory, manifest = backup_fixture(env)
    release.write_json(directory / "config/broker.json", {"instance_id": release.INSTANCE})
    (directory / "config/controller.toml").write_text("saved-controller-config")
    (directory / "controller-secrets/controller.env").write_text("fixture-controller-key")
    (release.PRIVATE / "secrets").mkdir()
    manifest["files"] = release.file_manifest(directory)
    manifest["files"].pop("manifest.json")
    release.write_json(directory / "manifest.json", manifest)
    commands, profiles, restored = [], [], []
    env.monkeypatch.setattr(release, "quiesce", lambda: env.owners)
    env.monkeypatch.setattr(release, "backup", lambda **kwargs: commands.append(("preserve-current", kwargs)) or Path("fresh-backup"))
    env.monkeypatch.setattr(release, "active_database", lambda _: "current_upgraded_db")
    env.monkeypatch.setattr(release, "docker", lambda *args, **kwargs: commands.append((args, kwargs)))
    def restore(_, name, destination):
        restored.append((name, destination))
        return manifest["linux_state"][name]
    env.monkeypatch.setattr(release, "restore_linux_archive", restore)
    def wsl(*args):
        assert len(restored) == 2  # Verify both trees before moving either.
        commands.append((args, {}))
    env.monkeypatch.setattr(release, "wsl", wsl)
    env.monkeypatch.setattr(release, "persist_profile", profiles.append)
    env.monkeypatch.setattr(release, "broker_start", lambda: None)
    env.monkeypatch.setattr(release, "compose", lambda *args: commands.append((args, {})))
    release.rollback(directory)
    assert commands[0] == ("preserve-current", {"stopped": True})
    assert "OMNI_RELEASE_IMAGE=" + OLD in profiles[0]
    assert "OMNI_SANDBOX_IMAGE=" + OLD_TOOL in profiles[0]
    assert release.TOOL not in profiles[0]
    assert "OMNI_DATABASE_NAME=omni_restore_" in profiles[0] and "OMNI_ENABLE_TOOLS=true" in profiles[0]
    assert not any("dropdb" in str(args) or "rmtree" in str(args) for args, _ in commands)
    assert any("retired" in str(args) for args, _ in commands)
    broker = release.read_json(release.PRIVATE / "broker.json")
    assert release.PRIVATE / "restores" in Path(broker["native_storage_root"]).parents
    assert (directory / "data/fixture").read_text() == "data"


@pytest.mark.parametrize("profile", ["", "OMNI_SANDBOX_IMAGE=tools:latest\n",
    "OMNI_SANDBOX_IMAGE=" + OLD_TOOL + "\nOMNI_SANDBOX_IMAGE=" + OLD_TOOL + "\n"])
def test_rollback_rejects_missing_mutable_or_duplicate_tool_pin_before_stopping(env, profile):
    directory, manifest = backup_fixture(env)
    (directory / "config/deployment.env").write_text(profile)
    manifest["files"]["config/deployment.env"] = hashlib.sha256((directory / "config/deployment.env").read_bytes()).hexdigest()
    release.write_json(directory / "manifest.json", manifest)
    env.monkeypatch.setattr(release, "quiesce", lambda: pytest.fail("must reject before stopping"))
    with pytest.raises(ValueError, match="exactly one immutable"):
        release.rollback(directory)


def test_old_tool_must_exist_locally_before_rollback_stops_services(env):
    directory, _ = backup_fixture(env)
    checked = []
    def inspect(image):
        checked.append(image)
        if image == OLD_TOOL:
            raise ValueError("Tool image unavailable")
        return image
    env.monkeypatch.setattr(release, "image_id", inspect)
    env.monkeypatch.setattr(release, "quiesce", lambda: pytest.fail("must reject before stopping"))
    with pytest.raises(ValueError, match="unavailable"):
        release.rollback(directory)
    assert checked == [OLD, OLD_TOOL]


def test_0008_recovery_does_not_require_a_tool_image(tmp_path):
    assert release.backup_tool_image(tmp_path, {"complete_m4": False}) is None
