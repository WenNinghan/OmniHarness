from copy import deepcopy

import pytest
from test_m4_release import IMAGE, backup_fixture, env, inventory  # noqa: F401

from omniharness.legacy_tool_retirement import LegacyMaintenanceScope
from scripts import m4_production_deploy as release
from scripts import retire_legacy_tool as maintenance


def scope():
    return LegacyMaintenanceScope(release.INSTANCE, "run-1", "a" * 64, "b" * 64, "c" * 64)


def unsafe_inventory():
    return {**inventory(), "unsafe_tools": 1, "uncertain_operations": 1,
            "unsafe_tool_ids": ["a" * 64], "unsafe_operation_ids": ["b" * 64]}


def test_exact_scope_is_the_only_backup_exception():
    value = unsafe_inventory()
    with pytest.raises(ValueError, match="prevent"):
        release.require_quiet(value)
    release.require_quiet(value, maintenance_scope=scope())
    for changed in ({"active_runs": 1}, {"unsafe_tools": 2}, {"uncertain_operations": 2},
                    {"unsafe_tool_ids": ["f" * 64]}, {"unsafe_operation_ids": []}):
        with pytest.raises(ValueError, match="exact single"):
            release.require_quiet({**value, **changed}, maintenance_scope=scope())
    with pytest.raises(ValueError, match="Unknown legacy"):
        release.require_quiet(value, maintenance_scope=True)
    with pytest.raises(ValueError, match="Independent creation"):
        release.require_quiet({**value, "external_creation_storage": True}, maintenance_scope=scope())


def test_scoped_quiescence_only_stops_owned_original_services(env):  # noqa: F811
    calls = []
    env.monkeypatch.setattr(release, "inventory", unsafe_inventory)
    env.monkeypatch.setattr(release, "stopped_inventory", lambda _: unsafe_inventory())
    env.monkeypatch.setattr(release, "docker", lambda *args: calls.append(args))
    env.monkeypatch.setattr(release, "broker_stop", lambda: calls.append(("broker-stop",)))
    release.quiesce(maintenance_scope=scope())
    assert calls == [("stop", "--time", "30", "omniharness-api-1", "omniharness-worker-1"),
                     ("broker-stop",), ("stop", "--time", "30", "omniharness-sandbox-controller-1")]


def test_racing_other_unknown_aborts_scoped_quiescence(env):  # noqa: F811
    env.monkeypatch.setattr(release, "inventory", unsafe_inventory)
    env.monkeypatch.setattr(release, "stopped_inventory", lambda _: {**unsafe_inventory(), "uncertain_operations": 2})
    env.monkeypatch.setattr(release, "docker", lambda *args: None)
    env.monkeypatch.setattr(release, "broker_stop", lambda: pytest.fail("Must stop before further mutations"))
    with pytest.raises(ValueError, match="exact single"):
        release.quiesce(maintenance_scope=scope())


def test_maintenance_backup_can_be_verified_but_cannot_be_used_for_release_or_rollback(env):  # noqa: F811
    directory, manifest = backup_fixture(env)
    manifest.update(inventory=unsafe_inventory(), legacy_maintenance_scope=scope().manifest())
    release.write_json(directory / "manifest.json", manifest)
    assert release.validated_backup(directory) == manifest
    env.monkeypatch.setattr(release, "quiesce", lambda: pytest.fail("Unsafe backup must reject before stopping"))
    with pytest.raises(ValueError, match="prevent"):
        release.rollback(directory)
    with pytest.raises(ValueError, match="prevent"):
        release.cutover(IMAGE, directory)
    manifest["legacy_maintenance_scope"]["ledger_id"] = "f" * 64
    release.write_json(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="exact single"):
        release.validated_backup(directory)


def test_host_epoch_cannot_change_between_preflight_and_write():
    original = {"epoch": {"engine_id": "engine", "boot_id": "boot", "processes": {"dockerd": {"pid": 5}},
                          "git_commit": "commit"}}
    maintenance.assert_same_epoch(original, deepcopy(original))
    for key in original["epoch"]:
        changed = deepcopy(original)
        changed["epoch"][key] = "different"
        with pytest.raises(ValueError, match="epoch changed"):
            maintenance.assert_same_epoch(original, changed)


def test_preflight_returns_the_persisted_evidence_to_the_execute_phase(tmp_path, monkeypatch):
    target = LegacyMaintenanceScope(release.INSTANCE, maintenance.RUN, maintenance.OPERATION, "b" * 64, "c" * 64)
    plan = {"scope": target.manifest(), "snapshot": {}, "masked": {}}
    proof = {"epoch": {"boot_id": "test-boot"}}
    credential = tmp_path / "ephemeral.env"
    credential.write_text("test-only")
    monkeypatch.setattr(release, "ownership", lambda: {})
    monkeypatch.setattr(release, "active_database", lambda _: "omniharness")
    monkeypatch.setattr(maintenance, "source_audit", lambda *_: "source-hash")
    monkeypatch.setattr(maintenance, "environment_file", lambda *_: credential)
    monkeypatch.setattr(maintenance, "database_action", lambda *_: deepcopy(plan))
    monkeypatch.setattr(maintenance, "collect_epoch", lambda *args, **kwargs: deepcopy(proof))
    _, returned, _ = maintenance.inspect_legacy(tmp_path)
    assert returned["preflight_evidence"] == proof
    assert release.read_json(tmp_path / "plan.json") == returned
    assert not credential.exists()


def test_maintenance_persists_original_service_states_before_stopping(tmp_path, monkeypatch):
    target = scope()
    plan = {"scope": target.manifest(), "snapshot": {}, "masked": {}, "preflight_evidence": {}}
    owners = {name: {"state": "running"} for name in ("api", "worker", "sandbox-controller")}
    manifest = {"code": "hash"}
    monkeypatch.setattr(maintenance, "AUDIT_ROOT", tmp_path)
    release.write_json(tmp_path / "self-test.json", {"passed": True, "code_manifest": manifest})
    monkeypatch.setattr(maintenance, "code_manifest", lambda: manifest)
    monkeypatch.setattr(release, "inventory", unsafe_inventory)
    monkeypatch.setattr(maintenance, "other_container_states", lambda: {})
    credential = tmp_path / "ephemeral.env"
    credential.write_text("test-only")
    monkeypatch.setattr(maintenance, "environment_file", lambda *_: credential)

    def interrupted(**kwargs):
        baseline = release.read_json(tmp_path / "maintenance-baseline.json")
        assert baseline["owners"] == owners
        assert baseline["inventory"] == unsafe_inventory()
        raise ValueError("simulated stop failure")

    monkeypatch.setattr(release, "quiesce", interrupted)
    with pytest.raises(ValueError, match="simulated stop failure"):
        maintenance.execute(tmp_path, owners, plan, "source-hash")
    assert not credential.exists()
