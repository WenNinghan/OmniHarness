"""Narrow original-workspace IO. No command execution and no snapshot write-back.

Registry and lease checks are injected by the owning application. Windows uses
native handles; a WSL broker can invoke this same module through a fixed native
Python helper. The helper is a private stdin protocol, never a network service.
"""

from __future__ import annotations

import contextlib
import ctypes
import difflib
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Protocol


class WorkspaceError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


DEFAULT_EXCLUDES = (
    ".git", ".env", ".env.*", ".ssh", ".aws", ".azure", ".kube", ".gnupg",
    ".omniharness", ".codex", "secrets", "credentials", "credentials.*",
    "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
)
_RESERVED = re.compile(
    r"^(con|prn|aux|nul|conin\$|conout\$|com(?:[0-9]|[\u00b9\u00b2\u00b3])|lpt(?:[0-9]|[\u00b9\u00b2\u00b3]))$",
    re.I,
)


@dataclass(frozen=True)
class WorkspaceGrant:
    workspace_id: str
    instance_id: str
    root: str
    version: int = 1
    read_allow: tuple[str, ...] = (".",)
    write_allow: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    enabled: bool = True
    expected_identity: dict | None = None


class WorkspaceRegistry(Protocol):
    # Return this immutable policy version with the current revocation state.
    def get(self, workspace_id: str, policy_version: int) -> WorkspaceGrant | None: ...


class DictWorkspaceRegistry:
    """Explicit in-memory registry for tests/embedding; production supplies its store."""

    def __init__(self, grants: list[WorkspaceGrant]):
        self.grants = {grant.workspace_id: grant for grant in grants}
        self.versions = {(grant.workspace_id, grant.version): grant for grant in grants}

    def get(self, workspace_id: str, policy_version: int) -> WorkspaceGrant | None:
        current = self.grants.get(workspace_id)
        if not current:
            return None
        grant = current if current.version == policy_version else self.versions.get((workspace_id, policy_version))
        return replace(grant, enabled=current.enabled) if grant else None


def _fail(code: str, message: str) -> None:
    raise WorkspaceError(code, message)


def _relative(value: str, *, directory: bool = False) -> str:
    if directory and value in ("", "."):
        return "."
    # Also disallow Windows ADS, device paths and ambiguous trailing characters
    # when running on Linux, so a policy has identical meaning on both hosts.
    if not value or value.startswith(("/", "\\")) or "\\" in value:
        _fail("invalid_path", "Use a nonempty slash-separated relative path.")
    parts = value.split("/")
    if any(
        part in ("", ".", "..") or part.endswith((".", " "))
        or _RESERVED.match(part.split(".", 1)[0].rstrip(" "))
        or any(ord(c) < 32 or c in ':<>"|?*' for c in part)
        for part in parts
    ):
        _fail("invalid_path", "Ambiguous or escaping paths are not permitted.")
    return "/".join(parts)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        _fail("invalid_id", "Invalid operation or run identifier.")
    return value


def _denied(relative: str, grant: WorkspaceGrant) -> bool:
    lowered = relative.casefold()
    return any(
        fnmatch.fnmatchcase(part, pattern.casefold()) or fnmatch.fnmatchcase(lowered, pattern.casefold())
        for pattern in (*DEFAULT_EXCLUDES, *grant.excludes)
        for part in lowered.split("/")
    )


def _allowed(relative: str, prefixes: tuple[str, ...], *, directory: bool = False) -> bool:
    for raw in prefixes:
        prefix = _relative(raw, directory=True)
        if prefix == "." or relative == prefix or relative.startswith(prefix + "/"):
            return True
        if directory and (relative == "." or prefix.startswith(relative + "/")):
            return True
    return False


if os.name == "nt":
    from ctypes import wintypes

    class _FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
            ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD),
        ]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    _kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD,
    ]


@contextlib.contextmanager
def _native_handle(path: Path, *, write: bool = False, create: bool = False) -> Iterator[tuple[int, dict]]:
    """Keep ancestor handles open without DELETE sharing to prevent link swaps."""
    if os.name != "nt":
        _fail("unsupported", "Native Windows path inspection is unavailable.")
    access = 0x80000000 | (0x40000000 if write else 0)
    # Writes are exclusive. Ancestors deny WRITE and DELETE sharing: denying
    # deletion alone does not prevent setting a junction reparse point in place.
    share = 0 if write else 1
    handle = _kernel.CreateFileW(str(path), access, share, None, 1 if create else 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        _fail("file_busy" if write else "unsafe_path", "Cannot exclusively inspect the requested path.")
    try:
        info = _FileInfo()
        if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            _fail("unsafe_path", "Cannot inspect native file identity.")
        buffer = ctypes.create_unicode_buffer(32768)
        if not _kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0):
            _fail("unsafe_path", "Cannot resolve native file identity.")
        final = buffer.value
        if final.startswith("\\\\?\\"):
            final = final[4:]
        if info.attributes & 0x400 or Path(final) != path:
            _fail("unsafe_link", "Reparse points and aliased paths are not permitted.")
        directory = bool(info.attributes & 0x10)
        if not directory and info.links != 1:
            _fail("unsafe_link", "Hard-linked files are not permitted.")
        yield handle, {
            "path": str(path), "volume_serial": info.volume,
            "file_id": (info.index_high << 32) | info.index_low,
            "links": info.links, "directory": directory,
        }
    finally:
        _kernel.CloseHandle(handle)


def native_path_identity(path: str) -> dict:
    """Inspect a drive-backed NTFS object without accepting aliases or reparse ancestors."""
    candidate = Path(path)
    if os.name != "nt" or not re.match(r"^[a-zA-Z]:[\\/]", path) or path.startswith("\\"):
        _fail("unsupported_path", "Only local drive-backed NTFS paths are supported on Windows.")
    if any(part in (".", "..") or part.endswith((".", " ")) for part in path.replace("\\", "/")[3:].split("/")):
        _fail("invalid_path", "Ambiguous absolute paths are not permitted.")
    filesystem = ctypes.create_unicode_buffer(64)
    if not _kernel.GetVolumeInformationW(candidate.anchor, None, 0, None, None, None, filesystem, 64):
        _fail("unsupported_path", "Cannot inspect the volume.")
    if filesystem.value != "NTFS":
        _fail("unsupported_path", "Only tested local NTFS volumes are permitted.")
    with contextlib.ExitStack() as stack:
        last = {}
        for item in [*reversed(candidate.parents), candidate]:
            _, last = stack.enter_context(_native_handle(item))
        return {**last, "filesystem": filesystem.value}


class WorkspaceIO:
    def __init__(
        self, registry: WorkspaceRegistry, storage_root: Path, *, protected_roots: tuple[Path, ...],
        require_write_lease: Callable[[str, str, str], None] | None = None,
        max_file_bytes: int = 16 * 1024 * 1024, max_snapshot_bytes: int = 256 * 1024 * 1024,
        max_snapshot_files: int = 10000,
    ):
        self.registry = registry
        self.storage_root = Path(storage_root).absolute()
        self.protected_roots = tuple(Path(p).absolute() for p in (*protected_roots, self.storage_root))
        self.require_write_lease = require_write_lease
        self.max_file_bytes, self.max_snapshot_bytes = max_file_bytes, max_snapshot_bytes
        self.max_snapshot_files = max_snapshot_files
        self._locks: dict[str, threading.RLock] = {}
        self._lock_guard = threading.Lock()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        # Storage is configured by the host, never by a tool request.
        if self.storage_root.is_symlink() or self.storage_root.resolve() != self.storage_root:
            _fail("unsafe_storage", "Storage root must not be an alias.")

    def _grant(self, workspace_id: str, instance_id: str, policy_version: int) -> WorkspaceGrant:
        grant = self.registry.get(workspace_id, policy_version)
        if not grant or grant.instance_id != instance_id:
            _fail("not_found", "Workspace not found in this instance.")
        if not grant.enabled:
            _fail("workspace_disabled", "Workspace access has been revoked.")
        if grant.version != policy_version:
            _fail("policy_conflict", "The workspace grant version does not match.")
        root = Path(grant.root)
        if not root.is_absolute() or root == Path(root.anchor):
            _fail("unsafe_root", "A filesystem root cannot be registered as a workspace.")
        if any(_within(root, protected) for protected in self.protected_roots):
            _fail("protected_path", "Framework and secret storage cannot be a workspace.")
        if not root.is_dir():
            _fail("not_found", "Workspace directory does not exist.")
        if os.name == "nt":
            self._verify_root_identity(grant, native_path_identity(str(root)))
        elif str(root).startswith("/mnt/"):
            _fail("native_probe_required", "Windows workspaces require the native helper.")
        else:
            info = root.stat()
            self._verify_root_identity(grant, {"device": info.st_dev, "file_id": info.st_ino})
        return grant

    @staticmethod
    def _verify_root_identity(grant: WorkspaceGrant, actual: dict) -> None:
        expected = grant.expected_identity
        if expected is None:  # The paired registration probe obtains this first.
            return
        volume = "volume_serial" if os.name == "nt" else "device"
        if (not isinstance(expected, dict) or any(key not in expected for key in (volume, "file_id"))
                or any(expected[key] != actual.get(key) for key in (volume, "file_id"))):
            _fail("workspace_replaced", "The registered workspace directory has been replaced; register it again.")

    def _lock(self, workspace_id: str) -> threading.RLock:
        with self._lock_guard:
            return self._locks.setdefault(workspace_id, threading.RLock())

    def _authorize(self, grant: WorkspaceGrant, relative: str, *, write=False, directory=False) -> Path:
        relative = _relative(relative, directory=directory)
        if _denied(relative, grant):
            _fail("excluded_path", "This path is excluded by the workspace policy.")
        prefixes = grant.write_allow if write else grant.read_allow
        if not _allowed(relative, prefixes, directory=directory):
            _fail("path_not_granted", "This path is outside the granted file range.")
        target = Path(grant.root) / relative
        if any(_within(target, protected) for protected in self.protected_roots):
            _fail("protected_path", "Framework and secret storage is protected.")
        return target

    @contextlib.contextmanager
    def _parents(self, target: Path, grant: WorkspaceGrant | None = None) -> Iterator[int | None]:
        # Opening each ancestor prevents symlink traversal. POSIX dir_fd then
        # anchors final access to the opened parent even if a name is renamed.
        with contextlib.ExitStack() as stack:
            parent_fd = None
            if os.name == "nt":
                for parent in reversed(target.parents):
                    _, info = stack.enter_context(_native_handle(parent))
                    if grant is not None and parent == Path(grant.root):
                        self._verify_root_identity(grant, info)
            else:
                try:
                    parent_fd = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                except OSError:
                    _fail("unsafe_path", "The path is missing, busy, or cannot be accessed safely.")
                stack.callback(os.close, parent_fd)
                current = Path(target.anchor)
                for part in target.parent.parts[1:]:
                    try:
                        parent_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    except OSError:
                        _fail("unsafe_path", "The path is missing, busy, or cannot be accessed safely.")
                    stack.callback(os.close, parent_fd)
                    current /= part
                    if grant is not None and current == Path(grant.root):
                        info = os.fstat(parent_fd)
                        self._verify_root_identity(grant, {"device": info.st_dev, "file_id": info.st_ino})
            yield parent_fd

    @contextlib.contextmanager
    def _file(self, target: Path, *, write=False, create=False, grant=None) -> Iterator[object]:
        try:
            with self._parents(target, grant) as parent_fd:
                if os.name == "nt":
                    import msvcrt

                    with _native_handle(target, write=write, create=create) as (handle, info):
                        if info["directory"]:
                            _fail("not_file", "Expected a regular file.")
                        # Duplicate the owned handle so Python closing its fd
                        # never closes the original validation handle twice.
                        duplicate = wintypes.HANDLE()
                        _kernel.GetCurrentProcess.restype = wintypes.HANDLE
                        _kernel.DuplicateHandle.argtypes = [
                            wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                            ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
                        ]
                        process = _kernel.GetCurrentProcess()
                        if not _kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
                            _fail("unsafe_path", "Could not acquire file handle.")
                        fd = msvcrt.open_osfhandle(duplicate.value, os.O_RDWR if write else os.O_RDONLY)
                        with os.fdopen(fd, "r+b" if write else "rb") as stream:
                            yield stream
                else:
                    flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
                    if create:
                        flags |= os.O_CREAT | os.O_EXCL
                    fd = os.open(target.name, flags, 0o600, dir_fd=parent_fd)
                    with os.fdopen(fd, "r+b" if write else "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            _fail("unsafe_link", "Only regular non-hard-linked files are allowed.")
                        if write:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        yield stream
        except WorkspaceError:
            raise
        except (OSError, ValueError):
            _fail("unsafe_path", "The path is missing, busy, or cannot be accessed safely.")

    def _read_bytes(self, grant: WorkspaceGrant, relative: str) -> bytes:
        target = self._authorize(grant, relative)
        with self._file(target, grant=grant) as stream:
            content = stream.read(self.max_file_bytes + 1)
        if len(content) > self.max_file_bytes:
            _fail("file_too_large", "File exceeds the configured limit.")
        return content

    def probe(self, workspace_id: str, instance_id: str, policy_version: int) -> dict:
        grant = self._grant(workspace_id, instance_id, policy_version)
        root = Path(grant.root)
        with self._parents(root / "__probe__", grant):
            identity = native_path_identity(str(root)) if os.name == "nt" else {
                "device": root.stat().st_dev, "file_id": root.stat().st_ino, "filesystem": "posix",
            }
        return {"workspace_id": workspace_id, "policy_version": grant.version, "root": str(root),
                "identity": identity, "read_allow": grant.read_allow, "write_allow": grant.write_allow,
                "excludes": (*DEFAULT_EXCLUDES, *grant.excludes)}

    def read(self, workspace_id: str, instance_id: str, policy_version: int, path: str) -> dict:
        grant = self._grant(workspace_id, instance_id, policy_version)
        with self._lock(workspace_id):
            content = self._read_bytes(grant, path)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            _fail("not_text", "This file is not UTF-8 text; use a registered binary reader.")
        return {"path": path, "content": text, "sha256": _digest(content), "size": len(content)}

    def list(self, workspace_id: str, instance_id: str, policy_version: int, path: str = ".") -> dict:
        grant = self._grant(workspace_id, instance_id, policy_version)
        target = self._authorize(grant, path, directory=True)
        entries = []
        try:
            with self._parents(target / "__list__", grant) as directory_fd:
                with os.scandir(target if os.name == "nt" else directory_fd) as scan:
                    children = sorted(scan, key=lambda item: item.name.casefold())
                for entry in children:
                    item = target / entry.name
                    relative = item.relative_to(grant.root).as_posix()
                    if _denied(relative, grant) or any(_within(item, p) for p in self.protected_roots):
                        continue
                    # Windows DirEntry caches omit st_nlink/st_ino; query the
                    # object itself, then verify again through its native handle.
                    info = item.lstat() if os.name == "nt" else entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        _fail("unsafe_link", "A link was encountered in the workspace.")
                    directory = stat.S_ISDIR(info.st_mode)
                    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                        _fail("unsafe_link", "A nonregular or hard-linked file was encountered.")
                    if not _allowed(relative, grant.read_allow, directory=directory):
                        continue
                    if os.name == "nt":
                        native_path_identity(str(item))
                    entries.append({"path": relative, "kind": "directory" if directory else "file",
                                    "size": None if directory else info.st_size})
                    if len(entries) > self.max_snapshot_files:
                        _fail("too_many_files", "Directory listing exceeds the configured file limit.")
        except OSError:
            _fail("unsafe_path", "Cannot safely list this directory.")
        return {"path": path, "entries": entries}

    def _files(self, workspace_id: str, instance_id: str, policy_version: int) -> list[str]:
        pending, files = ["."], []
        visited = 0
        while pending:
            for item in self.list(workspace_id, instance_id, policy_version, pending.pop())["entries"]:
                visited += 1
                if visited > self.max_snapshot_files:
                    _fail("too_many_files", "Workspace exceeds the configured file limit.")
                (pending if item["kind"] == "directory" else files).append(item["path"])
        return sorted(files)

    def search(self, workspace_id: str, instance_id: str, policy_version: int, query: str, limit: int = 100) -> dict:
        if not query or len(query) > 1000 or not 1 <= limit <= 1000:
            _fail("invalid_search", "Search requires a literal query and a bounded result limit.")
        matches = []
        for path in self._files(workspace_id, instance_id, policy_version):
            try:
                result = self.read(workspace_id, instance_id, policy_version, path)
            except WorkspaceError as exc:
                if exc.code in ("not_text", "file_too_large"):
                    continue
                raise
            for number, line in enumerate(result["content"].splitlines(), 1):
                if query in line:
                    matches.append({"path": path, "line": number, "text": line[:2000], "sha256": result["sha256"]})
                    if len(matches) == limit:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def patch(
        self, workspace_id: str, instance_id: str, policy_version: int, *, path: str,
        content: str, expected_sha256: str | None, operation_id: str, run_id: str,
    ) -> dict:
        """Replace one UTF-8 file, or create if expected_sha256 is None.

        Existing parents must already exist. There is deliberately no delete or
        mkdir interface. Recovery material is durable before the original write.
        The host ledger owns retries: an existing operation record is not rerun.
        """
        grant = self._grant(workspace_id, instance_id, policy_version)
        if self.require_write_lease is None:
            _fail("lease_required", "A workspace write lease is required.")
        _identifier(operation_id)
        _identifier(run_id)
        if expected_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            _fail("invalid_version", "Expected version must be a SHA-256 digest or null for a new file.")
        payload = content.encode("utf-8")
        if len(payload) > self.max_file_bytes:
            _fail("file_too_large", "Replacement exceeds the configured file limit.")
        target = self._authorize(grant, path, write=True)
        # A write never grants implicit access to a read-excluded original.
        self._authorize(grant, path)
        with self._lock(workspace_id):
            self.require_write_lease(instance_id, workspace_id, run_id)
            record_dir = self.storage_root / "recovery" / operation_id
            if record_dir.exists():
                _fail("operation_exists", "Operation already has recovery material; consult its ledger before retry.")
            creating = expected_sha256 is None
            if creating and os.path.lexists(target):
                _fail("version_conflict", "The new-file target already exists.")
            # The parent chain stays pinned during recovery and the write.
            with self._parents(target, grant):
                if creating:
                    original = b""
                    record = self._recovery(record_dir, grant, path, original, None, payload, run_id)
                    with self._file(target, write=True, create=True, grant=grant) as stream:
                        self._write(stream, payload)
                else:
                    with self._file(target, write=True, grant=grant) as stream:
                        original = stream.read(self.max_file_bytes + 1)
                        if len(original) > self.max_file_bytes or _digest(original) != expected_sha256:
                            _fail("version_conflict", "File changed since it was read; the patch was not applied.")
                        record = self._recovery(record_dir, grant, path, original, expected_sha256, payload, run_id)
                        self._write(stream, payload)
                record["state"] = "completed"
                try:
                    self._save_json(record_dir / "result.json", record)
                except OSError:
                    _fail("write_result_unknown", "File may have changed; reconcile its recovery record before retry.")
            return {**record, "recovery_id": operation_id, "diff": "".join(difflib.unified_diff(
                original.decode("utf-8", errors="replace").splitlines(keepends=True),
                content.splitlines(keepends=True), fromfile=path, tofile=path,
            ))}

    @staticmethod
    def _write(stream, payload: bytes) -> None:
        try:
            stream.seek(0)
            stream.write(payload)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        except OSError:
            _fail("write_result_unknown", "File may have changed; reconcile its recovery record before retry.")

    @staticmethod
    def _save_json(path: Path, data: dict) -> None:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())

    def _recovery(self, directory, grant, path, original, before, payload, run_id) -> dict:
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            _fail("operation_exists", "Operation already has recovery material; consult its ledger before retry.")
        with (directory / "before.bin").open("xb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        record = {"workspace_id": grant.workspace_id, "instance_id": grant.instance_id,
                  "policy_version": grant.version, "run_id": run_id,
                  "path": path, "before_sha256": before, "sha256": _digest(payload), "size": len(payload),
                  "state": "prepared"}
        self._save_json(directory / "prepared.json", record)
        return record

    def operation_result(
        self, workspace_id: str, instance_id: str, policy_version: int, operation_id: str, run_id: str,
    ) -> dict:
        """Read evidence after a lost response; never repeat a patch automatically."""
        grant = self._grant(workspace_id, instance_id, policy_version)
        directory = self.storage_root / "recovery" / _identifier(operation_id)
        try:
            record = json.loads((directory / "prepared.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _fail("not_found", "No complete recovery record exists for this operation.")
        if (record.get("workspace_id"), record.get("instance_id"), record.get("run_id"),
                record.get("policy_version")) != (workspace_id, instance_id, run_id, policy_version):
            _fail("not_found", "No recovery record exists in this run and workspace.")
        target = self._authorize(grant, record["path"])
        current_sha256 = None
        if os.path.lexists(target):
            current_sha256 = _digest(self._read_bytes(grant, record["path"]))
        # A crash can leave a partial result.json. File existence alone is not
        # a completed receipt and must never release an unknown write's lease.
        try:
            completed = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            completed = None
        recorded_completed = completed == {**record, "state": "completed"}
        return {**record, "recovery_id": operation_id, "recorded_completed": recorded_completed,
                "current_sha256": current_sha256, "matches_expected_result": current_sha256 == record["sha256"],
                "state": "completed" if recorded_completed else "uncertain"}

    def snapshot(self, workspace_id: str, instance_id: str, policy_version: int, run_id: str) -> dict:
        grant = self._grant(workspace_id, instance_id, policy_version)
        _identifier(run_id)
        snapshot_id = str(uuid.uuid4())
        destination = self.storage_root / "snapshots" / snapshot_id
        workdir = destination / "workspace"
        files, total = [], 0
        with self._lock(workspace_id):
            paths = self._files(workspace_id, instance_id, policy_version)
            workdir.mkdir(parents=True, exist_ok=False)
            # Failed snapshots remain evidence, without a ready manifest.
            for path in paths:
                content = self._read_bytes(grant, path)
                total += len(content)
                if total > self.max_snapshot_bytes:
                    _fail("snapshot_too_large", "Snapshot exceeds the configured byte limit.")
                output = workdir / path
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(content)
                files.append({"path": path, "sha256": _digest(content), "size": len(content)})
            if paths != self._files(workspace_id, instance_id, policy_version):
                _fail("version_conflict", "Workspace file list changed during snapshot creation.")
            for item in files:
                if _digest(self._read_bytes(grant, item["path"])) != item["sha256"]:
                    _fail("version_conflict", "Workspace changed during snapshot creation.")
            manifest = {"schema_version": 1, "snapshot_id": snapshot_id, "workspace_id": workspace_id,
                        "policy_version": policy_version, "run_id": run_id, "files": files,
                        "total_bytes": total, "source_kind": "filtered_workspace_copy"}
            self._save_json(destination / "manifest.json", manifest)
        return {**manifest, "workdir": str(workdir), "manifest_path": str(destination / "manifest.json")}


class NativeWorkspaceIO:
    """WSL adapter to a host-configured Windows Python module, never model commands.

    The native runtime/root paths are trusted startup settings. Only the WSL
    registry supplies grants. A native helper is spawned with scrubbed env and
    JSON on stdin; the network broker does not expose helper configuration.
    """

    def __init__(
        self, registry: WorkspaceRegistry, *, python_executable: str, module_file: str,
        native_storage_root: str, native_protected_roots: tuple[str, ...],
        mapped_native_storage_root: Path, snapshot_root: Path,
        require_write_lease: Callable[[str, str, str], None],
    ):
        self.registry, self.python_executable, self.module_file = registry, python_executable, module_file
        self.native_storage_root, self.native_protected_roots = native_storage_root, native_protected_roots
        self.mapped_native_storage_root, self.snapshot_root = mapped_native_storage_root, snapshot_root
        self.require_write_lease = require_write_lease

    def _call(self, method: str, workspace_id: str, instance_id: str, policy_version: int, **arguments) -> dict:
        grant = self.registry.get(workspace_id, policy_version)
        if not grant or grant.instance_id != instance_id:
            _fail("not_found", "Workspace not found in this instance.")
        if method == "patch":
            self.require_write_lease(instance_id, workspace_id, arguments["run_id"])
        request = {"grant": asdict(grant), "method": method, "instance_id": instance_id,
                   "policy_version": policy_version, "arguments": arguments,
                   "storage_root": self.native_storage_root, "protected_roots": self.native_protected_roots}
        try:
            process = subprocess.run(
                [self.python_executable, "-I", self.module_file, "--native-rpc"],
                input=json.dumps(request), capture_output=True, text=True, encoding="utf-8", timeout=180,
                env={name: os.environ[name] for name in ("SystemRoot", "WINDIR", "PATH", "WSL_INTEROP")
                     if name in os.environ}, check=False,
            )
            result = json.loads(process.stdout)
            if not isinstance(result, dict):
                raise ValueError("Invalid native result")
        except subprocess.TimeoutExpired:
            _fail("native_result_unknown" if method == "patch" else "native_helper_unavailable",
                  "Native helper timed out; a write must be reconciled, never automatically replayed.")
        except (OSError, ValueError):
            _fail("native_result_unknown" if method == "patch" else "native_helper_unavailable",
                  "Native workspace helper failed; reconcile any dispatched write before retry.")
        if "error" in result:
            raise WorkspaceError(result["error"]["code"], result["error"]["message"])
        if process.returncode:
            _fail("native_result_unknown" if method == "patch" else "native_helper_unavailable",
                  "Native workspace helper failed; reconcile any dispatched write before retry.")
        if method == "patch" and (
            result.get("state") != "completed"
            or result.get("workspace_id") != workspace_id
            or result.get("instance_id") != instance_id
            or result.get("policy_version") != policy_version
            or result.get("run_id") != arguments["run_id"]
            or result.get("path") != arguments["path"]
            or result.get("sha256") != _digest(arguments["content"].encode("utf-8"))
        ):
            _fail("native_result_unknown", "Native write evidence is incomplete; reconcile before retry.")
        return result

    def probe(self, workspace_id, instance_id, policy_version):
        return self._call("probe", workspace_id, instance_id, policy_version)

    def read(self, workspace_id, instance_id, policy_version, path):
        return self._call("read", workspace_id, instance_id, policy_version, path=path)

    def list(self, workspace_id, instance_id, policy_version, path="."):
        return self._call("list", workspace_id, instance_id, policy_version, path=path)

    def search(self, workspace_id, instance_id, policy_version, query, limit=100):
        return self._call("search", workspace_id, instance_id, policy_version, query=query, limit=limit)

    def patch(self, workspace_id, instance_id, policy_version, **arguments):
        return self._call("patch", workspace_id, instance_id, policy_version, **arguments)

    def operation_result(self, workspace_id, instance_id, policy_version, operation_id, run_id):
        return self._call("operation_result", workspace_id, instance_id, policy_version,
                          operation_id=operation_id, run_id=run_id)

    def snapshot(self, workspace_id, instance_id, policy_version, run_id):
        result = self._call("snapshot", workspace_id, instance_id, policy_version, run_id=run_id)
        # Trust the pinned staging root, not a path returned by the subprocess.
        snapshot_id = _identifier(result["snapshot_id"])
        source = self.mapped_native_storage_root / "snapshots" / snapshot_id / "workspace"
        destination = self.snapshot_root / snapshot_id
        destination.mkdir(parents=True, exist_ok=False)
        workdir = destination / "workspace"
        workdir.mkdir()
        for item in result["files"]:
            path = _relative(item["path"])
            src, dst = source / path, workdir / path
            if src.is_symlink() or src.resolve() != src or not src.is_file() or src.stat().st_nlink != 1:
                _fail("unsafe_snapshot", "Snapshot staging contains an unsafe path.")
            payload = src.read_bytes()
            if _digest(payload) != item["sha256"]:
                _fail("version_conflict", "Snapshot staging changed during transfer.")
            dst.parent.mkdir(parents=True, exist_ok=True)
            with dst.open("xb") as stream:
                stream.write(payload)
        result.update(workdir=str(workdir), manifest_path=str(destination / "manifest.json"))
        WorkspaceIO._save_json(destination / "manifest.json", result)
        # Only the exported copy is writable by the fixed sandbox workload.
        # Recovery and manifest files remain outside the tool mount, root-owned.
        # The production WSL broker owns this private root and runs as root;
        # never request CHOWN capability inside the tool container instead.
        try:
            for directory, _names, filenames in os.walk(workdir, followlinks=False):
                for name in filenames:
                    os.chown(Path(directory) / name, 10001, 10001, follow_symlinks=False)
                os.chown(directory, 10001, 10001, follow_symlinks=False)
        except (AttributeError, OSError):
            _fail("snapshot_unavailable", "The snapshot cannot be assigned to the non-root workload.")
        return result


def _native_rpc() -> None:
    if os.name != "nt":
        _fail("unsupported", "The native helper must run on Windows.")
    request = json.loads(sys.stdin.buffer.read(32 * 1024 * 1024).decode("utf-8"))
    method = request["method"]
    if method not in ("probe", "read", "list", "search", "patch", "snapshot", "operation_result"):
        _fail("unknown_method", "This helper only supports workspace file operations.")
    grant = WorkspaceGrant(**request["grant"])
    core = WorkspaceIO(
        DictWorkspaceRegistry([grant]), Path(request["storage_root"]),
        protected_roots=tuple(Path(p) for p in request["protected_roots"]),
        # The authenticated parent already checked the authoritative lease.
        require_write_lease=lambda *_: None,
    )
    result = getattr(core, method)(
        grant.workspace_id, request["instance_id"], request["policy_version"], **request["arguments"],
    )
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--native-rpc"]:
            _native_rpc()
        elif len(sys.argv) == 3 and sys.argv[1] == "--inspect-path":
            sys.stdout.buffer.write(json.dumps(native_path_identity(sys.argv[2]), ensure_ascii=False).encode("utf-8"))
        else:
            sys.exit("Only private native IO or path inspection is supported.")
    except WorkspaceError as exc:
        sys.stdout.buffer.write(json.dumps({"error": {"code": exc.code, "message": exc.message}}).encode())
        sys.exit(1)
