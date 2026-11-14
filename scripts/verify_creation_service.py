"""Isolated real PostgreSQL/controller/creation-service acceptance on local WSL.

Uses cached immutable images and uniquely labeled disposable containers. Never
starts the engine, changes a deployed instance, or invokes a paid model. Native
Linux journals/snapshots remain as evidence; only verified test containers and
their one-shot Docker markers are removed after successful acceptance.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psycopg
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
WSL = ["wsl.exe", "-d", "OmniHarnessCheckAlt", "-u", "root", "--exec"]
DOCKER = [*WSL, "docker"]
APP = "sha256:b22ba966ff9640f3c52ad7cb802a30e5fc03a975da1104a31b9af52621555a7c"
TOOL = "sha256:b37700184c262593a5535bbb8196b7d28cb07514ec67c9168aca230291aa76ad"
EXECD = "sha256:6b5a76c64ef66f500d38e068b1a50d68eb2f7fbc6d18cddbce525d86faeca0e1"
EGRESS = "sha256:cad71a020c0990470d4ebe12eb01fc4e6e12afb431d67ae6b655626f7539b8e3"
SERVER = "sha256:0fa4658bb6573f7ef8aae710b7dcb14291e1a486013b9377e6555deb330f55b0"
PG = "sha256:018dbecf6a31cfed1f83c218b5e428a52ac389ad46e495f72831641db49d85a9"
LABEL = "omni.creation.acceptance"


def run(arguments, *, data=None, timeout=60):
    result = subprocess.run(arguments, input=data, capture_output=True, timeout=timeout, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(f"Isolated command failed: {arguments[0]} ({result.returncode})")
    return result.stdout.decode("utf-8").strip()


def docker(*arguments, **kwargs):
    return run([*DOCKER, *arguments], **kwargs)


def mapped(path):
    return run([*WSL, "wslpath", "-a", str(Path(path).resolve())])


def port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def wait_until(probe, description, *, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except (OSError, httpx.HTTPError, psycopg.OperationalError):
            pass
        time.sleep(0.25)
    raise RuntimeError(description + " did not become ready")


def main():
    if os.name != "nt":
        raise RuntimeError("This acceptance entry uses the existing Windows/WSL profile")
    nonce = "sol-" + secrets.token_hex(6)
    directory = ROOT / ".omniharness" / "creation-validation" / nonce
    directory.mkdir(parents=True)
    linux = "/var/lib/omniharness-creation-validation/" + nonce
    witness = "/var/lib/omniharness-creation-witness-validation/" + nonce
    names = {kind: "omni-creation-" + nonce + "-" + kind for kind in ("pg", "controller", "service")}
    instance = "creation-" + nonce
    password, controller_key, service_key = (secrets.token_urlsafe(36) for _ in range(3))
    pg_port, controller_port, service_port = port(), port(), port()
    if len({pg_port, controller_port, service_port}) != 3:
        raise RuntimeError("Select distinct isolated listener ports")
    database = f"postgresql+psycopg://tester:{password}@127.0.0.1:{pg_port}/omniharness_test"
    engine_id = docker("info", "--format", "{{.ID}}")
    for image in (APP, TOOL, EXECD, EGRESS, SERVER, PG):
        if docker("image", "inspect", image, "--format", "{{.Id}}") != image:
            raise RuntimeError("Acceptance requires the inspected cached images")
    gateway = docker("network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}")
    if not re.fullmatch(r"[0-9.]+", gateway):
        raise RuntimeError("Unexpected sandbox bridge gateway")
    config = (ROOT / "deploy/opensandbox.toml").read_text(encoding="utf-8")
    config = config.replace("__M4_EXECD_IMAGE__", EXECD).replace("__M4_EGRESS_IMAGE__", EGRESS)
    config = config.replace("/var/lib/omniharness-m4a/snapshots", linux + "/snapshots")
    config = config.replace("172.17.0.1", gateway).replace("41000", "45000").replace("41100", "45100")
    settings_values = {"instance_id": instance, "sandbox_endpoint": f"http://127.0.0.1:{controller_port}",
        "sandbox_image": TOOL, "sandbox_snapshot_root": linux + "/snapshots", "sandbox_max_command_seconds": 600}
    from types import SimpleNamespace

    from omniharness.sandbox_creation_service import CreationServiceSettings, service_identity
    storage = CreationServiceSettings(journal_path=linux + "/primary/creation.sqlite", journal_id=secrets.token_hex(32),
        witness_path=witness + "/witness.sqlite", witness_id=secrets.token_hex(32),
        engine_id=engine_id, marker_image=TOOL)
    service_id = service_identity(SimpleNamespace(**settings_values), storage)
    service_url = f"http://127.0.0.1:{service_port}"
    env = {"OMNI_DATABASE_URL": database, "OMNI_DATA_DIR": "/testdata", "OMNI_SECRET_DIR": linux + "/keys",
        "OMNI_INSTANCE_ID": instance, "OMNI_SANDBOX_ENDPOINT": settings_values["sandbox_endpoint"],
        "OMNI_SANDBOX_IMAGE": TOOL, "OMNI_SANDBOX_SNAPSHOT_ROOT": linux + "/snapshots",
        "OMNI_SANDBOX_KEY_FILE": linux + "/keys/controller.key", "OMNI_SANDBOX_CREATION_URL": service_url,
        "OMNI_SANDBOX_CREATION_SERVICE_ID": service_id, "OMNI_SANDBOX_CREATION_KEY_FILE": linux + "/keys/creation.key",
        "OMNI_CREATION_JOURNAL_PATH": linux + "/primary/creation.sqlite", "OMNI_CREATION_JOURNAL_ID": storage.journal_id,
        "OMNI_CREATION_WITNESS_PATH": witness + "/witness.sqlite", "OMNI_CREATION_WITNESS_ID": storage.witness_id,
        "OMNI_CREATION_ENGINE_ID": engine_id, "OMNI_CREATION_MARKER_IMAGE": TOOL,
        "OMNI_CREATION_PORT": str(service_port), "PYTHONPATH": "/candidate", "PYTHONDONTWRITEBYTECODE": "1"}
    setup = '''import json,os,pathlib,re,socket,sys
p=json.load(sys.stdin); nonce=p['nonce']; assert re.fullmatch(r'sol-[a-f0-9]{12}',nonce)
root=pathlib.Path('/var/lib/omniharness-creation-validation')/nonce
witness=pathlib.Path('/var/lib/omniharness-creation-witness-validation')/nonce
for target in (root,witness):
 assert target==target.resolve() and not target.exists(); target.mkdir(parents=True,mode=0o700)
for name in ('primary','snapshots','controller','keys'): (root/name).mkdir(mode=0o700)
for key,value in p['keys'].items():
 target=root/'keys'/key
 with os.fdopen(os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as f: f.write(value)
for name,value in p['files'].items():
 with os.fdopen(os.open(root/name,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as f: f.write(value)
for port in range(45000,45101):
 with socket.socket() as s: s.bind(('0.0.0.0',port))
print('Protected acceptance directories provisioned')
'''
    files = {"controller.toml": config, "controller.env": "OPENSANDBOX_SERVER_API_KEY=" + controller_key + "\n",
        "service.env": "".join(key + "=" + value + "\n" for key, value in env.items()),
        "pg.env": "POSTGRES_USER=tester\nPOSTGRES_DB=omniharness_test\nPOSTGRES_PASSWORD=" + password + "\n"}
    run([*WSL, "python3", "-c", setup], data=json.dumps({"nonce": nonce, "keys": {
        "controller.key": controller_key, "creation.key": service_key}, "files": files}).encode())
    # Native test client needs only its own ephemeral credentials, never a
    # deployment's master/controller/provider key.
    (directory / "controller.key").write_text(controller_key)
    (directory / "creation.key").write_text(service_key)
    state = {"nonce": nonce, "instance_id": instance, "database_url": database,
        "linux_root": linux, "witness_root": witness, "service_url": service_url, "service_id": service_id,
        "engine_id": engine_id, "controller_endpoint": settings_values["sandbox_endpoint"], "tool_image": TOOL,
        "service_container": names["service"], "label": LABEL}
    state_path = directory / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    passed = False
    started = []
    try:
        docker("run", "-d", "--name", names["pg"], "--label", LABEL + "=" + nonce,
            "--tmpfs", "/var/lib/postgresql/data", "--env-file", linux + "/pg.env",
            "-p", f"127.0.0.1:{pg_port}:5432", PG)
        started.append(names["pg"])

        def database_ready():
            with psycopg.connect(host="127.0.0.1", port=pg_port, user="tester", password=password,
                                 dbname="omniharness_test", connect_timeout=1):
                return True

        wait_until(database_ready, "Isolated PostgreSQL")
        from omniharness.cli import migrate
        from omniharness.settings import Settings
        migrate(Settings(_env_file=None, database_url=database, data_dir=directory / "data",
                         secret_dir=directory / "secrets"))
        docker("run", "-d", "--name", names["controller"], "--label", LABEL + "=" + nonce,
            "--env-file", linux + "/controller.env", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "-p", f"127.0.0.1:{controller_port}:8080", "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount", f"type=bind,src={linux}/controller.toml,dst=/config.toml,readonly",
            "--mount", f"type=bind,src={linux}/controller,dst=/var/lib/opensandbox",
            "--mount", f"type=bind,src={linux}/snapshots,dst={linux}/snapshots,readonly", SERVER, "--config", "/config.toml")
        started.append(names["controller"])
        wait_until(lambda: httpx.get(settings_values["sandbox_endpoint"] + "/health", timeout=2,
                                    trust_env=False).status_code == 200, "Isolated controller")
        service_args = ["--network", "host", "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--env-file", linux + "/service.env", "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount", "type=bind,src=" + mapped(ROOT / "src") + ",dst=/candidate,readonly",
            "--mount", f"type=bind,src={linux},dst={linux}", "--mount", f"type=bind,src={witness},dst={witness}"]
        docker("run", "--rm", *service_args, "--entrypoint", "python", APP,
               "-m", "omniharness.sandbox_creation_service", "initialize")
        docker("run", "-d", "--name", names["service"], "--label", LABEL + "=" + nonce,
               *service_args, "--entrypoint", "python", APP, "-m", "omniharness.sandbox_creation_service", "serve")
        started.append(names["service"])
        headers = {"X-Creation-Service-Key": service_key, "X-Creation-Service-ID": service_id}
        wait_until(lambda: httpx.post(service_url + "/internal/status", headers=headers, json={},
                                     timeout=2, trust_env=False).status_code == 422, "Independent creation service")
        print("Independent PostgreSQL, hardened controller and creation service ready.", flush=True)
        child_env = os.environ.copy()
        child_env["PYTHONUTF8"] = "1"
        child_env["OMNI_CREATION_LIVE_STATE"] = str(state_path)
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "tests/test_sandbox_creation_live.py"],
                                cwd=ROOT, env=child_env, capture_output=True, timeout=480)
        output = result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")
        for secret in (password, controller_key, service_key):
            output = output.replace(secret, "[redacted test credential]")
        (directory / "pytest.txt").write_text(output, encoding="utf-8")
        print(output[-18000:], flush=True)
        if result.returncode:
            raise RuntimeError("Real acceptance failed; isolated evidence retained")
        # Fenced-checkpointer tests deliberately reject credentials embedded in
        # a URL, including test credentials. libpq reads the ephemeral password.
        child_env["OMNI_TEST_POSTGRES_URL"] = make_url(database)._replace(password=None).render_as_string(hide_password=False)
        child_env["PGPASSWORD"] = password
        regression = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x",
            "tests/test_sandbox_creation_authorization_postgres.py", "tests/test_m4_runtime_postgres.py",
            "tests/test_m3_host.py"], cwd=ROOT, env=child_env, capture_output=True, timeout=480)
        output = regression.stdout.decode("utf-8", errors="replace") + regression.stderr.decode("utf-8", errors="replace")
        for secret in (password, controller_key, service_key):
            output = output.replace(secret, "[redacted test credential]")
        (directory / "postgres-regression.txt").write_text(output, encoding="utf-8")
        print(output[-12000:], flush=True)
        if regression.returncode:
            raise RuntimeError("PostgreSQL Worker regression failed; isolated evidence retained")
        passed = True
    finally:
        if passed:
            # No success cleanup may hide a live sandbox or its egress sidecar.
            remaining = docker("ps", "-aq", "--filter", "label=omni_instance=" + instance).split()
            if remaining:
                raise RuntimeError("Acceptance left sandbox resources; retain the test environment")
            # Egress containers need not carry the application's instance label.
            # Verify every durable returned ID as well as the metadata filter.
            identities = json.loads(docker("exec", names["service"], "python", "-c",
                "import json,os,pathlib,sqlite3; p=pathlib.Path(os.environ['OMNI_CREATION_JOURNAL_PATH']); "
                "c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True); "
                "print(json.dumps([r[0] for r in c.execute(\"SELECT DISTINCT sandbox_id FROM "
                "sandbox_creation_journal WHERE state='CREATED'\")])); c.close()"))
            for identity in identities:
                if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity):
                    raise RuntimeError("Unexpected test sandbox identity; retain the environment")
                sandbox = docker("ps", "-aq", "--filter", "name=^/sandbox-" + re.escape(identity) + "$")
                sidecars = docker("ps", "-aq", "--filter", "label=opensandbox.io/egress-sidecar-for=" + identity)
                if sandbox or sidecars:
                    raise RuntimeError("Acceptance left a sandbox/sidecar; retain the test environment")
            (directory / "resource-absence.json").write_text(json.dumps({
                "instance_metadata_absent": True, "durable_sandbox_ids_checked": len(identities),
                "sandbox_names_and_sidecars_absent": True}), encoding="utf-8")
            markers = docker("ps", "-aq", "--filter", "label=omni_fence_instance=" + instance).split()
            for marker in markers:
                obj = json.loads(docker("inspect", marker))[0]
                if (obj["Config"]["Labels"].get("omni_fence_instance") != instance or obj["Image"] != TOOL
                        or obj["State"]["Status"] != "created"):
                    raise RuntimeError("Marker ownership differs; no removal")
                docker("rm", obj["Id"])
            for name in reversed(started):
                obj = json.loads(docker("inspect", name))[0]
                if obj["Config"]["Labels"].get(LABEL) != nonce:
                    raise RuntimeError("Container ownership differs; no removal")
                docker("stop", "--time", "10", obj["Id"], timeout=25)
                docker("rm", obj["Id"])
            print("Verified test containers/markers removed; journals, snapshots and report retained.", flush=True)
        else:
            print("Isolated environment retained for diagnosis: " + str(directory), flush=True)
    print(json.dumps({"passed": True, "report": str(directory / "pytest.txt"), "nonce": nonce}), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
