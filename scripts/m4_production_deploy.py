"""Reviewed local WSL deployment: inspect/backup/restore-test before explicit cutover.

No model credentials or expanded Compose configuration are printed. Backups and
restore tests never modify the live database. `backup-verify` quiesces services;
`update` and `cutover` are explicit release actions.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from omniharness.legacy_tool_retirement import LegacyMaintenanceScope

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".omniharness/m4-production"
SECRET = ROOT / ".omniharness/secrets"
LINUX = "/var/lib/omniharness-m4-production"
DISTRO = "OmniHarnessCheckAlt"
PROJECT = "omniharness"
INSTANCE = "omniharness-production"
TOOL = "sha256:79950f88eb5387086a7089357e293055b8beb906da9cb728961b6e88d30cc64c"
EXECD = "sha256:6b5a76c64ef66f500d38e068b1a50d68eb2f7fbc6d18cddbce525d86faeca0e1"
EGRESS = "sha256:cad71a020c0990470d4ebe12eb01fc4e6e12afb431d67ae6b655626f7539b8e3"
INVENTORY = '''import hashlib,json
from sqlalchemy import text
from omniharness.settings import Settings
from omniharness.db import make_engine
engine=make_engine(Settings())
with engine.connect() as db:
 db.execute(text("SET TRANSACTION READ ONLY"))
 db.execute(text("SET TIME ZONE 'UTC'"))
 tables={}
 names=list(db.execute(text("select schemaname,tablename from pg_tables where schemaname not like 'pg_%' and schemaname <> 'information_schema' order by 1,2")))
 quote=engine.dialect.identifier_preparer.quote_identifier
 for schema,table in names:
  rows=list(db.execute(text("select to_jsonb(t) from "+quote(schema)+"."+quote(table)+" t")).scalars())
  def digest(values):
   encoded=sorted(json.dumps(row,sort_keys=True,separators=(",",":"),default=str) for row in values)
   return hashlib.sha256(json.dumps(encoded).encode()).hexdigest()
  legacy=[{k:v for k,v in row.items() if k not in ('context_policy','workspace_id')} for row in rows]
  tables[schema+'.'+table]={"count":len(rows),"sha256":digest(rows),"legacy_sha256":digest(legacy)}
 tool_table=db.scalar(text("select to_regclass('public.tool_operations')"))
 unsafe_tools=db.scalar(text("select count(*) from tool_operations where slot_claimed or status in ('running','result_unknown','uncertain','executing','awaiting_approval','approved')")) if tool_table else 0
 uncertain_operations=db.scalar(text("select count(*) from operation_records where status in ('running','uncertain','result_unknown')"))
 value={"revision":db.scalar(text("select version_num from alembic_version")),"tables":tables,
  "active_runs":db.scalar(text("select count(*) from runs where status not in ('completed','failed','cancelled','interrupted')")),
  "unsafe_tools":unsafe_tools,"uncertain_operations":uncertain_operations,
  "sandbox_ids":list(db.execute(text("select distinct sandbox_id from tool_operations where sandbox_id is not null order by sandbox_id")).scalars()) if tool_table else [],
  "output_limits":[dict(row) for row in db.execute(text("select max_output_tokens,count(*) as count from model_configs group by max_output_tokens order by max_output_tokens")).mappings()]}
 if unsafe_tools:
  value['unsafe_tool_ids']=list(db.execute(text("select id from tool_operations where slot_claimed or status in ('running','result_unknown','uncertain','executing','awaiting_approval','approved') order by id")).scalars())
 if uncertain_operations:
  value['unsafe_operation_ids']=list(db.execute(text("select operation_id from operation_records where status in ('running','uncertain','result_unknown') order by operation_id")).scalars())
 creation_bound=db.scalar(text("select count(*) from runs where config_snapshot->'workspace'->'creation_service' is not null"))
 if creation_bound or getattr(Settings(),'sandbox_creation_url',''):
  value['external_creation_storage']=True
 print(json.dumps(value))
engine.dispose()
'''
LINUX_TREE = '''import hashlib,json,os,pathlib,stat,sys,tarfile
root=pathlib.Path(sys.argv[1]); result={}
if not root.is_dir() or root.is_symlink(): raise ValueError('Invalid backup root')
for p in [root,*sorted(root.rglob('*'))]:
 s=p.lstat(); rel=p.relative_to(root).as_posix()
 if stat.S_ISDIR(s.st_mode): item={'kind':'directory'}
 elif stat.S_ISREG(s.st_mode) and s.st_nlink==1:
  h=hashlib.sha256()
  with p.open('rb') as f:
   for block in iter(lambda:f.read(1048576),b''): h.update(block)
  item={'kind':'file','size':s.st_size,'sha256':h.hexdigest()}
 else: raise ValueError('Nonregular backup source')
 result[rel]={**item,'mode':stat.S_IMODE(s.st_mode),'uid':s.st_uid,'gid':s.st_gid}
if len(sys.argv)>2:
 with tarfile.open(sys.argv[2],'w') as archive:
  for rel in result: archive.add(root/rel,arcname=rel,recursive=False)
print(json.dumps(result,sort_keys=True))
'''
VERIFY_KEYS = '''import json
from sqlalchemy import text
from omniharness.settings import Settings
from omniharness.db import make_engine
from omniharness.security import SecretStore
settings=Settings(); store=SecretStore(settings); engine=make_engine(settings)
with engine.connect() as db:
 db.execute(text("SET TRANSACTION READ ONLY"))
 values=list(db.execute(text("select encrypted_api_key from providers where encrypted_api_key is not null")).scalars())
 assert all(store.decrypt(value) for value in values)
 print(json.dumps({"credentials_readable":len(values)}))
engine.dispose()
'''
VERIFY_0010 = '''from sqlalchemy import inspect
from omniharness.settings import Settings
from omniharness.db import make_engine
engine=make_engine(Settings()); inspector=inspect(engine)
columns={c['name'] for c in inspector.get_columns('clarifications')}
expected={'id','run_id','request_key','task_id','stage','question','reason','options','status',
 'answer','client_request_id','created_at','answered_at'}
assert columns==expected
assert inspector.get_pk_constraint('clarifications')['constrained_columns']==['id']
assert any(f['constrained_columns']==['run_id'] and f['referred_table']=='runs'
 for f in inspector.get_foreign_keys('clarifications'))
unique={tuple(c['column_names']) for c in inspector.get_unique_constraints('clarifications')}
assert {('run_id','request_key'),('run_id','client_request_id')} <= unique
assert any(i['name']=='ix_clarifications_run_id' and i['column_names']==['run_id']
 for i in inspector.get_indexes('clarifications'))
engine.dispose()
'''


def mapped(path: Path) -> str:
    path = path.resolve()
    if path.drive.lower() != "g:":
        raise ValueError("This tested local profile requires its existing G: checkout")
    return "/mnt/g/" + path.as_posix()[3:]


def wsl(*args, data=None, timeout=300):
    result = subprocess.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--exec", *args],
                            input=data, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"Private deployment command failed: {args[0]} (exit {result.returncode})")
    return result.stdout


def docker(*args, **kwargs):
    return wsl("docker", *args, **kwargs)


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    account = subprocess.check_output(["whoami"], encoding="utf-8").strip()
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"],
                   capture_output=True, check=True)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def deployment_lock():
    """Share the existing PowerShell project's ownership/mutation mutex."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateMutexW(None, False, "Local\\OmniHarness-Compose-" + PROJECT)
    if not handle:
        raise RuntimeError("Could not open the existing deployment ownership mutex")
    held = False
    try:
        held = kernel.WaitForSingleObject(handle, 60000) in (0, 0x80)
        if not held:
            raise RuntimeError("Another deployment command owns this Compose project")
        yield
    finally:
        if held:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def persist_profile(content):
    target = ROOT / ".omniharness/deployment.env"
    temporary = target.with_suffix(".pending")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, target)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def image_id(value):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError("An inspected immutable local application image ID is required")
    actual = docker("image", "inspect", value, "--format", "{{.Id}}").decode().strip()
    if actual != value:
        raise ValueError("Application image differs from the requested immutable ID")
    return value


def ownership():
    rows = []
    for identity in docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}").decode().split():
        obj = json.loads(docker("inspect", identity))[0]
        labels = obj["Config"]["Labels"]
        if (labels.get("com.docker.compose.project.working_dir") != mapped(ROOT)
                or labels.get("com.docker.compose.project.config_files") != mapped(ROOT / "compose.yaml")):
            raise ValueError("The production Compose name belongs to another checkout")
        if obj["Name"].lstrip("/") != f"{PROJECT}-{labels.get('com.docker.compose.service')}-1":
            raise ValueError("Production service/container identity differs from this single-instance profile")
        rows.append({"id": obj["Id"], "service": labels["com.docker.compose.service"],
                     "name": obj["Name"].lstrip("/"), "image": obj["Image"], "state": obj["State"]["Status"]})
    by_service = {row["service"]: row for row in rows}
    if len(by_service) != len(rows):
        raise ValueError("Duplicate production services exceed this single-worker deployment profile")
    if not {"api", "worker", "postgres"}.issubset(by_service):
        raise ValueError("Existing production containers are required for this upgrade profile")
    return by_service


def inventory(container="omniharness-api-1"):
    return json.loads(docker("exec", container, "python", "-c", INVENTORY))


def stopped_inventory(owners):
    """Read the DB with the original image after ingress and Worker stop."""
    obj = json.loads(docker("inspect", owners["api"]["id"]))[0]
    connection = next(value for value in obj["Config"]["Env"] if value.startswith("OMNI_DATABASE_URL="))
    private_directory(PRIVATE / "secrets")
    env_file = PRIVATE / "secrets" / ("inventory-" + secrets.token_hex(5) + ".env")
    env_file.write_text(connection + "\nOMNI_DATA_DIR=/tmp/data\n", encoding="utf-8")
    try:
        return json.loads(docker("run", "--rm", "--network", PROJECT + "_default", "--env-file", mapped(env_file),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", owners["api"]["image"],
            "python", "-c", INVENTORY))
    finally:
        env_file.unlink(missing_ok=True)


def compose(*args, candidate=False, profile_file=None):
    arguments = ["compose", "--project-directory", mapped(ROOT), "-f", mapped(ROOT / "compose.yaml"),
                 "--env-file", mapped(ROOT / ".env"), "--env-file", mapped(SECRET / "compose.env")]
    profile = profile_file or (PRIVATE / "candidate.env" if candidate else ROOT / ".omniharness/deployment.env")
    if profile.exists():
        arguments.extend(["--env-file", mapped(profile)])
    return docker(*arguments, *args)


def file_manifest(folder):
    if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
        raise ValueError("Backup source must be an existing regular directory")
    result = {}
    for parent, directories, files in os.walk(folder, followlinks=False):
        for name in sorted([*directories, *files]):
            path = Path(parent) / name
            if path.is_symlink() or path.is_junction():
                raise ValueError("Backup sources must not contain links or junctions")
            if path.is_file():
                with path.open("rb") as stream:
                    result[path.relative_to(folder).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def directory_manifest(folder):
    file_manifest(folder)  # Validate links before walking the same owned tree.
    return sorted(Path(parent).relative_to(folder).as_posix()
                  for parent, _, _ in os.walk(folder, followlinks=False))


def require_supported_creation_storage(value):
    if value.get("external_creation_storage"):
        raise ValueError("Independent creation storage requires its own verified backup/restore profile; "
                         "this release tool must not drop the witness or the frozen service binding")


def require_quiet(value, *, maintenance_scope=None):
    require_supported_creation_storage(value)
    if maintenance_scope is not None:
        if not isinstance(maintenance_scope, LegacyMaintenanceScope) or maintenance_scope.instance_id != INSTANCE:
            raise ValueError("Unknown legacy maintenance scope")
        maintenance_scope.validate_inventory(value)
        return
    if any(value.get(key, 0) for key in ("active_runs", "unsafe_tools", "uncertain_operations")):
        raise ValueError("Active Runs, uncertain operations or occupied tool slots prevent a consistent release backup")


def owned_sandboxes(value=None):
    # Exact instance metadata, independent of the controller's health/state.
    found = docker("ps", "-aq", "--filter", "label=omni_instance=" + INSTANCE).decode().split()
    for sandbox_id in (value or {}).get("sandbox_ids", []):
        found.extend(docker("ps", "-aq", "--filter", "label=opensandbox.io/egress-sidecar-for=" + sandbox_id).decode().split())
    return sorted(set(found))


def quiesce(*, maintenance_scope=None):
    owners = ownership()
    value = inventory() if owners["api"]["state"] == "running" else stopped_inventory(owners)
    require_quiet(value, maintenance_scope=maintenance_scope)
    if owned_sandboxes(value):
        raise ValueError("Owned sandbox containers still exist; reconcile them before release")
    docker("stop", "--time", "30", owners["api"]["name"], owners["worker"]["name"])
    # Recheck after removing ingress: a racing request must abort, not enter backup.
    require_quiet(stopped_inventory(owners), maintenance_scope=maintenance_scope)
    broker_stop()
    if "sandbox-controller" in owners:
        docker("stop", "--time", "30", owners["sandbox-controller"]["name"])
    return owners


def resume_owned(owners):
    if "sandbox-controller" in owners and owners["sandbox-controller"]["state"] == "running":
        docker("start", owners["sandbox-controller"]["name"])
        broker_start()
    for service in ("api", "worker"):
        if owners[service]["state"] == "running":
            docker("start", owners[service]["name"])


def mounted_paths(owners):
    obj = json.loads(docker("inspect", owners["api"]["id"]))[0]
    mounts = {entry["Destination"]: entry["Source"] for entry in obj["Mounts"] if entry["Type"] == "bind"}
    result = {}
    for target, name in (("/data", "data"), ("/secrets", "secrets")):
        source = mounts.get(target, "")
        if not source.startswith("/mnt/g/"):
            raise ValueError("Application backup mount is outside the tested G: profile")
        path = Path("G:/" + source.removeprefix("/mnt/g/")).resolve()
        defaults = {"data": ROOT / ".omniharness/data", "secrets": SECRET}
        if path != defaults[name].resolve() and PRIVATE.resolve() / "restores" not in path.parents:
            raise ValueError("Application backup mount has unknown ownership")
        result[name] = path
    return result


def active_database(owners):
    obj = json.loads(docker("inspect", owners["api"]["id"]))[0]
    value = next(entry.split("=", 1)[1] for entry in obj["Config"]["Env"] if entry.startswith("OMNI_DATABASE_URL="))
    name = urlsplit(value).path.lstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError("Unsupported production database name")
    return name


def archive_inventory(path):
    result = {}
    with tarfile.open(path, "r:") as archive:
        for item in archive:
            name = item.name.rstrip("/") or "."
            if (name.startswith("/") or ".." in name.split("/") or "\\" in name or name in result
                    or not (item.isfile() or item.isdir())):
                raise ValueError("State archive contains an unsafe or duplicate member")
            value = {"kind": "directory" if item.isdir() else "file", "mode": item.mode,
                     "uid": item.uid, "gid": item.gid}
            if item.isfile():
                stream = archive.extractfile(item)
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                value.update(size=item.size, sha256=digest)
            result[name] = value
    return result


def copy_checked(source, target):
    original = file_manifest(source)
    shutil.copytree(source, target)
    if file_manifest(source) != original or file_manifest(target) != original:
        raise ValueError("Files changed during backup; retain evidence and take a fresh backup")


def migration_preserved(before, after):
    if before["revision"] == after["revision"] and before["revision"] in {"0009", "0010"}:
        return before == after
    if before["revision"] == "0009" and after["revision"] == "0010":
        added = set(after["tables"]) - set(before["tables"])
        return (added == {"public.clarifications"} and after["tables"]["public.clarifications"]["count"] == 0
                and all(after["tables"].get(name) == value for name, value in before["tables"].items()
                        if name != "public.alembic_version")
                and before["output_limits"] == after["output_limits"]
                and all(before.get(key) == after.get(key) for key in
                        ("active_runs", "unsafe_tools", "uncertain_operations", "sandbox_ids")))
    if before["revision"] != "0008" or after["revision"] != "0009":
        return False
    for table, value in before["tables"].items():
        if table == "public.alembic_version":
            continue
        current = after["tables"].get(table, {})
        if value["count"] != current.get("count") or value["legacy_sha256"] != current.get("legacy_sha256"):
            return False
    if any(value["count"] != 0 for table, value in after["tables"].items() if table not in before["tables"]):
        return False
    return before["output_limits"] == after["output_limits"]


def inventory_matches(expected, actual):
    if all("." in name for name in expected["tables"]):
        return expected == actual
    # Read-only compatibility with the original 0008 backup manifests.
    if expected["revision"] != "0008" or actual["revision"] != "0008":
        return False
    return (expected["output_limits"] == actual["output_limits"]
        and all(actual["tables"].get("public." + name, {}).get("legacy_sha256") == table["sha256"]
                and actual["tables"]["public." + name]["count"] == table["count"]
                for name, table in expected["tables"].items()))


def restore_linux_archive(directory, name, destination):
    if name not in {"controller", "snapshots"} or not destination.startswith(LINUX + "/"):
        raise ValueError("Unknown Linux restore destination")
    # Validate every member before invoking tar as the deployment owner.
    archive_inventory(directory / (name + ".tar"))
    wsl("python3", "-c", "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "root=pathlib.Path(sys.argv[2]); assert p.resolve().is_relative_to(root.resolve()); "
        "assert not p.exists(); p.mkdir(parents=True)", destination, LINUX)
    wsl("tar", "--numeric-owner", "-xf", mapped(directory / (name + ".tar")), "-C", destination)
    return json.loads(wsl("python3", "-c", LINUX_TREE, destination))


def verify_files(directory, manifest, name):
    restored = PRIVATE / "restore-tests" / name
    private_directory(restored)
    for folder in ("data", "secrets", "native", "helper-src", "controller-secrets", "config"):
        if (directory / folder).is_dir():
            copy_checked(directory / folder, restored / folder)
    if manifest.get("complete_m4"):
        for folder, expected in manifest["linux_state"].items():
            destination = LINUX + "/restore-tests/" + name + "/" + folder
            if restore_linux_archive(directory, folder, destination) != expected:
                raise ValueError("Restored Linux files differ from the complete backup inventory")
        dbpath = LINUX + "/restore-tests/" + name + "/controller/controller.db"
        wsl("python3", "-c", "import pathlib,sqlite3,sys; p=pathlib.Path(sys.argv[1]); "
            "assert p.is_file(); c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True); "
            "assert c.execute('pragma quick_check').fetchone()[0]=='ok'; c.close()", dbpath)
    return restored


def backup(*, stopped=False, maintenance_scope=None):
    owners = ownership()
    before = stopped_inventory(owners) if stopped else inventory()
    require_quiet(before, maintenance_scope=maintenance_scope)
    if maintenance_scope is not None and not stopped:
        raise ValueError("Legacy maintenance backup requires stopped services")
    if before["revision"] not in {"0008", "0009", "0010"}:
        raise ValueError("This release profile supports only schema 0008, 0009 and 0010")
    complete_m4 = before["revision"] in {"0009", "0010"}
    if complete_m4 and (not stopped or any(owners.get(service, {}).get("state") == "running"
        for service in ("api", "worker", "sandbox-controller")) or broker_processes() or owned_sandboxes(before)):
        raise ValueError("M4 backup requires API, Worker, broker and controller stopped and no owned sandboxes")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    directory = PRIVATE / "backups" / stamp
    private_directory(directory)
    dump = docker("exec", owners["postgres"]["name"], "pg_dump", "-U", "omniharness", "-d", active_database(owners),
                  "--format=custom", "--no-owner", "--no-acl")
    if not dump.startswith(b"PGDMP"):
        raise ValueError("PostgreSQL did not return a custom-format backup")
    (directory / "database.pgdump").write_bytes(dump)
    paths = mounted_paths(owners)
    for name, source in paths.items():
        copy_checked(source, directory / name)
    config = directory / "config"
    config.mkdir()
    for name in ("compose.yaml", ".env", ".omniharness/deployment.env"):
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, config / source.name)
    for name in ("controller.toml", "broker.json", "prepared.json"):
        if (PRIVATE / name).is_file():
            shutil.copy2(PRIVATE / name, config / name)
    if (PRIVATE / "secrets").exists():
        copy_checked(PRIVATE / "secrets", directory / "controller-secrets")
    linux_state = {}
    if complete_m4:
        broker = read_json(PRIVATE / "broker.json")
        native = Path(broker["native_storage_root"]).resolve()
        if native != (PRIVATE / "native").resolve() and PRIVATE.resolve() / "restores" not in native.parents:
            raise ValueError("Native recovery store has unknown ownership")
        copy_checked(native, directory / "native")
        helper = Path(read_json(PRIVATE / "prepared.json")["broker_source"]).resolve()
        if PRIVATE.resolve() / "releases" not in helper.parents:
            raise ValueError("Fixed helper source has unknown ownership")
        copy_checked(helper, directory / "helper-src")
        for name in ("controller", "snapshots"):
            archive = directory / (name + ".tar")
            original = json.loads(wsl("python3", "-c", LINUX_TREE, LINUX + "/" + name, mapped(archive)))
            if original != json.loads(wsl("python3", "-c", LINUX_TREE, LINUX + "/" + name)):
                raise ValueError("Linux state changed during backup")
            if archive_inventory(archive) != original:
                raise ValueError("Linux state archive differs from its source")
            linux_state[name] = original
    after = stopped_inventory(owners) if stopped else inventory()
    if before != after:
        raise ValueError("Live database changed during backup; take a fresh backup before cutover")
    manifest = {"schema_version": 2, "created_at": datetime.now(timezone.utc).isoformat(), "source": str(ROOT),
                "project": PROJECT, "stopped": stopped, "inventory": before, "owners": owners,
                "complete_m4": complete_m4, "linux_state": linux_state,
                "source_paths": {name: str(path) for name, path in paths.items()},
                "database": active_database(owners),
                "directories": directory_manifest(directory),
                "files": file_manifest(directory)}
    if maintenance_scope is not None:
        manifest["legacy_maintenance_scope"] = maintenance_scope.manifest()
    write_json(directory / "manifest.json", manifest)
    print(json.dumps({"backup": str(directory), "revision": (before or {}).get("revision"),
                      "file_count": len(manifest["files"])}))
    return directory


def validated_backup(directory):
    directory = directory.resolve()
    if PRIVATE.resolve() / "backups" not in directory.parents:
        raise ValueError("Backup must be inside this production profile's private backup root")
    manifest = read_json(directory / "manifest.json")
    require_supported_creation_storage(manifest["inventory"])
    if "legacy_maintenance_scope" in manifest:
        scope = LegacyMaintenanceScope(**manifest["legacy_maintenance_scope"])
        require_quiet(manifest["inventory"], maintenance_scope=scope)
        if not manifest.get("stopped") or not manifest.get("complete_m4"):
            raise ValueError("Legacy maintenance backup requires complete stopped state")
    else:
        require_quiet(manifest["inventory"])
    actual = file_manifest(directory)
    actual.pop("manifest.json", None)
    actual.pop("verification.json", None)
    # Ephemeral verification credential files are deliberately never retained.
    if actual != manifest["files"] or manifest["source"] != str(ROOT) or manifest["project"] != PROJECT:
        raise ValueError("Backup contents or ownership differ from the recorded manifest")
    if "directories" in manifest and directory_manifest(directory) != manifest["directories"]:
        raise ValueError("Backup directory inventory differs from the manifest")
    if manifest["inventory"]["revision"] in {"0009", "0010"}:
        if not manifest.get("complete_m4") or not manifest.get("stopped"):
            raise ValueError("Schema 0009 requires a complete stopped M4 backup")
        for name in ("data", "secrets", "native", "helper-src", "controller-secrets", "config"):
            file_manifest(directory / name)
        for name in ("controller", "snapshots"):
            if archive_inventory(directory / (name + ".tar")) != manifest.get("linux_state", {}).get(name):
                raise ValueError("M4 archive inventory differs from its manifest")
    return manifest


def verify(directory, image, *, maintenance_probe=None):
    image_id(image)
    manifest = validated_backup(directory)
    name = "omni-m4-restore-" + secrets.token_hex(6)
    restored_files = verify_files(directory, manifest, name)
    credential = PRIVATE / "secrets" / (name + ".env")
    recovery_credential = PRIVATE / "secrets" / (name + "-recovery.env")
    private_directory(credential.parent)
    password = secrets.token_hex(32)
    credential.write_text(f"POSTGRES_PASSWORD={password}\nPOSTGRES_USER=omniharness\nPOSTGRES_DB=omniharness\n"
        f"OMNI_DATABASE_URL=postgresql+psycopg://omniharness:{password}@{name}:5432/omniharness\n"
        "OMNI_SECRET_DIR=/restore-secrets\nOMNI_DATA_DIR=/tmp/restore-data\nOMNI_ENABLE_AGENT=false\n", encoding="utf-8")
    pg_image = manifest["owners"]["postgres"]["image"]
    network = name + "-network"
    try:
        docker("network", "create", "--internal", "--label", "omni.m4.restore=" + name, network)
        docker("run", "-d", "--name", name, "--label", "omni.m4.restore=" + name, "--network", network,
               "--env-file", mapped(credential), pg_image)
        for _ in range(60):
            result = subprocess.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--exec", "docker", "exec", name,
                "pg_isready", "-U", "omniharness", "-d", "omniharness"], capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(.25)
        else:
            raise RuntimeError("Isolated restore database did not become ready")
        docker("exec", "-i", name, "pg_restore", "-U", "omniharness", "-d", "omniharness", "--no-owner", "--no-acl",
               "--exit-on-error", data=(directory / "database.pgdump").read_bytes())
        def candidate(*args):
            return docker("run", "--rm", "--network", network, "--env-file", mapped(credential),
                "--mount", f"type=bind,source={mapped(restored_files / 'secrets')},target=/restore-secrets,readonly",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", image, *args)
        restored = json.loads(candidate("python", "-c", INVENTORY))
        if not inventory_matches(manifest["inventory"], restored):
            raise ValueError("Restored database differs from the captured live inventory")
        head = candidate("python", "-c", "import pathlib,omniharness; from alembic.config import Config; "
            "from alembic.script import ScriptDirectory; c=Config(); "
            "c.set_main_option('script_location',str(pathlib.Path(omniharness.__file__).parent/'migrations')); "
            "print(ScriptDirectory.from_config(c).get_current_head())").decode().strip()
        source_revision = restored["revision"]
        if (source_revision, head) not in {("0008", "0009"), ("0009", "0009"),
                                           ("0009", "0010"), ("0010", "0010")}:
            raise ValueError("Candidate schema head differs from this reviewed release path")
        if source_revision != head:
            candidate("omniharness", "migrate")
        upgraded = json.loads(candidate("python", "-c", INVENTORY))
        if not migration_preserved(restored, upgraded):
            raise ValueError("Migration did not preserve configuration, history, pricing or output limits")
        if head == "0010":
            candidate("python", "-c", VERIFY_0010)
        readable = json.loads(candidate("python", "-c", VERIFY_KEYS))
        # Exercise rollback into a separate database using the backed-up image.
        # Never alter the migrated database or the live service.
        docker("exec", name, "createdb", "-U", "omniharness", "-O", "omniharness", "omni_recovery")
        docker("exec", "-i", name, "pg_restore", "-U", "omniharness", "-d", "omni_recovery",
               "--no-owner", "--no-acl", "--exit-on-error", data=(directory / "database.pgdump").read_bytes())
        recovery_credential.write_text(
            f"OMNI_DATABASE_URL=postgresql+psycopg://omniharness:{password}@{name}:5432/omni_recovery\n"
            "OMNI_SECRET_DIR=/restore-secrets\nOMNI_DATA_DIR=/tmp/restore-data\nOMNI_ENABLE_AGENT=false\n",
            encoding="utf-8")
        old_image = image_id(manifest["owners"]["api"]["image"])
        def recovery(*args):
            return docker("run", "--rm", "--network", network, "--env-file", mapped(recovery_credential),
                "--mount", f"type=bind,source={mapped(restored_files / 'secrets')},target=/restore-secrets,readonly",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", old_image, *args)
        if not inventory_matches(manifest["inventory"], json.loads(recovery("python", "-c", INVENTORY))):
            raise ValueError("Independent rollback database differs from its backup")
        recovery_readable = json.loads(recovery("python", "-c", VERIFY_KEYS))
        if recovery_readable != readable:
            raise ValueError("Rollback image could not verify the same saved credentials")
        report = {"image": image, "source_revision": restored["revision"], "target_revision": upgraded["revision"],
                  "mode": "same-schema-update" if source_revision == head else f"migration-{source_revision}-{head}",
                  "complete_m4_files_restored": bool(manifest.get("complete_m4")),
                  "manifest_sha256": hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
                  "restored_files": str(restored_files),
                  "tables_preserved": True, "rollback_database_verified": True, "output_limits": upgraded["output_limits"],
                  "credentials_readable": readable["credentials_readable"], "inventory": upgraded,
                  "verified_at": datetime.now(timezone.utc).isoformat()}
        if maintenance_probe is not None:
            if "legacy_maintenance_scope" not in manifest:
                raise ValueError("A legacy retirement rehearsal requires a scoped maintenance backup")
            report["legacy_retirement_rehearsal"] = maintenance_probe({
                "database_container": name, "network": network, "credential": credential, "manifest": manifest})
        write_json(directory / "verification.json", report)
        print(json.dumps({key: value for key, value in report.items() if key != "inventory"}))
        return report
    finally:
        # Only resources generated and labelled by this invocation are touched.
        for kind, identity in (("container", name), ("network", network)):
            result = subprocess.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--exec", "docker", kind, "inspect", identity], capture_output=True)
            if result.returncode == 0:
                item = json.loads(result.stdout)[0]
                labels = item.get("Config", {}).get("Labels", {}) if kind == "container" else item.get("Labels", {})
                if labels.get("omni.m4.restore") != name:
                    raise ValueError("Restore resource ownership changed; refusing cleanup")
                docker("rm", "-f", "-v", identity) if kind == "container" else docker("network", "rm", identity)
        credential.unlink(missing_ok=True)
        recovery_credential.unlink(missing_ok=True)


def prepare(image):
    image_id(image)
    owners = ownership()
    current_inventory = inventory() if owners["api"]["state"] == "running" else stopped_inventory(owners)
    require_supported_creation_storage(current_inventory)
    revision = current_inventory["revision"]
    state = PRIVATE / "staged" / image.split(":")[1] if revision in {"0009", "0010"} else PRIVATE
    app_paths = mounted_paths(owners)
    for folder in (PRIVATE, PRIVATE / "secrets", PRIVATE / "native", PRIVATE / "logs"):
        private_directory(folder)
    private_directory(state)
    private_directory(state / "secrets")
    for filename in ("m4-workspace-service.key", "m4-controller.key"):
        path = app_paths["secrets"] / filename
        if not path.exists():
            with path.open("x", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(48))
    for pinned in (TOOL, EXECD, EGRESS):
        image_id(pinned)
    config = (ROOT / "deploy/opensandbox.toml").read_text(encoding="utf-8")
    config = config.replace("__M4_EXECD_IMAGE__", EXECD).replace("__M4_EGRESS_IMAGE__", EGRESS)
    config = config.replace("/var/lib/omniharness-m4a/", LINUX + "/").replace("41000", "41400").replace("41100", "41500")
    (state / "controller.toml").write_text(config, encoding="utf-8")
    (state / "secrets/controller.env").write_text("OPENSANDBOX_SERVER_API_KEY=" +
        (app_paths["secrets"] / "m4-controller.key").read_text().strip() + "\n", encoding="utf-8")
    wsl("mkdir", "-p", LINUX + "/controller", LINUX + "/snapshots")
    wsl("chmod", "700", LINUX, LINUX + "/controller", LINUX + "/snapshots")
    source = PRIVATE / "releases" / image.split(":")[1] / "src"
    if not source.exists():
        source.parent.mkdir(parents=True)
        container = docker("create", "--network", "none", image).decode().strip()
        try:
            docker("cp", container + ":/app/src", mapped(source))
        finally:
            docker("rm", container)
    broker = {"schema_version": 1, "instance_id": INSTANCE, "application_url": "http://127.0.0.1:8787",
        "service_key_file": mapped(app_paths["secrets"] / "m4-workspace-service.key"),
        "python_executable": mapped(ROOT / ".venv/Scripts/python.exe"),
        "module_file": str(source / "omniharness/workspace_io.py"), "framework_root": str(ROOT),
        "native_storage_root": str(PRIVATE / "native"), "native_protected_roots": [str(ROOT), str(SECRET)],
        "mapped_native_storage_root": mapped(PRIVATE / "native"), "snapshot_root": LINUX + "/snapshots",
        "bind_host": "172.17.0.1", "port": 19098}
    write_json(state / "broker.json", broker)
    if revision == "0008":
        wsl("python3", "-m", "venv", LINUX + "/broker-venv")
        wsl(LINUX + "/broker-venv/bin/pip", "install", "--disable-pip-version-check", "-r",
            mapped(ROOT / "deploy/m4-broker-requirements.txt"))
    profile = f"OMNI_RELEASE_IMAGE={image}\nOMNI_ENABLE_AGENT=true\nOMNI_ENABLE_TOOLS=true\nOMNI_INSTANCE_ID={INSTANCE}\n"
    profile += f"OMNI_WORKSPACE_BROKER_URL=http://host.docker.internal:19098\nOMNI_SANDBOX_IMAGE={TOOL}\nCOMPOSE_PROFILES=tools\n"
    profile += f"OMNI_DOCKER_BACKEND=wsl\nOMNI_WSL_DISTRO={DISTRO}\n"
    profile += (f"OMNI_DATABASE_NAME={active_database(owners)}\nOMNI_DEPLOY_DATA_DIR={mapped(app_paths['data'])}\n"
                f"OMNI_DEPLOY_SECRET_DIR={mapped(app_paths['secrets'])}\n")
    (state / "candidate.env").write_text(profile, encoding="utf-8")
    write_json(state / "prepared.json", {"image": image, "broker_source": str(source), "instance_id": INSTANCE})
    compose("config", "--quiet", profile_file=state / "candidate.env")
    print("Independent production tool profile prepared; production containers were not changed.")


def prepared_state(image):
    staged = PRIVATE / "staged" / image.split(":")[1]
    state = staged if staged.is_dir() else PRIVATE
    if read_json(state / "prepared.json")["image"] != image:
        raise ValueError("Prepared helper must match the selected immutable release")
    return state


def activate_prepared(image):
    state = prepared_state(image)
    if state != PRIVATE:
        for filename in ("broker.json", "controller.toml", "prepared.json", "candidate.env", "secrets/controller.env"):
            shutil.copy2(state / filename, PRIVATE / filename)
    persist_profile((PRIVATE / "candidate.env").read_text(encoding="utf-8"))


def backup_verify(image):
    owners = quiesce()
    directory = backup(stopped=True)
    verify(directory, image)
    # Failure leaves services stopped and evidence intact for deliberate recovery.
    resume_owned(owners)
    return directory


def broker_start():
    prepared = read_json(PRIVATE / "prepared.json")
    profile = mapped(PRIVATE / "broker.json")
    instances = broker_processes()
    expected_source = mapped(Path(prepared["broker_source"]))
    if instances and any(item["source"] != expected_source for item in instances):
        raise ValueError("An older production broker is still active; stop that owned helper before starting the new release")
    probe = '''import json,urllib.request,sys
p=json.load(open(sys.argv[1])); key=open(p['service_key_file']).read().strip()
r=urllib.request.Request('http://172.17.0.1:19098/health',headers={'Authorization':'Bearer '+key,'X-Omni-Instance':p['instance_id']})
with urllib.request.urlopen(r,timeout=3) as response: value=json.load(response)
assert value['instance_id']==p['instance_id'] and value['status']=='ok'
'''
    try:
        wsl("python3", "-c", probe, profile, timeout=10)
        print("Production workspace broker is already healthy.")
        return
    except RuntimeError:
        pass
    wsl("python3", "-c", "import socket; s=socket.socket(); s.bind(('172.17.0.1',19098)); s.close()")
    command = ["wsl.exe", "-d", DISTRO, "-u", "root", "--exec", "env", "OMNI_BROKER_PROFILE=" + profile,
        LINUX + "/broker-venv/bin/python", "-m", "uvicorn", "omniharness.workspace_broker:create_deployment_app",
        "--factory", "--app-dir", mapped(Path(prepared["broker_source"])), "--host", "172.17.0.1", "--port", "19098", "--no-access-log"]
    with (PRIVATE / "logs/broker.stdout.log").open("ab") as out, (PRIVATE / "logs/broker.stderr.log").open("ab") as err:
        process = subprocess.Popen(command, stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
    write_json(PRIVATE / "broker-process.json", {"windows_pid": process.pid, "profile": profile})
    for _ in range(30):
        try:
            wsl("python3", "-c", probe, profile, timeout=10)
            print("Production workspace broker is healthy; fixed native helper and independent key active.")
            return
        except RuntimeError:
            time.sleep(.25)
    raise RuntimeError("Broker did not become healthy; inspect its private logs")


def broker_processes():
    code = '''import json,pathlib,sys
profile,interpreter=sys.argv[1:]; result=[]
for entry in pathlib.Path('/proc').iterdir():
 if not entry.name.isdigit(): continue
 try:
  args=[x.decode() for x in (entry/'cmdline').read_bytes().split(b'\\0') if x]
  environment=(entry/'environ').read_bytes().split(b'\\0')
 except (OSError,UnicodeError): continue
 if not args or args[0]!=interpreter or 'omniharness.workspace_broker:create_deployment_app' not in args: continue
 if ('OMNI_BROKER_PROFILE='+profile).encode() not in environment: continue
 if '--app-dir' not in args or '--port' not in args or args[args.index('--port')+1]!='19098': continue
 result.append({'pid':int(entry.name),'source':args[args.index('--app-dir')+1]})
print(json.dumps(result))
'''
    return json.loads(wsl("python3", "-c", code, mapped(PRIVATE / "broker.json"), LINUX + "/broker-venv/bin/python"))


def broker_stop():
    for item in broker_processes():
        if not item["source"].startswith(mapped(PRIVATE / "releases") + "/"):
            raise ValueError("Broker source is outside the owned release root; refusing to stop it")
        wsl("kill", "-TERM", str(item["pid"]))
    for _ in range(30):
        if not broker_processes():
            print("Owned production workspace broker stopped.")
            return
        time.sleep(.2)
    raise RuntimeError("Owned broker did not stop; no forced kill was attempted")


def cutover(image, verified_backup, *, same_schema=False):
    image_id(image)
    source = validated_backup(verified_backup)
    require_quiet(source["inventory"])
    verification = read_json(verified_backup / "verification.json")
    prepared_state(image)
    if (verification["image"] != image or verification.get("manifest_sha256") != hashlib.sha256(
            (verified_backup / "manifest.json").read_bytes()).hexdigest()):
        raise ValueError("Prepared and restore-tested images must exactly equal the selected release")
    source_revision = source["inventory"]["revision"]
    target_revision = verification.get("target_revision", "0009")
    if (source_revision == target_revision) != same_schema:
        raise ValueError("Choose update for the same schema or cutover for a migration")
    live_owners = ownership()
    live = inventory() if live_owners["api"]["state"] == "running" else stopped_inventory(live_owners)
    if live["revision"] != source["inventory"]["revision"]:
        raise ValueError("Live schema changed since the selected release backup")
    # The caller explicitly selected cutover. Pause ingress first, then the sole
    # Worker. A request racing the read-only precheck remains queued in backup.
    quiesce()
    fresh = backup(stopped=True)
    verify(fresh, image)
    fresh_proof = read_json(fresh / "verification.json")
    if (fresh_proof.get("target_revision", "0009") != target_revision
            or fresh_proof.get("source_revision", source_revision) != source_revision):
        raise ValueError("Fresh migration proof differs from the selected release")
    activate_prepared(image)
    if source_revision != target_revision:
        compose("run", "--rm", "--no-deps", "-T", "init", "omniharness", "migrate")
        migrated = json.loads(compose("run", "--rm", "--no-deps", "-T", "init", "python", "-c", INVENTORY))
        if migrated != fresh_proof["inventory"]:
            raise ValueError("Live migration differs from the isolated migration proof; services remain stopped")
        if target_revision == "0010":
            compose("run", "--rm", "--no-deps", "-T", "init", "python", "-c", VERIFY_0010)
    broker_start()
    compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "180")
    current = inventory()
    if current["revision"] != target_revision or current["tables"] != read_json(fresh / "verification.json")["inventory"]["tables"]:
        raise ValueError("Post-cutover data verification failed; stop and recover from the fresh backup")
    write_json(PRIVATE / "deployment.json", {"image": image, "backup": str(fresh), "inventory": current})
    print(json.dumps({"url": "http://127.0.0.1:8787", "image": image, "backup": str(fresh), "revision": current["revision"]}))


def backup_tool_image(directory, manifest):
    """Read the exact immutable tool image from the hash-verified old profile."""
    if not manifest.get("complete_m4"):
        return None  # The legacy 0008 recovery keeps tools disabled.
    name = "config/deployment.env"
    source = directory / name
    expected = manifest.get("files", {}).get(name)
    if not expected or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise ValueError("Backup has no verified deployment profile for its tool image")
    values = []
    for line in source.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "OMNI_SANDBOX_IMAGE":
            values.append(value.strip())
    if len(values) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", values[0]):
        raise ValueError("Backup must contain exactly one immutable tool image ID")
    return values[0]


def rollback(directory):
    """Restore to a NEW database and private paths; preserve the upgraded copy."""
    manifest = validated_backup(directory)
    require_quiet(manifest["inventory"])
    old_image = image_id(manifest["owners"]["api"]["image"])
    old_tool_image = backup_tool_image(directory, manifest)
    if old_tool_image is not None:
        image_id(old_tool_image)  # Check local availability before stopping services.
    owners = quiesce()
    preserved = backup(stopped=True)
    upgraded_database = active_database(owners)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + secrets.token_hex(3)
    dbname = "omni_restore_" + suffix
    restore = PRIVATE / "restores" / suffix
    private_directory(restore)
    for name in ("data", "secrets"):
        copy_checked(directory / name, restore / name)
    docker("exec", owners["postgres"]["name"], "createdb", "-U", "omniharness", "-O", "omniharness", dbname)
    docker("exec", "-i", owners["postgres"]["name"], "pg_restore", "-U", "omniharness", "-d", dbname,
        "--no-owner", "--no-acl", "--exit-on-error", data=(directory / "database.pgdump").read_bytes())
    tools = bool(manifest.get("complete_m4"))
    if tools:
        copy_checked(directory / "native", restore / "native")
        for name in ("controller", "snapshots"):
            target = LINUX + "/restores/" + suffix + "/" + name
            if restore_linux_archive(directory, name, target) != manifest["linux_state"][name]:
                raise ValueError("Recovery files differ before activation")
        helper = PRIVATE / "releases" / old_image.split(":")[1] / "src"
        if helper.exists():
            if file_manifest(helper) != file_manifest(directory / "helper-src"):
                raise ValueError("Saved immutable helper differs from the local release")
        else:
            helper.parent.mkdir(parents=True)
            copy_checked(directory / "helper-src", helper)
        for name in ("controller", "snapshots"):
            # All resolved paths are under the exact owned Linux root. Retain
            # the upgraded tree and move the independently restored tree in.
            wsl("python3", "-c", "import pathlib,sys; root=pathlib.Path(sys.argv[1]).resolve(); "
                "old=root/sys.argv[2]; fresh=root/'restores'/sys.argv[3]/sys.argv[2]; "
                "saved=root/'retired'/sys.argv[3]/sys.argv[2]; "
                "assert all(p.resolve().is_relative_to(root) for p in (old,fresh,saved)); "
                "assert not saved.exists() and fresh.is_dir(); saved.parent.mkdir(parents=True,exist_ok=True); "
                "old.rename(saved); fresh.rename(old)", LINUX, name, suffix)
        broker = read_json(directory / "config/broker.json")
        broker.update(native_storage_root=str(restore / "native"), mapped_native_storage_root=mapped(restore / "native"),
            service_key_file=mapped(restore / "secrets/m4-workspace-service.key"),
            module_file=str(helper / "omniharness/workspace_io.py"))
        write_json(PRIVATE / "broker.json", broker)
        write_json(PRIVATE / "prepared.json", {"image": old_image, "broker_source": str(helper), "instance_id": INSTANCE})
        shutil.copy2(directory / "config/controller.toml", PRIVATE / "controller.toml")
        shutil.copy2(directory / "controller-secrets/controller.env", PRIVATE / "secrets/controller.env")
    profile = (f"OMNI_RELEASE_IMAGE={old_image}\nOMNI_ENABLE_TOOLS={'true' if tools else 'false'}\nOMNI_ENABLE_AGENT=true\n"
               f"OMNI_DATABASE_NAME={dbname}\nOMNI_DEPLOY_DATA_DIR={mapped(restore / 'data')}\n"
               f"OMNI_DEPLOY_SECRET_DIR={mapped(restore / 'secrets')}\nCOMPOSE_PROFILES={'tools' if tools else ''}\n"
               f"OMNI_DOCKER_BACKEND=wsl\nOMNI_WSL_DISTRO={DISTRO}\n")
    if tools:
        profile += f"OMNI_INSTANCE_ID={INSTANCE}\nOMNI_WORKSPACE_BROKER_URL=http://host.docker.internal:19098\nOMNI_SANDBOX_IMAGE={old_tool_image}\n"
    persist_profile(profile)
    if tools:
        broker_start()
        compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "180", "sandbox-controller")
    compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "180", "api", "worker")
    current = inventory()
    if not inventory_matches(manifest["inventory"], current):
        raise ValueError("Restored live data differs from the selected backup")
    print(json.dumps({"recovered_database": dbname, "image": old_image, "revision": current["revision"],
                      "preserved_upgraded_database": upgraded_database, "preserved_state_backup": str(preserved),
                      "restored_files": str(restore)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "backup", "backup-verify", "verify", "prepare", "broker-start", "broker-stop", "cutover", "update", "rollback"))
    parser.add_argument("--image")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.action in {"verify", "prepare", "cutover", "update", "backup-verify"} and not args.image:
        parser.error("--image is required")
    if args.action in {"verify", "cutover", "update", "rollback"} and args.backup is None:
        parser.error("--backup is required")
    if args.action == "inspect":
        print(json.dumps({"owners": ownership(), "inventory": inventory()}, ensure_ascii=False))
    elif args.action == "backup":
        backup()
    elif args.action == "verify":
        verify(args.backup, args.image)
    else:
        with deployment_lock():
            if args.action == "prepare":
                prepare(args.image)
            elif args.action == "broker-start":
                broker_start()
            elif args.action == "broker-stop":
                broker_stop()
            elif args.action == "rollback":
                rollback(args.backup)
            elif args.action == "backup-verify":
                backup_verify(args.image)
            else:
                cutover(args.image, args.backup, same_schema=args.action == "update")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Deployment stopped: {exc}" if isinstance(exc, (ValueError, RuntimeError))
              else "Deployment stopped; inspect private state and do not print credentials.")
        raise SystemExit(1) from None
