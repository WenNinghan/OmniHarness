import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniharness.workspace_broker import create_broker_app
from omniharness.workspace_io import (
    DictWorkspaceRegistry,
    NativeWorkspaceIO,
    WorkspaceError,
    WorkspaceGrant,
    WorkspaceIO,
)


@pytest.mark.parametrize('reply', ['{}', '[]', '{"state":"completed"}'])
def test_native_write_missing_result_evidence_is_unknown(workspace, monkeypatch, reply):
    from types import SimpleNamespace

    io, _, registry, _ = workspace
    native = NativeWorkspaceIO(
        registry, python_executable=sys.executable, module_file='trusted-module.py',
        native_storage_root=str(io.storage_root), native_protected_roots=(),
        mapped_native_storage_root=io.storage_root, snapshot_root=io.storage_root / 'snapshots',
        require_write_lease=lambda *_: None,
    )
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=reply, returncode=0))
    with pytest.raises(WorkspaceError) as error:
        native.patch('ws', 'instance', 1, path='src/app.py', content='changed', expected_sha256='a' * 64,
                     operation_id='native-unknown', run_id='run-1')
    assert error.value.code == 'native_result_unknown'


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "中文 workspace"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('original')\n", encoding="utf-8", newline="")
    (root / ".env").write_text("PRIVATE=test-secret", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("credential", encoding="utf-8")
    framework = tmp_path / "framework"
    framework.mkdir()
    registry = DictWorkspaceRegistry([
        WorkspaceGrant("ws", "instance", str(root), write_allow=("src",)),
    ])
    leases = []

    def require_lease(instance, workspace_id, run_id):
        if run_id != "run-1":
            raise WorkspaceError("lease_required", "Run does not hold the write lease.")
        leases.append((instance, workspace_id, run_id))

    io = WorkspaceIO(registry, tmp_path / "storage", protected_roots=(framework,), require_write_lease=require_lease)
    return io, root, registry, leases


def test_read_patch_retains_original_and_rejects_stale_version(workspace):
    io, root, _, leases = workspace
    original = io.read("ws", "instance", 1, "src/app.py")
    result = io.patch("ws", "instance", 1, path="src/app.py", content="print('changed')\n",
                      expected_sha256=original["sha256"], operation_id="op-1", run_id="run-1")
    assert (root / "src/app.py").read_text() == "print('changed')\n"
    assert result["before_sha256"] == original["sha256"]
    assert (io.storage_root / "recovery/op-1/before.bin").read_bytes() == original["content"].encode()
    assert leases == [("instance", "ws", "run-1")]
    with pytest.raises(WorkspaceError, match="changed since"):
        io.patch("ws", "instance", 1, path="src/app.py", content="stale", expected_sha256=original["sha256"],
                 operation_id="op-2", run_id="run-1")
    assert not (io.storage_root / "recovery/op-2").exists()
    with pytest.raises(WorkspaceError, match="already has recovery"):
        io.patch("ws", "instance", 1, path="src/app.py", content="duplicate", expected_sha256=result["sha256"],
                 operation_id="op-1", run_id="run-1")


@pytest.mark.parametrize("receipt", ["{", "{}", '{"state":"completed"}'])
def test_partial_completion_receipt_does_not_prove_write_completion(workspace, receipt):
    io, _, _, _ = workspace
    original = io.read("ws", "instance", 1, "src/app.py")
    io.patch("ws", "instance", 1, path="src/app.py", content="print(42)\n",
             expected_sha256=original["sha256"], operation_id="partial", run_id="run-1")
    valid = io.operation_result("ws", "instance", 1, "partial", "run-1")
    assert valid["recorded_completed"] and valid["matches_expected_result"]
    (io.storage_root / "recovery/partial/result.json").write_text(receipt, encoding="utf-8")
    incomplete = io.operation_result("ws", "instance", 1, "partial", "run-1")
    assert incomplete["matches_expected_result"] and not incomplete["recorded_completed"]
    assert incomplete["state"] == "uncertain"


@pytest.mark.parametrize("swap_after_grant", [False, True])
def test_registered_root_identity_rejects_replacement(workspace, monkeypatch, swap_after_grant):
    io, root, registry, _ = workspace
    identity = io.probe("ws", "instance", 1)["identity"]
    registry.grants["ws"] = replace(registry.grants["ws"], expected_identity=identity)
    original_grant = io._grant

    def swap():
        root.rename(root.with_name(root.name + " old"))
        root.mkdir()
        (root / "src").mkdir()
        (root / "src/app.py").write_text("replacement")

    if swap_after_grant:
        def grant_then_swap(*args):
            grant = original_grant(*args)
            swap()
            return grant
        monkeypatch.setattr(io, "_grant", grant_then_swap)
    else:
        swap()
    with pytest.raises(WorkspaceError) as error:
        io.read("ws", "instance", 1, "src/app.py")
    assert error.value.code == "workspace_replaced"


def test_new_file_and_write_allowlist(workspace):
    io, root, _, _ = workspace
    io.patch("ws", "instance", 1, path="src/new.txt", content="new", expected_sha256=None,
             operation_id="create", run_id="run-1")
    assert (root / "src/new.txt").read_text() == "new"
    for path in ("README.md", ".env", "src/missing/file.py"):
        with pytest.raises(WorkspaceError):
            io.patch("ws", "instance", 1, path=path, content="no", expected_sha256=None,
                     operation_id="create-no", run_id="run-1")


def test_same_version_concurrent_patches_have_one_winner(workspace):
    from concurrent.futures import ThreadPoolExecutor

    io, _, _, _ = workspace
    original = io.read("ws", "instance", 1, "src/app.py")

    def write(index):
        try:
            return io.patch("ws", "instance", 1, path="src/app.py", content=f"winner {index}",
                            expected_sha256=original["sha256"], operation_id=f"parallel-{index}", run_id="run-1")
        except WorkspaceError as exc:
            return {"error": exc.code}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [1, 2]))
    assert sum(r.get("state") == "completed" for r in results) == 1
    assert sum(r.get("error") == "version_conflict" for r in results) == 1


def test_write_without_authoritative_lease_is_denied(workspace):
    io, *_ = workspace
    with pytest.raises(WorkspaceError, match="lease"):
        io.patch("ws", "instance", 1, path="src/new.txt", content="no", expected_sha256=None,
                 operation_id="no-lease", run_id="run-other")


def test_lost_completion_record_keeps_recovery_and_never_replays(workspace, monkeypatch):
    io, root, _, _ = workspace
    original = io.read("ws", "instance", 1, "src/app.py")
    real_save = io._save_json

    def fail_result(path, data):
        if path.name == "result.json":
            raise OSError("test-only interrupted persistence")
        return real_save(path, data)

    monkeypatch.setattr(io, "_save_json", fail_result)
    with pytest.raises(WorkspaceError) as result:
        io.patch("ws", "instance", 1, path="src/app.py", content="written before interruption",
                 expected_sha256=original["sha256"], operation_id="lost", run_id="run-1")
    assert result.value.code == "write_result_unknown"
    assert (root / "src/app.py").read_text() == "written before interruption"
    recovery = io.operation_result("ws", "instance", 1, "lost", "run-1")
    assert recovery["state"] == "uncertain" and recovery["matches_expected_result"]
    assert not recovery["recorded_completed"]
    with pytest.raises(WorkspaceError):
        io.operation_result("ws", "instance", 1, "lost", "another-run")
    assert (io.storage_root / "recovery/lost/before.bin").read_bytes() == original["content"].encode()


def test_write_handle_prevents_external_windows_write(workspace, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows exclusive sharing semantics")
    io, root, _, _ = workspace
    original = io.read("ws", "instance", 1, "src/app.py")
    real_recovery = io._recovery

    def attempt_external_write(*args):
        with pytest.raises(PermissionError):
            (root / "src/app.py").write_text("external conflicting change")
        with pytest.raises(PermissionError):
            os.link(root / "src/app.py", root / "src/concurrent-hardlink.py")
        return real_recovery(*args)

    monkeypatch.setattr(io, "_recovery", attempt_external_write)
    io.patch("ws", "instance", 1, path="src/app.py", content="approved patch",
             expected_sha256=original["sha256"], operation_id="exclusive", run_id="run-1")


@pytest.mark.skipif(os.name != "nt", reason="Windows directory handle access check")
def test_held_ancestor_denies_reparse_write_access(workspace):
    import ctypes

    import omniharness.workspace_io as module

    io, root, _, _ = workspace
    with io._parents(root / "src/new.txt"):
        # FSCTL_SET_REPARSE_POINT needs a writable directory handle. Denying
        # that prerequisite prevents converting a pinned empty parent in place.
        handle = module._kernel.CreateFileW(str(root / "src"), 0x40000000, 7, None, 3, 0x02200000, None)
        try:
            assert handle == ctypes.c_void_p(-1).value
            assert ctypes.get_last_error() == 32  # ERROR_SHARING_VIOLATION
        finally:
            if handle != ctypes.c_void_p(-1).value:
                module._kernel.CloseHandle(handle)


@pytest.mark.parametrize("path", [
    "../outside", "/etc/passwd", "C:/Windows/win.ini", "\\\\server\\share", "src/../.env",
    "src//app.py", "src/app.py:stream", "src/app.py.", "src/app.py ", "src/CON", "src\\app.py",
])
def test_path_aliases_and_traversal_are_rejected(workspace, path):
    io, *_ = workspace
    with pytest.raises(WorkspaceError):
        io.read("ws", "instance", 1, path)


def test_protected_instance_version_and_revoked_grants(workspace):
    io, root, registry, _ = workspace
    for instance, version in (("other", 1), ("instance", 2)):
        with pytest.raises(WorkspaceError):
            io.read("ws", instance, version, "src/app.py")
    registry.grants["ws"] = replace(registry.grants["ws"], enabled=False)
    with pytest.raises(WorkspaceError, match="revoked"):
        io.read("ws", "instance", 1, "src/app.py")
    for blocked_root in (root.anchor, str(io.protected_roots[0])):
        registry.grants["ws"] = replace(registry.grants["ws"], root=blocked_root, enabled=True)
        with pytest.raises(WorkspaceError):
            io.probe("ws", "instance", 1)


def test_frozen_grant_remains_usable_after_policy_update_but_not_revocation(workspace):
    io, _, registry, _ = workspace
    original = registry.grants["ws"]
    registry.grants["ws"] = replace(original, version=2, read_allow=("other",), write_allow=())
    assert io.read("ws", "instance", 1, "src/app.py")["content"] == "print('original')\n"
    with pytest.raises(WorkspaceError):
        io.read("ws", "instance", 2, "src/app.py")
    registry.grants["ws"] = replace(registry.grants["ws"], enabled=False)
    with pytest.raises(WorkspaceError, match="revoked"):
        io.read("ws", "instance", 1, "src/app.py")


def test_snapshot_filters_secrets_and_never_writes_back(workspace):
    io, root, _, _ = workspace
    snapshot = io.snapshot("ws", "instance", 1, "run-1")
    assert [f["path"] for f in snapshot["files"]] == ["src/app.py"]
    copied = Path(snapshot["workdir"])
    assert not (copied / ".env").exists() and not (copied / ".git").exists()
    (copied / "src/app.py").write_text("sandbox modification")
    assert (root / "src/app.py").read_text() == "print('original')\n"
    assert Path(snapshot["manifest_path"]).exists()
    with pytest.raises(WorkspaceError, match="excluded"):
        io.read("ws", "instance", 1, ".env")


def test_snapshot_detects_external_modification(workspace, monkeypatch):
    io, root, _, _ = workspace
    real_read = io._read_bytes
    calls = 0

    def mutate(grant, path):
        nonlocal calls
        result = real_read(grant, path)
        calls += 1
        if calls == 1:
            (root / "src/app.py").write_text("user concurrently changed file")
        return result

    monkeypatch.setattr(io, "_read_bytes", mutate)
    with pytest.raises(WorkspaceError, match="changed during"):
        io.snapshot("ws", "instance", 1, "run-1")
    assert not list((io.storage_root / "snapshots").glob("*/manifest.json"))


def test_hardlink_is_rejected_by_read_list_patch(workspace):
    io, root, _, _ = workspace
    source = root / "src/app.py"
    os.link(source, root / "src/hard.py")
    for action in (
        lambda: io.read("ws", "instance", 1, "src/app.py"),
        lambda: io.list("ws", "instance", 1, "src"),
        lambda: io.patch("ws", "instance", 1, path="src/app.py", content="no",
                         expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                         operation_id="hard", run_id="run-1"),
    ):
        with pytest.raises(WorkspaceError):
            action()


def test_symlink_is_rejected(workspace):
    io, root, _, _ = workspace
    try:
        (root / "src/link.py").symlink_to(root / "src/app.py")
    except OSError:
        pytest.skip("Creating Windows symlinks requires an OS privilege; junction test remains separate.")
    with pytest.raises(WorkspaceError):
        io.read("ws", "instance", 1, "src/link.py")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_junction_rejected_in_operation_and_registration(workspace):
    io, root, registry, _ = workspace
    junction = root / "junction"
    env = {**os.environ, "OMNI_TEST_LINK": str(junction), "OMNI_TEST_TARGET": str(root / "src")}
    process = subprocess.run([
        "powershell", "-NoProfile", "-Command",
        "New-Item -ItemType Junction -Path $env:OMNI_TEST_LINK -Target $env:OMNI_TEST_TARGET | Out-Null",
    ], env=env, capture_output=True, check=False)
    assert process.returncode == 0
    try:
        with pytest.raises(WorkspaceError):
            io.read("ws", "instance", 1, "junction/app.py")
        registry.grants["ws"] = replace(registry.grants["ws"], root=str(junction))
        with pytest.raises(WorkspaceError):
            io.probe("ws", "instance", 1)
    finally:
        junction.rmdir()  # Remove only the link, never recursively enumerate its target.


def test_search_returns_source_versions(workspace):
    io, *_ = workspace
    result = io.search("ws", "instance", 1, "original")
    assert result["matches"][0]["path"] == "src/app.py"
    assert result["matches"][0]["line"] == 1
    assert result["matches"][0]["sha256"]


def test_broker_requires_independent_key_instance_and_narrow_requests(workspace):
    io, *_ = workspace
    client = TestClient(create_broker_app(io, instance_id="instance", service_key="test-only-key-" * 4))
    assert client.get("/health").status_code == 401
    headers = {"Authorization": "Bearer " + "test-only-key-" * 4, "X-Omni-Instance": "other"}
    assert client.get("/health", headers=headers).status_code == 403
    headers["X-Omni-Instance"] = "instance"
    assert client.get("/health", headers=headers).status_code == 200
    body = {"policy_version": 1, "path": "src/app.py"}
    response = client.post("/workspaces/ws/read", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["content"] == "print('original')\n"
    assert client.post("/workspaces/ws/read", json={**body, "root": "G:/"}, headers=headers).status_code == 422
    assert client.post("/workspaces/ws/execute", json={"command": "dir"}, headers=headers).status_code == 404
    assert client.post("/workspaces/ws/read", json={**body, "path": ".env"}, headers=headers).status_code == 403


@pytest.mark.skipif(os.name != "nt", reason="Native helper test")
def test_fixed_native_helper_protocol(workspace):
    io, root, registry, _ = workspace
    from dataclasses import asdict

    import omniharness.workspace_io as module

    request = {"grant": asdict(registry.grants["ws"]), "method": "read", "instance_id": "instance",
               "policy_version": 1, "arguments": {"path": "src/app.py"}, "storage_root": str(io.storage_root),
               "protected_roots": [str(io.protected_roots[0])]}
    result = subprocess.run([sys.executable, "-I", module.__file__, "--native-rpc"],
                            input=json.dumps(request).encode(), capture_output=True, check=True)
    assert json.loads(result.stdout)["content"] == (root / "src/app.py").read_text()


@pytest.mark.skipif(os.name != "nt" or not os.environ.get("OMNI_M4_WSL_DISTRO"), reason="Explicit WSL path probe")
def test_windows_wsl_same_object_probe(workspace):
    _, root, *_ = workspace
    script = Path(__file__).resolve().parents[1] / "scripts/workspace-path-probe.ps1"
    result = subprocess.run([
        "powershell", "-NoProfile", "-File", str(script), "-LiteralPath", str(root),
        "-WslDistro", os.environ["OMNI_M4_WSL_DISTRO"], "-ProbeWrite",
    ], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    payload = json.loads(result.stdout)
    assert payload["native_identity"]["filesystem"] == "NTFS"
    assert payload["wsl_same_object"] and payload["write_probe"]
    assert not list(root.glob(".omni-path-probe-*"))


@pytest.mark.skipif(os.name != "nt" or not os.environ.get("OMNI_M4_WSL_DISTRO"), reason="Explicit WSL helper roundtrip")
def test_wsl_native_adapter_reads_patches_and_exports_snapshot(workspace):
    import uuid
    from dataclasses import asdict

    io, root, registry, _ = workspace
    repository = Path(__file__).resolve().parents[1]
    script = io.storage_root / "wsl-roundtrip.py"
    staging = str(io.storage_root).replace("\\", "/")
    mapped_storage = "/mnt/" + staging[0].lower() + staging[2:]
    linux_snapshot_root = "/var/lib/omniharness/m4-path-probes/" + str(uuid.uuid4())
    source = '''
import json, sys
from pathlib import Path
sys.path.insert(0, '/mnt/g/harness/src')
from omniharness.workspace_io import DictWorkspaceRegistry, WorkspaceGrant, NativeWorkspaceIO
config = json.loads(sys.stdin.read())
io = NativeWorkspaceIO(DictWorkspaceRegistry([WorkspaceGrant(**config['grant'])]),
    python_executable='/mnt/g/harness/.venv/Scripts/python.exe',
    module_file=r'G:\\harness\\src\\omniharness\\workspace_io.py',
    native_storage_root=config['native_storage'], native_protected_roots=tuple(config['protected']),
    mapped_native_storage_root=Path(config['mapped_storage']),
    snapshot_root=Path(config['snapshot_root']), require_write_lease=lambda *args: None)
initial = io.read('ws', 'instance', 1, 'src/app.py')
updated = io.patch('ws', 'instance', 1, path='src/app.py', content='patched from WSL',
    expected_sha256=initial['sha256'], operation_id='wsl-op', run_id='run-1')
recovery = io.operation_result('ws', 'instance', 1, 'wsl-op', 'run-1')
snapshot = io.snapshot('ws', 'instance', 1, 'run-1')
assert Path(snapshot['workdir'], 'src/app.py').read_text() == 'patched from WSL'
assert Path(snapshot['workdir']).stat().st_uid == 10001
assert Path(snapshot['workdir'], 'src/app.py').stat().st_uid == 10001
Path(snapshot['workdir'], 'src/app.py').write_text('only snapshot changes')
assert io.read('ws', 'instance', 1, 'src/app.py')['content'] == 'patched from WSL'
print(json.dumps({'snapshot': snapshot, 'updated': updated, 'recovery': recovery}))
'''
    script.write_text(source, encoding="utf-8")
    linux_script = mapped_storage + "/wsl-roundtrip.py"
    config = {"grant": asdict(registry.grants["ws"]), "native_storage": str(io.storage_root),
              "mapped_storage": mapped_storage, "snapshot_root": linux_snapshot_root,
              "protected": [str(repository)]}
    # The dedicated sample lives under .omniharness for this test only. Protect
    # the separate framework fixture rather than authorizing the actual repo.
    config["protected"] = [str(io.protected_roots[0])]
    process = subprocess.run([
        "wsl", "-d", os.environ["OMNI_M4_WSL_DISTRO"], "--exec", "/usr/bin/python3", linux_script,
    ], input=json.dumps(config).encode(), capture_output=True, check=False)
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    result = json.loads(process.stdout)
    assert result["recovery"]["recorded_completed"]
    assert result["snapshot"]["workdir"].startswith(linux_snapshot_root)
    assert (root / "src/app.py").read_text() == "patched from WSL"
