"""Explicit, isolated M4 preview deployment. Never operates on the production stack."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".omniharness/m4-preview"
LINUX_ROOT = "/var/lib/omniharness-m4-preview"
DISTRO = "OmniHarnessCheckAlt"
PROJECT = "omni-m4-preview"
TOOL = "sha256:8e0afff95c8c7b387992f479d9641685bae3e9ccfe4545168a6efbde5b955165"
EXECD = "sha256:6b5a76c64ef66f500d38e068b1a50d68eb2f7fbc6d18cddbce525d86faeca0e1"
EGRESS = "sha256:cad71a020c0990470d4ebe12eb01fc4e6e12afb431d67ae6b655626f7539b8e3"
BASES = {
    "node:24-bookworm-slim": "docker.m.daocloud.io/library/node@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9",
    "ghcr.io/astral-sh/uv:0.11.6": "ghcr.m.daocloud.io/astral-sh/uv@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754",
    "python:3.12-slim-bookworm": "docker.m.daocloud.io/library/python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df",
}


def mapped(path: Path) -> str:
    path = path.resolve()
    if path.drive.lower() != "g:":
        raise ValueError("This reviewed preview profile is restricted to G:")
    return "/mnt/g/" + path.as_posix()[3:]


def wsl(*args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["wsl.exe", "-d", DISTRO, "-u", "root", "--exec", *args],
        input=data,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        # Do not include process input, environment or arbitrary remote output.
        raise RuntimeError(f"WSL operation failed ({args[0]}, exit {result.returncode})")
    return result.stdout


def compose(*args: str) -> bytes:
    return wsl(
        "docker",
        "compose",
        "--project-name",
        PROJECT,
        "--file",
        mapped(ROOT / "deploy/m4-preview.compose.yaml"),
        "--env-file",
        mapped(PRIVATE / "secrets/compose.env"),
        *args,
    )


def secret_file(name: str) -> Path:
    path = PRIVATE / "secrets" / name
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return path


def prepare() -> None:
    for folder in ("secrets", "data", "native", "logs"):
        (PRIVATE / folder).mkdir(parents=True, exist_ok=True)
    account = subprocess.check_output(["whoami"], text=True).strip()
    subprocess.run(
        ["icacls", str(PRIVATE / "secrets"), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"],
        check=True,
        capture_output=True,
    )
    for name in ("workspace-service.key", "controller.key", "postgres.key"):
        secret_file(name)
    master = PRIVATE / "secrets/master.key"
    if not master.exists():
        master.write_bytes((ROOT / ".omniharness/secrets/master.key").read_bytes())
    elif master.read_bytes() != (ROOT / ".omniharness/secrets/master.key").read_bytes():
        raise ValueError("Candidate encryption key differs; refusing credential copy")
    config = (ROOT / "deploy/opensandbox.toml").read_text(encoding="utf-8")
    config = config.replace("__M4_EXECD_IMAGE__", EXECD).replace("__M4_EGRESS_IMAGE__", EGRESS)
    config = config.replace("/var/lib/omniharness-m4a/", LINUX_ROOT + "/")
    config = config.replace("41000", "41200").replace("41100", "41300")
    (PRIVATE / "controller.toml").write_text(config, encoding="utf-8")
    (PRIVATE / "secrets/controller.env").write_text(
        "OPENSANDBOX_SERVER_API_KEY=" + secret_file("controller.key").read_text() + "\n",
        encoding="utf-8",
    )
    wsl("mkdir", "-p", LINUX_ROOT, LINUX_ROOT + "/snapshots", LINUX_ROOT + "/controller")
    wsl("chmod", "700", LINUX_ROOT, LINUX_ROOT + "/snapshots", LINUX_ROOT + "/controller")
    print("Preview private roots and independent service credentials prepared; no values printed.")


def build(*, reuse_dependencies: bool = False) -> None:
    context = PRIVATE / ("build-" + secrets.token_hex(5))
    context.mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "alembic.ini", ".dockerignore"):
        shutil.copy2(ROOT / name, context / name)
    ignore = shutil.ignore_patterns("node_modules", "dist", "__pycache__", "*.pyc", ".env", ".env.*")
    shutil.copytree(ROOT / "src", context / "src", ignore=ignore)
    shutil.copytree(ROOT / "web", context / "web", ignore=ignore)
    baseline = ROOT / ".omniharness/output-limits-web-20261108/src/styles.css"
    shutil.copy2(baseline, context / "web/src/styles.css")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for source, pinned in BASES.items():
        dockerfile = dockerfile.replace("FROM " + source, "FROM " + pinned)
    dependency_source = None
    if reuse_dependencies:
        # A local, already tested dependency layer is usable only with identical
        # dependency declarations and locks. Source and web assets are rebuilt.
        previous = PRIVATE / "build-dd803b4432"
        for name in ("pyproject.toml", "uv.lock", "web/package-lock.json"):
            if (previous / name).read_bytes() != (ROOT / name).read_bytes():
                raise ValueError("Dependency inputs changed; use the full locked build")
        old = json.loads((previous / "web/package.json").read_text())
        new = json.loads((ROOT / "web/package.json").read_text())
        old.get("scripts", {}).pop("test", None)
        new.get("scripts", {}).pop("test", None)
        if old != new:
            raise ValueError("Web dependency/build settings changed; use the full locked build")
        app_base = "sha256:a596d6108779a275829c22abf2ef0516531634d317db909f7777becba758cc8d"
        web_base = "sha256:98be25eb6a1c99b820b0ca9e65ed84f7c42b8190493ef873bc7ac25f9edbf7ae"
        dependency_source = {"application": app_base, "web": web_base, "locks_verified": True}
        app_tag, web_tag = "omniharness/deps:a596d6108779", "omniharness/deps:98be25eb6a1c"
        for image_id, tag in ((app_base, app_tag), (web_base, web_tag)):
            wsl("docker", "tag", image_id, tag)
            if wsl("docker", "image", "inspect", tag, "--format", "{{.Id}}").decode().strip() != image_id:
                raise ValueError("Local dependency layer identity changed")
        dockerfile = f"""FROM {web_tag} AS web-build
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
"""
    (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    log = PRIVATE / "logs/build.log"
    with log.open("wb") as stream:
        result = subprocess.run(
            [
                "wsl.exe",
                "-d",
                DISTRO,
                "-u",
                "root",
                "--exec",
                "docker",
                "build",
                "--progress=plain",
                "-t",
                "omniharness/m4-preview:local",
                mapped(context),
            ],
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode:
        raise RuntimeError("Candidate build failed; inspect private logs/build.log")
    image = (
        wsl("docker", "image", "inspect", "omniharness/m4-preview:local", "--format", "{{.Id}}")
        .decode()
        .strip()
    )
    metadata = {"image": image, "context": str(context), "stylesheet_baseline": str(baseline),
                "dependency_source": dependency_source}
    (PRIVATE / "image.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata))


def configure() -> None:
    image = json.loads((PRIVATE / "image.json").read_text(encoding="utf-8"))["image"]
    values = {
        "OMNI_PREVIEW_POSTGRES_PASSWORD": secret_file("postgres.key").read_text(),
        "OMNI_PREVIEW_TOOL_IMAGE": TOOL,
        "OMNI_PREVIEW_IMAGE": image,
        "OMNI_PREVIEW_CONTROLLER_ENV": mapped(PRIVATE / "secrets/controller.env"),
        "OMNI_PREVIEW_CONTROLLER_CONFIG": mapped(PRIVATE / "controller.toml"),
        "OMNI_PREVIEW_DATA_DIR": mapped(PRIVATE / "data"),
        "OMNI_PREVIEW_SECRET_DIR": mapped(PRIVATE / "secrets"),
    }
    (PRIVATE / "secrets/compose.env").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    compose("config", "--quiet")
    print("Candidate Compose profile validated; credentials not displayed.")


def broker_environment() -> None:
    wsl("python3", "-m", "venv", LINUX_ROOT + "/broker-venv")
    wsl(
        LINUX_ROOT + "/broker-venv/bin/pip",
        "install",
        "--disable-pip-version-check",
        "-r",
        mapped(ROOT / "deploy/m4-broker-requirements.txt"),
    )
    print("Dedicated WSL host broker environment installed from the tested requirements.")


EXPORT = """import json
from sqlalchemy import text
from omniharness.settings import Settings
from omniharness.db import make_engine
engine=make_engine(Settings())
with engine.connect() as db:
    db.execute(text("SET TRANSACTION READ ONLY"))
    data={table:[dict(r) for r in db.execute(text("SELECT * FROM " + table)).mappings()] for table in ("providers","model_configs","role_bindings","stage_binding_sets")}
    print(json.dumps(data,default=str))
engine.dispose()
"""

IMPORT = """import json,sys
from datetime import datetime
from sqlalchemy import DateTime,select,func
from omniharness.settings import Settings
from omniharness.db import make_engine
from omniharness.models import Provider,ModelConfig,RoleBinding,StageBindingSet
data=json.load(sys.stdin)
engine=make_engine(Settings())
with engine.begin() as db:
    assert db.scalar(select(func.count()).select_from(ModelConfig)) == 0, "Candidate model table must be empty"
    for model in (Provider,ModelConfig,RoleBinding,StageBindingSet):
        table=model.__table__
        rows=[]
        for original in data[table.name]:
            row={k:v for k,v in original.items() if k in table.columns}
            for column in table.columns:
                if isinstance(column.type,DateTime) and row.get(column.name): row[column.name]=datetime.fromisoformat(row[column.name])
            rows.append(row)
        if model is StageBindingSet:
            for row in rows:
                existing=db.execute(select(table).where(table.c.id==row['id'])).mappings().first()
                if existing:
                    assert existing['version']==0 and not existing['bindings'], 'Only the empty migration seed may be replaced'
                    db.execute(table.update().where(table.c.id==row['id']).values(**row))
                else: db.execute(table.insert(),row)
        elif rows: db.execute(table.insert(),rows)
    print(json.dumps({k:len(v) for k,v in data.items()}))
engine.dispose()
"""


def copy_configs() -> None:
    # Production is read-only; encrypted fields stay in process memory / stdin.
    payload = wsl("docker", "exec", "omniharness-api-1", "python", "-c", EXPORT)
    result = wsl("docker", "exec", "-i", PROJECT + "-api-1", "python", "-c", IMPORT, data=payload)
    print(result.decode().strip())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=("prepare", "build", "build-cached", "configure", "broker-env", "up", "copy-configs", "status")
    )
    action = parser.parse_args().action
    if action == "prepare":
        prepare()
    elif action == "build":
        build()
    elif action == "build-cached":
        build(reuse_dependencies=True)
    elif action == "configure":
        configure()
    elif action == "broker-env":
        broker_environment()
    elif action == "up":
        compose("up", "-d", "--wait", "--wait-timeout", "180")
        print("Independent preview services started.")
    elif action == "copy-configs":
        copy_configs()
    else:
        for line in compose("ps", "--format", "json").decode().splitlines():
            row = json.loads(line)
            print(json.dumps({k: row.get(k) for k in ("Name", "State", "Health", "Ports")}))


if __name__ == "__main__":
    main()
