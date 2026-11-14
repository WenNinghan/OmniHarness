"""Explicit, isolated V6 preview. Never modifies the production Compose stack.

Run actions in order: prepare, build, configure, database, bootstrap,
import-configs, broker, up, status. No action sends a model request.
Secrets are stored under the preview's private directory and never printed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import inspect
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".omniharness" / "v6-preview"
LINUX_ROOT = "/var/lib/omniharness-v6-preview"
DISTRO = "OmniHarnessCheckAlt"
PROJECT = "omni-v6-preview"
FIXTURE = Path(r"G:\OmniHarness-V6-Preview-Fixture")
TOOL = "sha256:8e0afff95c8c7b387992f479d9641685bae3e9ccfe4545168a6efbde5b955165"
EXECD = "sha256:6b5a76c64ef66f500d38e068b1a50d68eb2f7fbc6d18cddbce525d86faeca0e1"
EGRESS = "sha256:cad71a020c0990470d4ebe12eb01fc4e6e12afb431d67ae6b655626f7539b8e3"
APP_BASE = "sha256:a596d6108779a275829c22abf2ef0516531634d317db909f7777becba758cc8d"
WEB_BASE = "sha256:98be25eb6a1c99b820b0ca9e65ed84f7c42b8190493ef873bc7ac25f9edbf7ae"
BASES = {
    "node:24-bookworm-slim": "docker.m.daocloud.io/library/node@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9",
    "ghcr.io/astral-sh/uv:0.11.6": "ghcr.m.daocloud.io/astral-sh/uv@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754",
    "python:3.12-slim-bookworm": "docker.m.daocloud.io/library/python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df",
}


def mapped(path: Path) -> str:
    path = path.resolve()
    if path.drive.lower() != "g:":
        raise ValueError("The isolated profile requires the G: workspace")
    return "/mnt/g/" + path.as_posix()[3:]


def wsl(*args: str, data: bytes | None = None, timeout: int = 600) -> bytes:
    result = subprocess.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--exec", *args],
                            input=data, capture_output=True, check=False, timeout=timeout)
    if result.returncode:
        # Process output may contain private config. Never reproduce it here.
        raise RuntimeError(f"Isolated preview operation failed ({args[0]}, exit {result.returncode})")
    return result.stdout


def compose(*args: str, data: bytes | None = None, timeout: int = 600) -> bytes:
    _assert_compose_locations()
    return wsl("env", "-i", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               "HOME=/root", "docker", "compose", "--project-name", PROJECT,
               "--file", mapped(ROOT / "deploy/v6-preview.compose.yaml"),
               "--env-file", mapped(PRIVATE / "secrets/compose.env"),
               *args, data=data, timeout=timeout)


def _file(name: str) -> Path:
    if name not in {"postgres.key", "controller.key", "workspace-service.key", "master.key"}:
        raise ValueError("Invalid private filename")
    return PRIVATE / "secrets" / name


def _exclusive(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)


def _assert_project_root() -> None:
    if (str(ROOT).casefold() != r"g:\harness" or PRIVATE.parent != ROOT / ".omniharness"
            or (ROOT / ".omniharness").resolve() != ROOT / ".omniharness"
            or PRIVATE.is_symlink() or PRIVATE.resolve() != PRIVATE):
        raise ValueError("The reviewed preview is pinned to G:\\harness")


def _profile_fields(profile: bytes) -> dict[str, str]:
    fields = {}
    for line in profile.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in fields:
            raise ValueError("Invalid or duplicated private preview setting")
        fields[key] = value
    return fields


def _assert_compose_locations() -> None:
    """Reject untrusted mount-source overrides before expanding Compose."""
    _assert_project_root()
    fields = _profile_fields((PRIVATE / "secrets" / "compose.env").read_bytes())
    expected = {"OMNI_V6_DATA_DIR": PRIVATE / "data", "OMNI_V6_SECRET_DIR": PRIVATE / "secrets",
                "OMNI_V6_CONTROLLER_ENV": PRIVATE / "secrets" / "controller.env",
                "OMNI_V6_CONTROLLER_CONFIG": PRIVATE / "controller.toml"}
    for key, path in expected.items():
        if path.is_symlink() or path.resolve() != path or fields.get(key) != mapped(path):
            raise ValueError("Private preview path does not match the fixed isolated target")


def _assert_ports_free() -> None:
    for port in (8788, 19100):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    # Probe the Docker bridge broker and the dedicated sandbox range in WSL.
    code = ("import socket; held=[]\n"
            "try:\n"
            " for host,port in [('172.17.0.1',19102)]+[('172.17.0.1',p) for p in range(41400,41501)]:\n"
            "  s=socket.socket(); s.bind((host,port)); held.append(s)\n"
            "finally:\n"
            " for s in held: s.close()\n")
    wsl("python3", "-c", code)


def prepare() -> None:
    _assert_project_root()
    _assert_ports_free()
    if PRIVATE.exists() or PRIVATE.is_symlink() or FIXTURE.exists() or FIXTURE.is_symlink():
        raise ValueError("Preview directory or fixture already exists; refusing to overwrite")
    existing = wsl("docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}",
                   "--format", "{{.Names}}").decode().strip()
    if existing:
        raise ValueError("A preview project already exists; refusing to reuse it")
    linux_exists = wsl("python3", "-c", "import os; print(int(os.path.lexists('/var/lib/omniharness-v6-preview')))").decode().strip()
    if linux_exists != "0":
        raise ValueError("The preview Linux storage root already exists; refusing to reuse it")
    volumes = set(wsl("docker", "volume", "ls", "--format", "{{.Name}}").decode().splitlines())
    owned_volumes = wsl("docker", "volume", "ls", "--filter", f"label=com.docker.compose.project={PROJECT}",
                        "--format", "{{.Name}}").decode().strip()
    if PROJECT + "_preview-postgres" in volumes or owned_volumes:
        raise ValueError("A preview database volume already exists; refusing to reuse it")
    networks = set(wsl("docker", "network", "ls", "--format", "{{.Name}}").decode().splitlines())
    owned_networks = wsl("docker", "network", "ls", "--filter", f"label=com.docker.compose.project={PROJECT}",
                         "--format", "{{.Name}}").decode().strip()
    if PROJECT + "_default" in networks or owned_networks:
        raise ValueError("A preview project network already exists; refusing to reuse it")
    PRIVATE.mkdir(parents=True)
    for folder in ("secrets", "data", "native", "logs"):
        (PRIVATE / folder).mkdir()
    account = subprocess.check_output(["whoami"], text=True).strip()
    subprocess.run(["icacls", str(PRIVATE / "secrets"), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"],
                   capture_output=True, check=True)
    for name in ("postgres.key", "controller.key", "workspace-service.key"):
        _exclusive(_file(name), secrets.token_hex(32).encode())
    _exclusive(_file("master.key"), Fernet.generate_key())
    _exclusive(PRIVATE / "secrets/controller.env", b"OPENSANDBOX_SERVER_API_KEY=" + _file("controller.key").read_bytes() + b"\n")
    config = (ROOT / "deploy/opensandbox.toml").read_text(encoding="utf-8")
    config = config.replace("__M4_EXECD_IMAGE__", EXECD).replace("__M4_EGRESS_IMAGE__", EGRESS)
    config = config.replace("/var/lib/omniharness-m4a/", LINUX_ROOT + "/")
    config = config.replace("41000", "41400").replace("41100", "41500")
    if "__M4_" in config or "/var/lib/omniharness-m4a/" in config:
        raise ValueError("Sandbox substitution is incomplete")
    _exclusive(PRIVATE / "controller.toml", config.encode("utf-8"))
    FIXTURE.mkdir()
    _exclusive(FIXTURE / "README.md", "# V6 预览测试项目\n\n仅供独立预览测试，不要在 8787 同时运行此目录。\n".encode())
    _exclusive(FIXTURE / "calculator.py", b"def add(a, b):\n    return a + b\n")
    _exclusive(FIXTURE / "test_calculator.py", b"from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    wsl("mkdir", LINUX_ROOT)
    wsl("mkdir", LINUX_ROOT + "/snapshots", LINUX_ROOT + "/controller")
    wsl("chmod", "700", LINUX_ROOT, LINUX_ROOT + "/snapshots", LINUX_ROOT + "/controller")
    print("Independent directories and test fixture prepared; no credentials printed.")


def build(*, metadata_path: Path | None = None) -> None:
    _assert_project_root()
    if not (PRIVATE / "secrets/master.key").is_file():
        raise ValueError("Prepare the isolated preview first")
    context = PRIVATE / ("build-" + secrets.token_hex(6))
    context.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "alembic.ini", ".dockerignore"):
        shutil.copy2(ROOT / name, context / name)
    ignore = shutil.ignore_patterns("node_modules", "dist", "__pycache__", "*.pyc", ".env", ".env.*")
    shutil.copytree(ROOT / "src", context / "src", ignore=ignore)
    shutil.copytree(ROOT / "web", context / "web", ignore=ignore)
    previous = ROOT / ".omniharness/m4-preview/build-dd803b4432"
    for name in ("pyproject.toml", "uv.lock", "web/package-lock.json"):
        if (previous / name).read_bytes() != (ROOT / name).read_bytes():
            raise ValueError("Dependency locks changed; cached image is not eligible")
    old = json.loads((previous / "web/package.json").read_text(encoding="utf-8"))
    new = json.loads((ROOT / "web/package.json").read_text(encoding="utf-8"))
    old.get("scripts", {}).pop("test", None)
    new.get("scripts", {}).pop("test", None)
    if old != new:
        raise ValueError("Web dependency settings changed; cached image is not eligible")
    app_tag, web_tag = f"omniharness/v6-deps:app-{context.name}", f"omniharness/v6-deps:web-{context.name}"
    candidate_tag = "omniharness/v6-preview:" + context.name
    existing_tags = set(wsl("docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}").decode().splitlines())
    if any(tag in existing_tags for tag in (app_tag, web_tag, candidate_tag)):
        raise ValueError("A generated preview image tag already exists; refusing to overwrite")
    for image, tag in ((APP_BASE, app_tag), (WEB_BASE, web_tag)):
        wsl("docker", "image", "inspect", image, "--format", "{{.Id}}")
        wsl("docker", "tag", image, tag)
        if wsl("docker", "image", "inspect", tag, "--format", "{{.Id}}").decode().strip() != image:
            raise ValueError("Cached dependency image identity differs")
    dockerfile = f'''FROM {web_tag} AS web-build
WORKDIR /build/web
COPY web/ ./
COPY src/omniharness/data/reference-prices-v1.json /build/src/omniharness/data/reference-prices-v1.json
RUN npm run build
FROM {app_tag} AS application
USER root
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
COPY alembic.ini ./
RUN uv sync --frozen --offline --no-dev --no-editable --reinstall-package omniharness
COPY --from=web-build /build/web/dist ./web/dist
USER 1000:1000
CMD ["omniharness", "serve"]
'''
    _exclusive(context / "Dockerfile", dockerfile.encode())
    log_path = (metadata_path.parent / "build.log") if metadata_path else (PRIVATE / "logs/build.log")
    with log_path.open("xb" if metadata_path else "wb") as log:
        result = subprocess.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--exec", "docker", "build",
                                 "--progress=plain", "-t", candidate_tag, mapped(context)],
                                stdout=log, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError("Isolated image build failed; see its private build log")
    image = wsl("docker", "image", "inspect", candidate_tag, "--format", "{{.Id}}").decode().strip()
    pinned = {}
    for name in ("workspace_broker.py", "workspace_io.py"):
        staged = (context / "src/omniharness" / name).read_bytes()
        if staged != (ROOT / "src/omniharness" / name).read_bytes():
            raise ValueError("Broker source changed during image build")
        pinned[name] = hashlib.sha256(staged).hexdigest()
    _exclusive(metadata_path or PRIVATE / "image.json", json.dumps({"image": image, "context": str(context),
                                                    "broker_sources": pinned}).encode())
    print("Candidate image built from current source; no prior preview was changed.")


def build_upgrade() -> None:
    """Build a separate application candidate without changing the live profile."""
    _assert_project_root()
    parent = PRIVATE / "upgrades"
    parent.mkdir(exist_ok=True)
    if parent.is_symlink() or parent.resolve().parent != PRIVATE.resolve():
        raise ValueError("unsafe preview candidate directory")
    directory = parent / ("candidate-" + secrets.token_hex(8))
    directory.mkdir(exist_ok=False)
    build(metadata_path=directory / "image.json")
    print(json.dumps({"candidate": directory.name}))


def configure() -> None:
    image = json.loads((PRIVATE / "image.json").read_text(encoding="utf-8"))["image"]
    if not image.startswith("sha256:"):
        raise ValueError("Invalid candidate image identity")
    values = {"OMNI_V6_POSTGRES_PASSWORD": _file("postgres.key").read_text(),
              "OMNI_V6_TOOL_IMAGE": TOOL, "OMNI_V6_IMAGE": image,
              "OMNI_V6_CONTROLLER_ENV": mapped(PRIVATE / "secrets/controller.env"),
              "OMNI_V6_CONTROLLER_CONFIG": mapped(PRIVATE / "controller.toml"),
              "OMNI_V6_DATA_DIR": mapped(PRIVATE / "data"),
              "OMNI_V6_SECRET_DIR": mapped(PRIVATE / "secrets")}
    _exclusive(PRIVATE / "secrets/compose.env", "".join(f"{k}={v}\n" for k, v in values.items()).encode())
    compose("config", "--quiet")
    print("Isolated Compose profile prepared; credentials not displayed.")


# The following two snippets communicate only through captured process pipes.
# They never print or persist a plaintext key. The source performs a single
# read-only, repeatable-read transaction and encrypts keys with the target key
# before returning the in-memory payload to the importer.
EXPORT = r'''import json, sys
from cryptography.fernet import Fernet
from sqlalchemy import text
from omniharness.db import make_engine
from omniharness.security import SecretStore
from omniharness.settings import Settings
target_key=sys.stdin.buffer.read().strip()
settings=Settings()
source_key=(settings.secret_dir/'master.key').read_bytes().strip()
if source_key == target_key: raise ValueError('preview key must be independent')
target=Fernet(target_key); source=SecretStore(settings)
engine=make_engine(settings)
tables=('providers','model_configs','role_bindings','stage_binding_sets')
with engine.connect() as db:
 db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
 data={table:[dict(r) for r in db.execute(text('SELECT * FROM '+table)).mappings()] for table in tables}
 for row in data['providers']:
  if row.get('adapter_type') != 'openai-compatible' or (row.get('base_url') or '').rstrip('/') != 'https://e-flowcode.cc/v1':
   raise ValueError('provider requires separate authorization')
  encrypted=row.get('encrypted_api_key')
  row['encrypted_api_key']=target.encrypt(source.decrypt(encrypted).encode()).decode() if encrypted else None
sys.stdout.write(json.dumps(data,default=str,ensure_ascii=False))
engine.dispose()
'''

IMPORT = r'''import json,sys
from datetime import datetime
from sqlalchemy import DateTime,func,select
from omniharness.db import make_engine
from omniharness.models import Provider,ModelConfig,RoleBinding,StageBindingSet,Run,Conversation,Message,Workspace,LocalSession,PairingCode
from omniharness.settings import Settings
data=json.load(sys.stdin)
engine=make_engine(Settings())
models=(Provider,ModelConfig,RoleBinding,StageBindingSet)
with engine.begin() as db:
 for model in (Provider,ModelConfig,RoleBinding,Run,Conversation,Message,Workspace,LocalSession,PairingCode):
  if db.scalar(select(func.count()).select_from(model)) != 0: raise ValueError('target is not empty')
 existing=list(db.execute(select(StageBindingSet.__table__)).mappings())
 if len(existing)>1 or any(r['id']!='local' or r['version']!=0 or r['bindings']!={} for r in existing):
  raise ValueError('target stage bindings are not the initial seed')
 providers={r['id'] for r in data['providers']}
 model_ids={r['id'] for r in data['model_configs']}
 if len(providers)!=len(data['providers']) or len(model_ids)!=len(data['model_configs']):
  raise ValueError('duplicate configuration identities')
 if any(r['provider_id'] not in providers for r in data['model_configs']): raise ValueError('provider binding missing')
 if any(r.get('model_config_id') and r['model_config_id'] not in model_ids for r in data['role_bindings']):
  raise ValueError('role binding missing')
 for row in data['stage_binding_sets']:
  bindings=row.get('bindings')
  if not isinstance(bindings,dict) or any(value and value not in model_ids for value in bindings.values()):
   raise ValueError('stage binding missing')
 for model in models:
  table=model.__table__; rows=[]
  for original in data[table.name]:
   row={k:v for k,v in original.items() if k in table.columns}
   for column in table.columns:
    if isinstance(column.type,DateTime) and row.get(column.name): row[column.name]=datetime.fromisoformat(row[column.name])
   rows.append(row)
  if model is StageBindingSet:
   for row in rows:
    found=db.execute(select(table).where(table.c.id==row['id'])).mappings().first()
    if found: db.execute(table.update().where(table.c.id==row['id']).values(**row))
    else: db.execute(table.insert(),row)
  elif rows: db.execute(table.insert(),rows)
 for model in models:
  table=model.__table__; expected=data[table.name]
  actual=list(db.execute(select(table)).mappings())
  keys=[c.name for c in table.primary_key.columns]
  key=lambda row: tuple(str(row[k]) for k in keys)
  if {key(row) for row in actual}!={key(row) for row in expected}: raise ValueError('import identity mismatch')
  if table.name=='stage_binding_sets':
   if any(next(r for r in actual if key(r)==key(src))['bindings']!=src['bindings'] for src in expected):
    raise ValueError('stage binding mismatch')
  if table.name=='role_bindings':
   if any(next(r for r in actual if key(r)==key(src))['model_config_id']!=src['model_config_id'] for src in expected):
    raise ValueError('role binding mismatch')
  safe_fields={'providers':('adapter_type','base_url','enabled','version'),
               'model_configs':('provider_id','name','model_name','parameters','timeout_seconds','max_output_tokens','version','capabilities','context_policy'),
               'role_bindings':('model_config_id','enabled','version'),
               'stage_binding_sets':('version','bindings')}[table.name]
  actual_by_key={key(row):row for row in actual}
  if any(any(actual_by_key[key(src)][field]!=src.get(field) for field in safe_fields) for src in expected):
   raise ValueError('imported configuration mismatch')
 print(json.dumps({table:len(data[table]) for table in data}))
engine.dispose()
'''


def database() -> None:
    compose("up", "-d", "--wait", "--wait-timeout", "180", "postgres", "sandbox-controller")
    print("Only the named preview database and controller were started.")


def bootstrap() -> None:
    compose("run", "--rm", "--no-deps", "-T", "init")
    print("Isolated database migrations and checkpointer initialized.")


def import_configs() -> None:
    if (PRIVATE / "configs-imported.json").exists():
        raise ValueError("Configuration has already been imported; refusing to overwrite")
    # Only this source container reads the original key. Its stdout is an
    # in-memory pipe carrying data already re-encrypted for the preview key.
    payload = wsl("docker", "exec", "-i", "omniharness-api-1", "python", "-c", EXPORT,
                  data=_file("master.key").read_bytes())
    result = compose("run", "--rm", "--no-deps", "-T", "api", "python", "-c", IMPORT, data=payload)
    summary = json.loads(result.decode("utf-8"))
    if set(summary) != {"providers", "model_configs", "role_bindings", "stage_binding_sets"}:
        raise ValueError("Unexpected import result")
    _exclusive(PRIVATE / "configs-imported.json", json.dumps(summary).encode())
    print(json.dumps({"model_configs": summary["model_configs"], "result": "imported"}))


def broker() -> None:
    if (PRIVATE / "broker-wsl.pid").exists():
        raise ValueError("Preview broker already has an ownership record")
    build_metadata = json.loads((PRIVATE / "image.json").read_text(encoding="utf-8"))
    for name in ("workspace_broker.py", "workspace_io.py"):
        if hashlib.sha256((ROOT / "src/omniharness" / name).read_bytes()).hexdigest() != build_metadata["broker_sources"][name]:
            raise ValueError("Broker source differs from the candidate image")
    interpreter = "/var/lib/omniharness-m4-preview/broker-venv/bin/python"
    wsl("test", "-x", interpreter)
    args = ["wsl.exe", "-d", DISTRO, "-u", "root", "--exec", "env",
            "OMNI_BROKER_PROFILE=" + mapped(ROOT / "deploy/v6-preview.broker.json"),
            interpreter, "-m", "uvicorn", "omniharness.workspace_broker:create_deployment_app",
            "--factory", "--app-dir", mapped(ROOT / "src"), "--host", "172.17.0.1", "--port", "19102",
            "--no-access-log"]
    out = (PRIVATE / "logs/broker.stdout.log").open("ab")
    err = (PRIVATE / "logs/broker.stderr.log").open("ab")
    try:
        process = subprocess.Popen(args, stdout=out, stderr=err,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        out.close()
        err.close()
    _exclusive(PRIVATE / "broker-wsl.pid", str(process.pid).encode())
    print("Dedicated broker launched; health must be verified separately.")


def up() -> None:
    if not (PRIVATE / "configs-imported.json").is_file() or not (PRIVATE / "broker-wsl.pid").is_file():
        raise ValueError("Preview configuration import and broker startup are required first")
    _verify_interactive_start()
    compose("up", "-d", "--wait", "--wait-timeout", "180", "api", "worker")
    print("Isolated V6 preview services started; perform health and authentication checks.")


def status() -> None:
    for line in compose("ps", "--format", "json").decode().splitlines():
        row = json.loads(line)
        print(json.dumps({key: row.get(key) for key in ("Name", "State", "Health", "Ports")}))


def fixture_policy_upgrade(db, actual_identity, instance_id, *, apply=True):
    """Append one fixture-only grant; caller holds the transaction and takes all entry points offline."""
    from sqlalchemy import select

    from omniharness.models import (
        Approval,
        OperationRecord,
        Run,
        ToolOperation,
        Workspace,
        WorkspaceLease,
        WorkspacePolicy,
    )
    from omniharness.services import TERMINAL, parameters_digest

    if instance_id != "omni-v6-preview":
        raise ValueError("wrong instance")
    if db.scalar(select(Run.id).where(Run.status.not_in(TERMINAL)).limit(1)):
        raise ValueError("preview has an active or queued run")
    if db.scalar(select(WorkspaceLease.workspace_id).limit(1)):
        raise ValueError("preview has a workspace lease")
    terminal_tools = ("completed", "failed", "rejected", "cancelled")
    if db.scalar(select(ToolOperation.id).where(
            ToolOperation.status.not_in(terminal_tools) | ToolOperation.slot_claimed).limit(1)):
        raise ValueError("preview has an unsettled tool")
    if db.scalar(select(OperationRecord.operation_id).where(
            OperationRecord.node_id == "agent:execute:tool_calls",
            OperationRecord.status.in_(("running", "uncertain"))).limit(1)):
        raise ValueError("preview has an unsettled tool ledger")
    if db.scalar(select(Approval.id).where(Approval.status == "pending").limit(1)):
        raise ValueError("preview has a pending approval")
    workspaces = list(db.scalars(select(Workspace).with_for_update()))
    if len(workspaces) != 1:
        raise ValueError("expected exactly one fixture workspace")
    row = workspaces[0]
    path = r"G:\OmniHarness-V6-Preview-Fixture"
    def normalize(value):
        return str(value).replace("/", "\\").rstrip("\\").casefold()
    if not row.enabled or normalize(row.host_path) != normalize(path) or row.policy_version not in (1, 2):
        raise ValueError("fixture workspace or policy changed")
    identity = row.probe.get("identity") if isinstance(row.probe, dict) else None
    if not isinstance(identity, dict) or not isinstance(actual_identity, dict):
        raise ValueError("fixture identity missing")
    if (identity.get("filesystem") != "NTFS" or actual_identity.get("filesystem") != "NTFS"
            or actual_identity.get("directory") is not True
            or any(identity.get(key) is None or identity.get(key) != actual_identity.get(key)
                   for key in ("volume_serial", "file_id"))):
        raise ValueError("fixture physical identity changed")
    if row.physical_key != parameters_digest({k: identity.get(k) for k in ("volume_serial", "device", "file_id")}):
        raise ValueError("fixture physical key changed")
    versions = list(db.scalars(select(WorkspacePolicy).where(WorkspacePolicy.workspace_id == row.id)))
    old = db.get(WorkspacePolicy, (row.id, 1))
    if old is None or {item.version for item in versions} != set(range(1, row.policy_version + 1)):
        raise ValueError("fixture policy version mismatch")
    grant = copy.deepcopy(old.grant)
    if (not isinstance(grant, dict) or grant.get("workspace_id") != row.id
            or grant.get("instance_id") != instance_id or normalize(grant.get("root")) != normalize(path)
            or grant.get("version") != 1 or grant.get("write_allow") != []
            or grant.get("expected_identity") != identity or grant.get("enabled") is not True):
        raise ValueError("fixture grant changed")
    if (not isinstance(old.tool_policy, dict) or old.tool_policy.get("network") != "deny"
            or old.tool_policy.get("snapshot_writeback") is not False):
        raise ValueError("fixture tool policy changed")
    expected_v2 = {**grant, "version": 2, "write_allow": ["."]}
    if row.policy_version == 2:
        current = db.get(WorkspacePolicy, (row.id, 2))
        if current is None or current.grant != expected_v2 or current.tool_policy != old.tool_policy:
            raise ValueError("fixture v2 grant changed")
    elif apply:
        grant = expected_v2
        db.add(WorkspacePolicy(workspace_id=row.id, version=2, grant=grant,
                               tool_policy=copy.deepcopy(old.tool_policy)))
        row.policy_version = 2
        db.flush()
    return {"workspace_id": row.id, "old_version": 1, "new_version": 2}


def _broker_offline() -> None:
    # Check both listener and exact profile in process environment. Failure to
    # inspect /proc is an error; do not treat it as proof of being offline.
    code = r'''import os,socket,sys
needle=b'OMNI_BROKER_PROFILE=/mnt/g/harness/deploy/v6-preview.broker.json'
for name in os.listdir('/proc'):
 if not name.isdigit(): continue
 try:
  env=open('/proc/'+name+'/environ','rb').read().split(b'\0')
 except FileNotFoundError: continue
 except PermissionError: sys.exit(2)
 if needle in env: sys.exit(3)
s=socket.socket(); s.settimeout(1)
try:
 if s.connect_ex(('172.17.0.1',19102)) == 0: sys.exit(4)
finally: s.close()
'''
    wsl("python3", "-c", code, timeout=15)


def _broker_online() -> None:
    """Require the expected preview broker process and its listener to be live."""
    code = r'''import json,os,socket,sys,urllib.request
needle=b'OMNI_BROKER_PROFILE=/mnt/g/harness/deploy/v6-preview.broker.json'
expected=['/var/lib/omniharness-m4-preview/broker-venv/bin/python','-m','uvicorn',
 'omniharness.workspace_broker:create_deployment_app','--factory','--app-dir',
 '/mnt/g/harness/src','--host','172.17.0.1','--port','19102','--no-access-log']
matches=[]
for name in os.listdir('/proc'):
 if not name.isdigit(): continue
 try:
  env=open('/proc/'+name+'/environ','rb').read().split(b'\0')
  args=open('/proc/'+name+'/cmdline','rb').read().rstrip(b'\0').decode().split('\0')
 except FileNotFoundError: continue
 except PermissionError: sys.exit(2)
 if needle in env:
  if args != expected: sys.exit(3)
  matches.append(name)
if len(matches)!=1: sys.exit(4)
s=socket.socket(); s.settimeout(1)
try:
 if s.connect_ex(('172.17.0.1',19102)) != 0: sys.exit(5)
finally: s.close()
try:
 key=sys.stdin.buffer.read().decode().strip()
 request=urllib.request.Request('http://172.17.0.1:19102/health', headers={'Authorization':'Bearer '+key,'X-Omni-Instance':'omni-v6-preview'})
 with urllib.request.urlopen(request,timeout=2) as response:
  health=json.load(response)
 if health.get('status')!='ok' or health.get('instance_id')!='omni-v6-preview' or health.get('service')!='workspace-broker': sys.exit(6)
except Exception: sys.exit(7)
'''
    wsl("python3", "-c", code, data=_file("workspace-service.key").read_bytes(), timeout=15)


def _preview_offline() -> None:
    _assert_project_root()
    rows = [json.loads(line) for line in compose("ps", "--all", "--format", "json").decode().splitlines()]
    by_service = {row.get("Service"): row for row in rows}
    if any(by_service.get(name, {}).get("State") not in {"exited", "created", "dead"} for name in ("api", "worker")):
        raise ValueError("preview API and Worker must be stopped first")
    if by_service.get("postgres", {}).get("State") != "running":
        raise ValueError("preview PostgreSQL must be running")
    _broker_offline()


def stop_preview_broker() -> None:
    """Stop only the exact 19102/profile broker using a Linux pidfd."""
    _assert_project_root()
    rows = [json.loads(line) for line in compose("ps", "--all", "--format", "json").decode().splitlines()]
    states = {row.get("Service"): row.get("State") for row in rows}
    if any(states.get(name) not in {"exited", "created", "dead"} for name in ("api", "worker")):
        raise ValueError("stop preview API and Worker before the broker")
    code = r'''import os,select,signal,socket,sys
needle=b'OMNI_BROKER_PROFILE=/mnt/g/harness/deploy/v6-preview.broker.json'
expected=['/var/lib/omniharness-m4-preview/broker-venv/bin/python','-m','uvicorn',
 'omniharness.workspace_broker:create_deployment_app','--factory','--app-dir',
 '/mnt/g/harness/src','--host','172.17.0.1','--port','19102','--no-access-log']
matches=[]
for name in os.listdir('/proc'):
 if not name.isdigit(): continue
 try:
  env=open('/proc/'+name+'/environ','rb').read().split(b'\0')
  args=open('/proc/'+name+'/cmdline','rb').read().rstrip(b'\0').decode().split('\0')
 except FileNotFoundError: continue
 except PermissionError: sys.exit(2)
 if needle in env:
  if args != expected: sys.exit(3)
  matches.append(int(name))
if len(matches)!=1 or not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'): sys.exit(4)
pid=matches[0]; fd=os.pidfd_open(pid)
try:
 env=open('/proc/'+str(pid)+'/environ','rb').read().split(b'\0')
 args=open('/proc/'+str(pid)+'/cmdline','rb').read().rstrip(b'\0').decode().split('\0')
 if needle not in env or args != expected: sys.exit(7)
except (FileNotFoundError,PermissionError): sys.exit(8)
signal.pidfd_send_signal(fd,signal.SIGTERM)
p=select.poll(); p.register(fd,select.POLLIN)
if not p.poll(10000): sys.exit(5)
os.close(fd)
s=socket.socket(); s.settimeout(1)
try:
 if s.connect_ex(('172.17.0.1',19102)) == 0: sys.exit(6)
finally: s.close()
'''
    wsl("python3", "-c", code, timeout=20)
    _broker_offline()
    print("Only the verified V6 preview broker was stopped; its PID marker was retained.")


def quiesce_preview() -> None:
    """Block new requests, recheck activity, then stop only preview execution services."""
    audit_preview()
    backup_dir = _backup_preview_data()
    print(json.dumps({"preflight_backup": backup_dir.name}))
    audit_preview()
    try:
        compose("stop", "--timeout", "30", "api")
    except BaseException:
        # The CLI can fail after the container actually stopped. Restore only
        # the previous API state after confirming the other two components.
        try:
            rows = [json.loads(line) for line in compose("ps", "--all", "--format", "json").decode().splitlines()]
            states = {row.get("Service"): row.get("State") for row in rows}
            if states.get("worker") != "running" or states.get("api") not in {"running", "exited"}:
                raise ValueError("Preview service state is unknown")
            _broker_online()
            if states["api"] == "exited":
                compose("start", "api")
            print("Preview API state was restored; Worker was not interrupted.")
        except Exception:
            print("Preview service state requires inspection; no automatic start was attempted.")
        raise
    try:
        # API is offline; a second point-in-time check catches requests that
        # arrived between the first check and stopping the API.
        audit_preview(api_stopped=True)
    except BaseException:
        compose("start", "api")
        raise
    try:
        compose("stop", "--timeout", "30", "worker")
        stop_preview_broker()
        _preview_offline()
    except BaseException:
        try:
            _broker_online()
        except Exception:
            print("Preview remains stopped; verify broker state before recovery. "
                  f"If broker is confirmed offline, use resume-quiesce-broker --backup {backup_dir.name}, then up.")
        else:
            try:
                compose("start", "api", "worker")
                print("The verified existing broker remained online; preview API and Worker were restarted.")
            except Exception:
                print("Preview restart failed; check service status before retrying.")
        raise
    print("Preview execution services are offline; PostgreSQL remains available.")


def _run_policy_transaction(actual_identity: dict, *, apply: bool = True) -> dict:
    # Source is embedded into a one-off preview container; the Docker command
    # carries no database URL, provider keys, or raw exception text.
    source = "import copy,json,sys\n" + inspect.getsource(fixture_policy_upgrade) + r'''
from omniharness.db import make_engine
from omniharness.settings import Settings
from sqlalchemy.orm import Session
try:
 settings=Settings()
 if not settings.v6_preview or settings.instance_id != 'omni-v6-preview': raise ValueError('wrong preview')
 engine=make_engine(settings)
 payload=json.load(sys.stdin)
 with Session(engine) as db, db.begin():
  result=fixture_policy_upgrade(db,payload['identity'],settings.instance_id,apply=payload['apply'])
 print(json.dumps(result))
except Exception:
 sys.exit(1)
'''
    raw = compose("run", "--rm", "--no-deps", "-T", "api", "python", "-c", source,
                  data=json.dumps({"identity": actual_identity, "apply": apply}).encode())
    result = json.loads(raw)
    if result.get("old_version") != 1 or result.get("new_version") != 2:
        raise ValueError("unexpected policy result")
    return result


def audit_preview(*, api_stopped: bool = False) -> None:
    """Read-only point-in-time check, before quiescing the preview."""
    _assert_project_root()
    profile = (PRIVATE / "secrets" / "compose.env").read_bytes()
    _verify_compose_profile(profile)
    _verify_live_preview(profile, api_stopped=api_stopped)
    image = _profile_fields(profile).get("OMNI_V6_IMAGE")
    rows = [json.loads(line) for line in compose("ps", "--all", "--format", "json").decode().splitlines()]
    for name in ("api", "worker"):
        matches = [row for row in rows if row.get("Service") == name]
        expected_state = "exited" if name == "api" and api_stopped else "running"
        if len(matches) != 1 or matches[0].get("State") != expected_state or matches[0].get("Image") != image:
            raise ValueError("Running preview image or service state differs from the private profile")
    from omniharness.workspace_io import native_path_identity
    result = _run_policy_transaction(native_path_identity(str(FIXTURE)), apply=False)
    print(json.dumps({"workspace_id": result["workspace_id"], "result": "quiescent-at-check"}))


def _regular_tree(root: Path, *, required: bool = False) -> list[tuple[Path, Path, os.stat_result]]:
    items = []
    def check(path: Path):
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or not path.resolve().is_relative_to(PRIVATE.resolve())):
            raise ValueError("Preview backup contains an unsafe filesystem object")
        return info
    try:
        relative = root.relative_to(PRIVATE)
    except ValueError:
        raise ValueError("Preview backup root is outside the isolated directory") from None
    ancestor = PRIVATE
    check(ancestor)
    for part in relative.parts:
        ancestor = ancestor / part
        try:
            check(ancestor)
        except FileNotFoundError:
            if required:
                raise ValueError("Required preview data directory is unavailable") from None
            return []
    root_info = check(root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("Preview backup root is not a directory")
    items.append((root, root, root_info))
    for current, dirs, files in os.walk(root, followlinks=False, onerror=lambda _: (_ for _ in ()).throw(
            ValueError("Preview backup cannot inspect its data"))):
        current_path = Path(current)
        check(current_path)
        dirs.sort()
        files.sort()
        for name in dirs:
            info = check(current_path / name)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Preview backup contains a non-directory")
            items.append((root, current_path / name, info))
        for name in files:
            path = current_path / name
            info = check(path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Preview backup contains an unsafe file")
            items.append((root, path, info))
    return items


def _stable_stat(path: Path, previous: os.stat_result) -> None:
    current = path.lstat()
    if (stat.S_ISLNK(current.st_mode) or getattr(current, "st_file_attributes", 0) & 0x400
            or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_nlink) !=
               (previous.st_dev, previous.st_ino, previous.st_size, previous.st_mtime_ns, previous.st_nlink)):
        raise ValueError("Preview backup data changed while being captured")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _linux_snapshot_inventory() -> list[bytes]:
    source = r'''import json,os,stat,sys
root=sys.argv[1]
items=[]
def visit(path):
 st=os.lstat(path)
 if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
  raise ValueError()
 if stat.S_ISREG(st.st_mode) and st.st_nlink != 1: raise ValueError()
 items.append([os.path.relpath(path,root),st.st_dev,st.st_ino,st.st_mode,st.st_size,st.st_mtime_ns,st.st_nlink])
 if stat.S_ISDIR(st.st_mode):
  with os.scandir(path) as children:
   names=sorted(child.name for child in children)
  for name in names: visit(os.path.join(path,name))
try:
 visit(root)
 print(json.dumps(items,ensure_ascii=True))
except Exception:
 sys.exit(1)
'''
    rows = json.loads(wsl("python3", "-c", source, LINUX_ROOT + "/snapshots"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("Could not inspect isolated snapshot files")
    return [json.dumps(row, ensure_ascii=True).encode() for row in rows]


def _check_tar(path: Path) -> None:
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            if (not name or name.startswith("/") or ".." in name.split("/")
                    or not (name == "snapshots" or name.startswith("snapshots/"))
                    or not (member.isfile() or member.isdir())):
                raise ValueError("Snapshot backup contains an unsafe archive member")
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Cannot verify snapshot backup member")
                while stream.read(1024 * 1024):
                    pass


def _backup_preview_data(*, include_data: bool = False) -> Path:
    profile = (PRIVATE / "secrets/compose.env").read_bytes()
    _verify_compose_profile(profile)
    _verify_live_preview(profile, api_stopped=include_data, worker_stopped=include_data)
    if include_data:
        _preview_offline()
    trees = [("data", PRIVATE / "data", True), ("native/recovery", PRIVATE / "native" / "recovery", False),
             ("native/snapshots", PRIVATE / "native" / "snapshots", False)] if include_data else []
    files = [(label, root, path, info) for label, root, required in trees
             for _, path, info in _regular_tree(root, required=required)]
    pg_size = int(compose("exec", "-T", "postgres", "psql", "-U", "omniharness", "-d", "omniharness",
                          "-At", "-c", "SELECT pg_database_size(current_database())").strip())
    snapshot_size = int(wsl("du", "-sb", LINUX_ROOT + "/snapshots").split(None, 1)[0]) if include_data else 0
    estimated = pg_size + snapshot_size + sum(info.st_size for _, _, _, info in files if stat.S_ISREG(info.st_mode))
    if pg_size < 0 or snapshot_size < 0 or shutil.disk_usage(PRIVATE / "secrets").free < estimated * 2 + 64 * 1024 * 1024:
        raise ValueError("Insufficient verified space for the preview backup")
    backup_dir = PRIVATE / "secrets" / ("upgrade-" + secrets.token_hex(8))
    backup_dir.mkdir(exist_ok=False)
    # The env and database can contain secrets; both remain in the restricted
    # preview secret directory. The binary never enters a command argument.
    _exclusive(backup_dir / "compose.env", profile)
    dump = compose("exec", "-T", "postgres", "pg_dump", "-U", "omniharness", "-Fc", "omniharness")
    if not dump:
        raise ValueError("empty preview database backup")
    _exclusive(backup_dir / "database.dump", dump)
    compose("exec", "-T", "postgres", "pg_restore", "-l", data=dump)
    manifest = {"profile_sha256": hashlib.sha256(profile).hexdigest(),
                "database_sha256": hashlib.sha256(dump).hexdigest(), "instance": PROJECT}
    if include_data:
        archive_path = backup_dir / "preview-data.zip"
        entries = {}
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for label, root, path, info in files:
                is_directory = stat.S_ISDIR(info.st_mode)
                relative = path.relative_to(root).as_posix()
                name = label + "/" + ("" if relative == "." else relative)
                if is_directory and not name.endswith("/"):
                    name += "/"
                _stable_stat(path, info)
                archive.write(path, name)
                _stable_stat(path, info)
                entries[name] = {"size": 0 if is_directory else info.st_size,
                                 "sha256": hashlib.sha256(b"").hexdigest() if is_directory else _sha256_file(path)}
                _stable_stat(path, info)
        with zipfile.ZipFile(archive_path, "r") as archive:
            if archive.testzip() or set(archive.namelist()) != set(entries):
                raise ValueError("Preview data backup failed integrity verification")
            for name, metadata in entries.items():
                digest = hashlib.sha256()
                with archive.open(name) as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != metadata["sha256"]:
                    raise ValueError("Preview data backup differs from its source")
        captured = {(label, str(path)): (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                                          info.st_mtime_ns, info.st_nlink) for label, _, path, info in files}
        present = {(label, str(path)): (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                                         info.st_mtime_ns, info.st_nlink)
                   for label, root, required in trees for _, path, info in _regular_tree(root, required=required)}
        if present != captured:
            raise ValueError("Preview backup data changed while being captured")
        before = _linux_snapshot_inventory()
        tar_path = backup_dir / "snapshot-data.tar"
        with tar_path.open("xb") as stream:
            result = subprocess.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--exec", "tar", "-C", LINUX_ROOT,
                                     "-cf", "-", "snapshots"], stdout=stream, stderr=subprocess.PIPE,
                                    timeout=600, check=False)
        if result.returncode or before != _linux_snapshot_inventory():
            raise ValueError("Isolated snapshots changed or could not be backed up")
        _check_tar(tar_path)
        data_manifest = json.dumps(entries, sort_keys=True).encode()
        _exclusive(backup_dir / "data-manifest.json", data_manifest)
        manifest.update({"data_archive_sha256": _sha256_file(archive_path),
                         "data_manifest_sha256": hashlib.sha256(data_manifest).hexdigest(),
                         "snapshot_archive_sha256": _sha256_file(tar_path)})
        _preview_offline()
    if (PRIVATE / "secrets/compose.env").read_bytes() != profile:
        raise ValueError("Preview profile changed while backup was being captured")
    _verify_compose_profile(profile)
    _exclusive(backup_dir / "backup.json", json.dumps(manifest).encode())
    return backup_dir


def backup_and_grant_fixture() -> None:
    """Offline-only one-time policy migration with a verified preview-only backup."""
    _preview_offline()
    from omniharness.workspace_io import native_path_identity
    identity = native_path_identity(str(FIXTURE))
    backup_dir = _backup_preview_data(include_data=True)
    _preview_offline()
    result = _run_policy_transaction(identity)
    _exclusive(backup_dir / "policy-result.json", json.dumps(result).encode())
    print(json.dumps({"backup": backup_dir.name, "workspace_id": result["workspace_id"], "policy_version": 2}))


def _named_directory(parent: Path, name: str, prefix: str) -> Path:
    if not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{16}", name or ""):
        raise ValueError("invalid isolated preview record")
    root = parent.resolve(strict=True)
    path = parent / name
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != root:
        raise ValueError("isolated preview record unavailable")
    return path


def _read_verified_backup(name: str, *, require_policy: bool = True) -> tuple[Path, bytes]:
    directory = _named_directory(PRIVATE / "secrets", name, "upgrade-")
    manifest = json.loads((directory / "backup.json").read_text(encoding="utf-8"))
    profile = (directory / "compose.env").read_bytes()
    dump = (directory / "database.dump").read_bytes()
    if (manifest.get("instance") != PROJECT or not dump
            or manifest.get("profile_sha256") != hashlib.sha256(profile).hexdigest()
            or manifest.get("database_sha256") != hashlib.sha256(dump).hexdigest()
            or (require_policy and not (directory / "policy-result.json").is_file())):
        raise ValueError("preview backup verification failed")
    if require_policy:
        archive_path = directory / "preview-data.zip"
        data_manifest_path = directory / "data-manifest.json"
        tar_path = directory / "snapshot-data.tar"
        if (any(path.is_symlink() or not path.is_file() for path in (archive_path, data_manifest_path, tar_path))
                or manifest.get("data_archive_sha256") != _sha256_file(archive_path)
                or manifest.get("data_manifest_sha256") != _sha256_file(data_manifest_path)
                or manifest.get("snapshot_archive_sha256") != _sha256_file(tar_path)):
            raise ValueError("Preview data backup verification failed")
        entries = json.loads(data_manifest_path.read_text(encoding="utf-8"))
        if not isinstance(entries, dict):
            raise ValueError("Preview data backup manifest is invalid")
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(entries) or archive.testzip():
                raise ValueError("Preview data archive failed verification")
            for item in archive.infolist():
                name = item.filename
                if (name.startswith("/") or ".." in name.split("/") or "\\" in name
                        or not name.startswith(("data/", "native/recovery/", "native/snapshots/"))):
                    raise ValueError("Preview data archive contains an unsafe path")
                digest = hashlib.sha256()
                with archive.open(item) as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                metadata = entries[name]
                if (not isinstance(metadata, dict) or item.file_size != metadata.get("size")
                        or digest.hexdigest() != metadata.get("sha256")):
                    raise ValueError("Preview data archive differs from its manifest")
        _check_tar(tar_path)
    return directory, profile


def _image_id(image: str) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image or ""):
        raise ValueError("image must be a pinned local image identity")
    found = wsl("docker", "image", "inspect", image, "--format", "{{.Id}}").decode().strip()
    if found != image:
        raise ValueError("pinned local image does not match")


def _replace_profile(profile: bytes, app_image: str, tool_image: str) -> bytes:
    _image_id(app_image)
    _image_id(tool_image)
    lines = profile.decode("utf-8").splitlines(keepends=True)
    replacements = {"OMNI_V6_IMAGE": app_image, "OMNI_V6_TOOL_IMAGE": tool_image,
                    "OMNI_INTERACTIVE_PREVIEW": "true"}
    seen = set()
    result = []
    for line in lines:
        key = line.partition("=")[0]
        if key in replacements:
            if key in seen:
                raise ValueError("duplicate image entry in preview profile")
            if key == "OMNI_INTERACTIVE_PREVIEW" and line.partition("=")[2].strip().lower() not in {"false", "true"}:
                raise ValueError("invalid interactive preview setting")
            seen.add(key)
            result.append(f"{key}={replacements[key]}\n")
        else:
            result.append(line)
    if not {"OMNI_V6_IMAGE", "OMNI_V6_TOOL_IMAGE"}.issubset(seen):
        raise ValueError("preview profile lacks image entries")
    if "OMNI_INTERACTIVE_PREVIEW" not in seen:
        if result and not result[-1].endswith("\n"):
            result[-1] += "\n"
        result.append("OMNI_INTERACTIVE_PREVIEW=true\n")
    return "".join(result).encode("utf-8")


def _atomic_profile(data: bytes) -> None:
    target = PRIVATE / "secrets/compose.env"
    staging = target.parent / (".compose-" + secrets.token_hex(8))
    _exclusive(staging, data)
    os.replace(staging, target)


def _migration_revision_state() -> dict:
    """Query the candidate image's Alembic head and this preview DB revision."""
    source = r'''import json,sys
try:
 from pathlib import Path
 from alembic.config import Config
 from alembic.script import ScriptDirectory
 from sqlalchemy import text
 import omniharness
 from omniharness.db import make_engine
 from omniharness.settings import Settings
 settings=Settings()
 if not settings.v6_preview or not settings.interactive_preview or settings.instance_id != 'omni-v6-preview': raise ValueError()
 config=Config()
 config.set_main_option('script_location',str(Path(omniharness.__file__).parent/'migrations'))
 heads=sorted(ScriptDirectory.from_config(config).get_heads())
 engine=make_engine(settings)
 with engine.connect() as connection:
  revisions=sorted(connection.scalars(text('SELECT version_num FROM alembic_version')).all())
 engine.dispose()
 print(json.dumps({'heads':heads,'revisions':revisions}))
except Exception:
 sys.exit(1)
'''
    value = json.loads(compose("run", "--rm", "--no-deps", "-T", "api", "python", "-c", source))
    if (set(value) != {"heads", "revisions"} or not isinstance(value["heads"], list)
            or not value["heads"] or value["heads"] != value["revisions"]
            or any(not isinstance(item, str) or not item for item in value["heads"])):
        raise ValueError("Preview database revision does not match candidate migrations")
    return value


def _interactive_profile(profile: bytes) -> bool:
    values = [line.partition("=")[2].strip().lower() for line in profile.decode("utf-8").splitlines()
              if line.partition("=")[0] == "OMNI_INTERACTIVE_PREVIEW"]
    if len(values) > 1 or (values and values[0] not in {"true", "false"}):
        raise ValueError("Invalid interactive preview profile")
    return bool(values and values[0] == "true")


def _verify_compose_profile(profile: bytes) -> None:
    """Reject environment overrides without printing the expanded secrets."""
    declared = _profile_fields(profile)
    config = json.loads(compose("config", "--format", "json"))
    if (config.get("name") != PROJECT
            or set(config.get("services", {})) != {"api", "worker", "init", "postgres", "sandbox-controller"}
            or set(config.get("networks", {})) != {"default"}
            or config["networks"]["default"].get("name") != PROJECT + "_default"
            or config["networks"]["default"].get("external") not in (None, False)
            or config["networks"]["default"].get("driver_opts") not in (None, {})
            or set(config.get("volumes", {})) != {"preview-postgres"}
            or config["volumes"]["preview-postgres"].get("name") != PROJECT + "_preview-postgres"
            or config["volumes"]["preview-postgres"].get("external") not in (None, False)
            or config["volumes"]["preview-postgres"].get("driver_opts") not in (None, {})):
        raise ValueError("Effective preview project, network or volume differs")
    for service in config["services"].values():
        if (set(service.get("networks", {})) != {"default"}
                or any(service.get(k) not in (None, False, [], {}) for k in
                       ("entrypoint", "cap_add", "pid", "ipc", "devices", "privileged", "network_mode"))):
            raise ValueError("Effective preview service isolation differs")
    expected_db = ("postgresql+psycopg://omniharness:" + declared.get("OMNI_V6_POSTGRES_PASSWORD", "")
                   + "@postgres:5432/omniharness")
    if not declared.get("OMNI_V6_POSTGRES_PASSWORD"):
        raise ValueError("Missing preview database authentication")

    def volumes(service):
        return {(v.get("type"), v.get("source"), v.get("target"), bool(v.get("read_only", False)))
                for v in service.get("volumes", [])}

    def ports(service):
        return {(str(p.get("host_ip", "")), str(p.get("published", "")), str(p.get("target", "")),
                 p.get("protocol", "tcp")) for p in service.get("ports", [])}

    for name in ("api", "worker", "init"):
        service = config.get("services", {}).get(name, {})
        env = service.get("environment", {})
        if (service.get("command") != ["omniharness", {"api":"serve", "worker":"worker", "init":"bootstrap"}[name]]
                or service.get("user") != "1000:1000" or service.get("init") is not True
                or service.get("cap_drop") != ["ALL"] or service.get("security_opt") != ["no-new-privileges:true"]
                or service.get("extra_hosts") != ["host.docker.internal=host-gateway"]
                or service.get("image") != declared.get("OMNI_V6_IMAGE")
                or not declared.get("OMNI_V6_TOOL_IMAGE")
                or str(env.get("OMNI_SANDBOX_IMAGE")) != declared.get("OMNI_V6_TOOL_IMAGE")
                or env.get("OMNI_DATABASE_URL") != expected_db
                or env.get("OMNI_CHECKPOINT_SCHEMA") != "omniharness_v6_checkpoints"
                or env.get("OMNI_DATA_DIR") != "/data" or env.get("OMNI_SECRET_DIR") != "/secrets"
                or env.get("OMNI_WORKSPACE_SERVICE_KEY_FILE") != "/secrets/workspace-service.key"
                or env.get("OMNI_SANDBOX_KEY_FILE") != "/secrets/controller.key"
                or env.get("OMNI_WORKSPACE_BROKER_URL") != "http://host.docker.internal:19102"
                or env.get("OMNI_SANDBOX_ENDPOINT") != "http://sandbox-controller:8080"
                or env.get("OMNI_SANDBOX_SNAPSHOT_ROOT") != LINUX_ROOT + "/snapshots"
                or env.get("OMNI_ALLOWED_ORIGINS") != "http://localhost:8788"
                or str(env.get("OMNI_ENABLE_AGENT")).lower() != "true"
                or str(env.get("OMNI_ENABLE_TOOLS")).lower() != "true"
                or str(env.get("OMNI_INSTANCE_ID")) != PROJECT
                or str(env.get("OMNI_V6_PREVIEW")).lower() != "true"
                or str(env.get("OMNI_INTERACTIVE_PREVIEW")).lower() !=
                   str(declared.get("OMNI_INTERACTIVE_PREVIEW", "false")).lower()
                or volumes(service) != {("bind", declared.get("OMNI_V6_DATA_DIR"), "/data", False),
                                        ("bind", declared.get("OMNI_V6_SECRET_DIR"), "/secrets", name != "init")}
                or ports(service) != ({("127.0.0.1", "8788", "8787", "tcp")} if name == "api" else set())):
            raise ValueError("Effective preview configuration differs from its private profile")
    postgres = config.get("services", {}).get("postgres", {})
    controller = config.get("services", {}).get("sandbox-controller", {})
    pg_env = postgres.get("environment", {})
    if (postgres.get("image") != "docker.m.daocloud.io/library/postgres@sha256:665dc7b37ac52a17e298511414c21d23ffa4e6a6ef6225b3e5d59c7afb35e994"
            or controller.get("image") != "docker.m.daocloud.io/opensandbox/server@sha256:25bebdcf1cf7724eb923faecbdbbf82d4038c6ab35c736151574726d14006f30"
            or postgres.get("command") is not None or controller.get("command") != ["--config", "/config.toml"]
            or pg_env.get("POSTGRES_DB") != "omniharness" or pg_env.get("POSTGRES_USER") != "omniharness"
            or pg_env.get("POSTGRES_PASSWORD") != declared.get("OMNI_V6_POSTGRES_PASSWORD")
            or ports(postgres) or volumes(postgres) != {("volume", "preview-postgres", "/var/lib/postgresql/data", False)}
            or ports(controller) != {("127.0.0.1", "19100", "8080", "tcp")}
            or volumes(controller) != {("bind", "/var/run/docker.sock", "/var/run/docker.sock", False),
                ("bind", declared.get("OMNI_V6_CONTROLLER_CONFIG"), "/config.toml", True),
                ("bind", LINUX_ROOT + "/controller", "/var/lib/opensandbox", False),
                ("bind", LINUX_ROOT + "/snapshots", LINUX_ROOT + "/snapshots", True)}):
        raise ValueError("Effective preview persistence or controller mounts differ")


def _verify_live_preview(profile: bytes, *, api_stopped: bool = False, worker_stopped: bool = False,
                         infrastructure_only: bool = False) -> None:
    """Validate existing containers and ownership without exposing inspect data."""
    fields = _profile_fields(profile)
    postgres_image = "docker.m.daocloud.io/library/postgres@sha256:665dc7b37ac52a17e298511414c21d23ffa4e6a6ef6225b3e5d59c7afb35e994"
    controller_image = "docker.m.daocloud.io/opensandbox/server@sha256:25bebdcf1cf7724eb923faecbdbbf82d4038c6ab35c736151574726d14006f30"
    expected_images = {"api": fields.get("OMNI_V6_IMAGE"), "worker": fields.get("OMNI_V6_IMAGE"),
                       "postgres": postgres_image, "sandbox-controller": controller_image}
    names = ("postgres", "sandbox-controller") if infrastructure_only else tuple(expected_images)
    for name in names:
        identifiers = compose("ps", "--all", "-q", name).decode().split()
        if len(identifiers) != 1 or not re.fullmatch(r"[a-f0-9]{12,64}", identifiers[0]):
            raise ValueError("Cannot identify one owned preview service")
        rows = json.loads(wsl("docker", "inspect", identifiers[0]))
        expected_image = wsl("docker", "image", "inspect", expected_images[name], "--format", "{{.Id}}").decode().strip()
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("Cannot inspect one owned preview service")
        row = rows[0]
        runtime_config = row.get("Config", {})
        labels = runtime_config.get("Labels") or {}
        raw_env = runtime_config.get("Env")
        if not isinstance(raw_env, list):
            raise ValueError("Existing preview container environment differs")
        runtime_env = {}
        for entry in raw_env:
            if not isinstance(entry, str):
                raise ValueError("Existing preview container environment differs")
            key, separator, value = entry.partition("=")
            if not separator or not key or key in runtime_env:
                raise ValueError("Existing preview container environment differs")
            runtime_env[key] = value
        password = fields.get("OMNI_V6_POSTGRES_PASSWORD")
        if not password:
            raise ValueError("Existing preview container environment differs")
        if name in {"api", "worker"}:
            expected_env = {
                "OMNI_DATABASE_URL": "postgresql+psycopg://omniharness:" + password + "@postgres:5432/omniharness",
                "OMNI_INSTANCE_ID": PROJECT,
                "OMNI_V6_PREVIEW": "true",
                "OMNI_INTERACTIVE_PREVIEW": fields.get("OMNI_INTERACTIVE_PREVIEW", "false"),
                "OMNI_ENABLE_AGENT": "true",
                "OMNI_ENABLE_TOOLS": "true",
                "OMNI_ALLOWED_ORIGINS": "http://localhost:8788",
                "OMNI_DATA_DIR": "/data",
                "OMNI_SECRET_DIR": "/secrets",
                "OMNI_WORKSPACE_SERVICE_KEY_FILE": "/secrets/workspace-service.key",
                "OMNI_SANDBOX_KEY_FILE": "/secrets/controller.key",
                "OMNI_WORKSPACE_BROKER_URL": "http://host.docker.internal:19102",
                "OMNI_SANDBOX_ENDPOINT": "http://sandbox-controller:8080",
                "OMNI_SANDBOX_IMAGE": fields.get("OMNI_V6_TOOL_IMAGE"),
                "OMNI_SANDBOX_SNAPSHOT_ROOT": LINUX_ROOT + "/snapshots",
                "OMNI_CHECKPOINT_SCHEMA": "omniharness_v6_checkpoints",
            }
        elif name == "postgres":
            expected_env = {"POSTGRES_DB": "omniharness", "POSTGRES_USER": "omniharness",
                            "POSTGRES_PASSWORD": password}
        else:
            expected_env = {}
        for key, value in expected_env.items():
            actual = runtime_env.get(key)
            # The original running preview predates this optional setting;
            # Settings.interactive_preview defaults to False when omitted.
            if key == "OMNI_INTERACTIVE_PREVIEW" and actual is None and value == "false":
                actual = "false"
            if value is None or actual != value:
                raise ValueError("Existing preview container environment differs")
        expected_state = ("exited" if (name == "api" and api_stopped) or
                          (name == "worker" and worker_stopped) else "running")
        if (labels.get("com.docker.compose.project") != PROJECT or labels.get("com.docker.compose.service") != name
                or row.get("Image") != expected_image or row.get("State", {}).get("Status") != expected_state
                or row.get("HostConfig", {}).get("Privileged") is not False
                or row.get("HostConfig", {}).get("NetworkMode") != PROJECT + "_default"
                or set(row.get("NetworkSettings", {}).get("Networks", {})) != {PROJECT + "_default"}):
            raise ValueError("Existing preview container identity or isolation differs")
        mounts = {(m.get("Type"), m.get("Name") if m.get("Type") == "volume" else m.get("Source"),
                   m.get("Destination"), m.get("RW")) for m in row.get("Mounts", [])}
        if name in {"api", "worker"}:
            expected = {("bind", fields.get("OMNI_V6_DATA_DIR"), "/data", True),
                        ("bind", fields.get("OMNI_V6_SECRET_DIR"), "/secrets", False)}
        elif name == "postgres":
            expected = {("volume", PROJECT + "_preview-postgres", "/var/lib/postgresql/data", True)}
        else:
            expected = {("bind", "/var/run/docker.sock", "/var/run/docker.sock", True),
                        ("bind", fields.get("OMNI_V6_CONTROLLER_CONFIG"), "/config.toml", False),
                        ("bind", LINUX_ROOT + "/controller", "/var/lib/opensandbox", True),
                        ("bind", LINUX_ROOT + "/snapshots", LINUX_ROOT + "/snapshots", False)}
        if mounts != expected:
            raise ValueError("Existing preview container mounts differ")
    volume_rows = json.loads(wsl("docker", "volume", "inspect", PROJECT + "_preview-postgres"))
    network_rows = json.loads(wsl("docker", "network", "inspect", PROJECT + "_default"))
    if len(volume_rows) != 1 or len(network_rows) != 1:
        raise ValueError("Cannot inspect isolated preview storage and network")
    volume, network = volume_rows[0], network_rows[0]
    if (volume.get("Driver") != "local" or volume.get("Options") not in (None, {})
            or (volume.get("Labels") or {}).get("com.docker.compose.project") != PROJECT
            or (volume.get("Labels") or {}).get("com.docker.compose.volume") != "preview-postgres"
            or network.get("Driver") != "bridge" or network.get("Options") not in (None, {})
            or (network.get("Labels") or {}).get("com.docker.compose.project") != PROJECT
            or (network.get("Labels") or {}).get("com.docker.compose.network") != "default"):
        raise ValueError("Existing preview volume or network ownership differs")


def _verify_interactive_start() -> None:
    if (PRIVATE / "secrets" / "migration-blocked.json").exists():
        raise ValueError("A preview migration needs recovery; refusing to start services")
    profile = (PRIVATE / "secrets" / "compose.env").read_bytes()
    _verify_compose_profile(profile)
    if not _interactive_profile(profile):
        return
    wanted = hashlib.sha256(profile).hexdigest()
    proof = None
    for directory in (PRIVATE / "secrets").glob("upgrade-*"):
        if directory.is_symlink() or not directory.is_dir():
            continue
        candidate = directory / "migration-proof.json"
        if candidate.is_symlink() or not candidate.is_file():
            continue
        value = json.loads(candidate.read_text(encoding="utf-8"))
        if value.get("profile_sha256") == wanted and value.get("instance") == PROJECT:
            proof = value
            break
    if not proof:
        raise ValueError("Interactive preview migration proof is missing or stale")
    if proof.get("revision") != _migration_revision_state():
        raise ValueError("Preview database revision changed after migration")


def migrate_upgrade(backup: str) -> None:
    """Migrate an offline candidate; on failure keep the preview blocked."""
    _preview_offline()
    directory, original = _read_verified_backup(backup)
    target = PRIVATE / "secrets" / "compose.env"
    profile = target.read_bytes()
    fields = {}
    for line in profile.decode("utf-8").splitlines():
        key, _, value = line.partition("=")
        if key in {"OMNI_V6_IMAGE", "OMNI_V6_TOOL_IMAGE", "OMNI_INTERACTIVE_PREVIEW"}:
            if key in fields:
                raise ValueError("Duplicate preview image or interaction setting")
            fields[key] = value
    if (fields.get("OMNI_INTERACTIVE_PREVIEW") != "true"
            or _replace_profile(original, fields.get("OMNI_V6_IMAGE"), fields.get("OMNI_V6_TOOL_IMAGE")) != profile):
        raise ValueError("Preview profile does not match the backed-up candidate upgrade")
    _verify_compose_profile(profile)
    _verify_live_preview(profile, infrastructure_only=True)
    block = PRIVATE / "secrets" / "migration-blocked.json"
    _exclusive(block, json.dumps({"instance": PROJECT, "backup": backup,
                                  "profile_sha256": hashlib.sha256(profile).hexdigest()}).encode())
    try:
        compose("run", "--rm", "--no-deps", "-T", "init")
        revision = _migration_revision_state()
        proof = {"instance": PROJECT, "profile_sha256": hashlib.sha256(profile).hexdigest(),
                 "revision": revision}
        _exclusive(directory / "migration-proof.json", json.dumps(proof).encode())
        block.unlink()
    except BaseException:
        # Alembic may have committed before checkpoint bootstrap failed.
        # Preserve both the database and backup; keep the start block.
        _atomic_profile(original)
        print("Preview remains offline: migration requires database review and recovery from its verified backup.")
        raise
    print(json.dumps({"backup": backup, "result": "migration-verified", "revision": revision["heads"]}))


def activate_upgrade(candidate: str, backup: str, tool_image: str) -> None:
    """Switch only the offline preview profile; leave all data and old images intact."""
    _preview_offline()
    _, original = _read_verified_backup(backup)
    candidate_dir = _named_directory(PRIVATE / "upgrades", candidate, "candidate-")
    meta = json.loads((candidate_dir / "image.json").read_text(encoding="utf-8"))
    for name in ("workspace_broker.py", "workspace_io.py"):
        if hashlib.sha256((ROOT / "src/omniharness" / name).read_bytes()).hexdigest() != meta["broker_sources"][name]:
            raise ValueError("broker source changed since candidate build")
    target = PRIVATE / "secrets/compose.env"
    if target.read_bytes() != original:
        raise ValueError("preview profile changed after backup")
    replacement = _replace_profile(original, meta.get("image"), tool_image)
    _atomic_profile(replacement)
    try:
        compose("config", "--quiet")
    except BaseException:
        _atomic_profile(original)
        raise
    print(json.dumps({"candidate": candidate, "backup": backup, "result": "profile-updated"}))


def rollback_upgrade_profile(backup: str) -> None:
    """Restore the previous image profile only; no database or data is deleted."""
    _preview_offline()
    _, original = _read_verified_backup(backup)
    current = (PRIVATE / "secrets/compose.env").read_bytes()
    _atomic_profile(original)
    try:
        compose("config", "--quiet")
    except BaseException:
        _atomic_profile(current)
        raise
    print(json.dumps({"backup": backup, "result": "profile-restored", "policy": "unchanged"}))


def resume_upgrade_broker(backup: str, *, require_policy: bool = True) -> None:
    """Start a new broker only after verifying the previous one has stopped."""
    _preview_offline()
    directory, _ = _read_verified_backup(backup, require_policy=require_policy)
    marker = PRIVATE / "broker-wsl.pid"
    archived = directory / "broker-wsl.pid"
    if not marker.is_file() or marker.is_symlink() or archived.exists():
        raise ValueError("broker ownership marker is missing or already archived")
    marker.replace(archived)
    try:
        broker()
    except BaseException:
        # If startup may have succeeded, leave both records for reconciliation.
        # Never start another broker or destroy an unknown process automatically.
        raise


def pair() -> None:
    # Explicit action: display only the one-time local pairing output, never a
    # Provider credential. The code is short-lived and user-facing by design.
    print(compose("exec", "-T", "api", "omniharness", "auth-code").decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "build", "build-upgrade", "configure", "database", "bootstrap",
                                           "import-configs", "broker", "up", "status", "pair", "grant-fixture",
                                           "activate-upgrade", "rollback-upgrade-profile", "audit-preview",
                                           "resume-upgrade-broker", "resume-quiesce-broker",
                                           "stop-preview-broker", "quiesce-preview", "migrate-upgrade"))
    parser.add_argument("--candidate")
    parser.add_argument("--backup")
    parser.add_argument("--tool-image")
    args = parser.parse_args()
    action = args.action
    if action == "migrate-upgrade":
        return migrate_upgrade(args.backup)
    if action == "activate-upgrade":
        return activate_upgrade(args.candidate, args.backup, args.tool_image)
    if action == "rollback-upgrade-profile":
        return rollback_upgrade_profile(args.backup)
    if action == "resume-upgrade-broker":
        return resume_upgrade_broker(args.backup)
    if action == "resume-quiesce-broker":
        return resume_upgrade_broker(args.backup, require_policy=False)
    {"prepare": prepare, "build": build, "configure": configure, "database": database,
     "bootstrap": bootstrap, "import-configs": import_configs, "broker": broker,
     "up": up, "status": status, "pair": pair, "build-upgrade": build_upgrade,
     "grant-fixture": backup_and_grant_fixture, "audit-preview": audit_preview,
     "stop-preview-broker": stop_preview_broker, "quiesce-preview": quiesce_preview}[action]()


if __name__ == "__main__":
    main()

