"""A create-only patch must not overwrite a concurrent creator's file."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from omniharness.workspace_io import DictWorkspaceRegistry, WorkspaceError, WorkspaceGrant, WorkspaceIO


@pytest.mark.parametrize("round_number", range(10))
def test_parallel_create_only_patches_never_replace_winner(tmp_path, round_number):
    root = tmp_path / "中文 空格项目"
    root.mkdir()
    (root / "src").mkdir()
    registry = DictWorkspaceRegistry([WorkspaceGrant("ws", "instance", str(root), write_allow=("src",))])
    io = WorkspaceIO(registry, tmp_path / "storage", protected_roots=(), require_write_lease=lambda *_: None)
    start = Barrier(4)

    def create(index):
        start.wait()
        try:
            return io.patch("ws", "instance", 1, path="src/created.txt", content=f"creator-{index}",
                            expected_sha256=None, operation_id=f"op-{round_number}-{index}", run_id="run")
        except WorkspaceError as exc:
            return {"error": exc.code}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))

    assert sum(result.get("state") == "completed" for result in results) == 1
    assert sum("error" in result for result in results) == 3
    winner = next(result for result in results if result.get("state") == "completed")
    assert hashlib.sha256((root / "src/created.txt").read_bytes()).hexdigest() == winner["sha256"]


def test_external_creator_between_precheck_and_open_is_not_overwritten(tmp_path, monkeypatch):
    root = tmp_path / "中文 空格项目"
    (root / "src").mkdir(parents=True)
    registry = DictWorkspaceRegistry([WorkspaceGrant("ws", "instance", str(root), write_allow=("src",))])
    io = WorkspaceIO(registry, tmp_path / "storage", protected_roots=(), require_write_lease=lambda *_: None)
    recovery = io._recovery

    def competing_creator(*args):
        result = recovery(*args)
        (root / "src/created.txt").write_text("external", encoding="utf-8")
        return result

    monkeypatch.setattr(io, "_recovery", competing_creator)
    with pytest.raises(WorkspaceError):
        io.patch("ws", "instance", 1, path="src/created.txt", content="agent",
                 expected_sha256=None, operation_id="competing-create", run_id="run")
    assert (root / "src/created.txt").read_text(encoding="utf-8") == "external"
