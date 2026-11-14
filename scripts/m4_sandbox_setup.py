"""Opt-in M4-A sandbox checks. Production resources are never changed."""

from __future__ import annotations

import json
import secrets
import subprocess
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".omniharness" / "m4a"
DISTRO = "OmniHarnessCheckAlt"
DATA = "/var/lib/omniharness-m4a"
SNAPSHOTS = DATA + "/snapshots"
NAME = "omni-m4a-controller"
NETWORK = "omni-m4a-control"
CANARY = "omni-m4a-canary"
TOOL = "omniharness/m4a-tool-runtime:0.2"
OWNER = "omniharness.m4a=probe"
DIGESTS = {
    "server": "sha256:25bebdcf1cf7724eb923faecbdbbf82d4038c6ab35c736151574726d14006f30",
    "execd": "sha256:c75836c46ef4302afccec182d5d7339eb2f905f51e3d73b7aa17bb76c832526c",
    "egress": "sha256:a3d30246e557525f3fa44d16226258268cea175532e652822bd1eb4e83e0f54d",
}


def wsl(*args, timeout=180, check=True):
    r = subprocess.run(
        ["wsl.exe", "-d", DISTRO, "-u", "root", "--exec", *args], capture_output=True, timeout=timeout
    )
    out = r.stdout.decode("utf-8", errors="replace").strip()
    if check and r.returncode:
        raise RuntimeError(f"WSL action failed ({r.returncode}): {out[:700]}")
    return out


def docker(*args, **kwargs):
    return wsl("docker", *args, **kwargs)


def mapped(path):
    return wsl("wslpath", "-a", str(Path(path).resolve()))


def image(component):
    return "docker.m.daocloud.io/opensandbox/" + component + "@" + DIGESTS[component]


def save(name, value):
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def credential():
    value = (STATE / "secrets/controller.key").read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise RuntimeError("M4-A credential is missing")
    return value


def prepare():
    folder = STATE / "secrets"
    folder.mkdir(parents=True, exist_ok=True)
    sid = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["icacls.exe", str(folder), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
        capture_output=True,
        check=True,
    )
    if not (folder / "controller.key").exists():
        (folder / "controller.key").write_text(secrets.token_urlsafe(36), encoding="utf-8")
    (folder / "controller.env").write_text(
        "OPENSANDBOX_SERVER_API_KEY=" + credential() + "\n", encoding="utf-8"
    )
    wsl("install", "-d", "-m", "755", DATA, SNAPSHOTS)
    wsl("install", "-d", "-m", "700", DATA + "/controller")
    if int(wsl("cat", "/proc/sys/kernel/cap_last_cap")) > 40:
        raise RuntimeError("New kernel capabilities require an updated trusted-daemon policy")
    images = {}
    for component, digest in DIGESTS.items():
        detail = json.loads(docker("image", "inspect", image(component), "--format", "{{json .}}"))
        if not any(digest in x for x in detail["RepoDigests"]):
            raise RuntimeError("Unexpected upstream digest")
        images[component] = {"source": image(component), "id": detail["Id"], "digests": detail["RepoDigests"]}
    patched = {}
    for component in ("execd", "egress"):
        tag = "omniharness/m4a-" + component + ":1.2.0-m4a1"
        detail = json.loads(docker("image", "inspect", tag, "--format", "{{json .}}"))
        patched[component] = {"tag": tag, "id": detail["Id"]}
    tool_detail = json.loads(docker("image", "inspect", TOOL, "--format", "{{json .}}"))
    config = (ROOT / "deploy/opensandbox.toml").read_text(encoding="utf-8")
    config = config.replace("__M4_EXECD_IMAGE__", patched["execd"]["id"]).replace(
        "__M4_EGRESS_IMAGE__", patched["egress"]["id"]
    )
    (STATE / "controller.toml").write_text(config, encoding="utf-8")
    result = {
        "endpoint": "http://127.0.0.1:19080",
        "snapshots": SNAPSHOTS,
        "tool_image": tool_detail["Id"],
        "images": images,
        "patched": patched,
        "hardened": True,
    }
    save("deployment.json", result)
    return result


def owned(name):
    info = json.loads(docker("inspect", name))[0]
    if info["Config"].get("Labels", {}).get("omniharness.m4a") != "probe":
        raise RuntimeError("Refusing to change an unowned container")
    return info


def up():
    import httpx

    if docker("ps", "-aq", "--filter", "name=^/" + NAME + "$"):
        raise RuntimeError("M4-A controller already exists")
    existing = docker("network", "ls", "-q", "--filter", "name=^" + NETWORK + "$")
    if not existing:
        docker("network", "create", "--label", OWNER, NETWORK)
    elif (
        json.loads(docker("network", "inspect", NETWORK))[0].get("Labels", {}).get("omniharness.m4a")
        != "probe"
    ):
        raise RuntimeError("Unowned control network")
    docker(
        "run",
        "-d",
        "--name",
        CANARY,
        "--label",
        OWNER,
        "--network",
        "bridge",
        "-p",
        "172.17.0.1:19081:8080",
        "--user",
        "10001:10001",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=32",
        "--memory=128m",
        TOOL,
        "python",
        "-m",
        "http.server",
        "8080",
    )
    docker(
        "run",
        "-d",
        "--name",
        NAME,
        "--label",
        OWNER,
        "--network",
        NETWORK,
        "-p",
        "127.0.0.1:19080:8080",
        "--env-file",
        mapped(STATE / "secrets/controller.env"),
        "--mount",
        "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
        "--mount",
        f"type=bind,src={mapped(STATE / 'controller.toml')},dst=/config.toml,readonly",
        "--mount",
        f"type=bind,src={SNAPSHOTS},dst={SNAPSHOTS},readonly",
        "--mount",
        f"type=bind,src={DATA}/controller,dst=/var/lib/opensandbox",
        image("server"),
        "--config",
        "/config.toml",
    )
    with httpx.Client(trust_env=False, timeout=2) as client:
        for _ in range(45):
            try:
                if client.get("http://127.0.0.1:19080/health").status_code == 200:
                    baseline = docker(
                        "exec",
                        NAME,
                        "python",
                        "-c",
                        "import urllib.request; print(urllib.request.urlopen('http://172.17.0.1:19081',timeout=3).status)",
                    )
                    if baseline != "200":
                        raise RuntimeError("Network canary unavailable")
                    return {"health": "ready", "canary_baseline": 200}
            except httpx.HTTPError:
                pass
            time.sleep(1)
    raise RuntimeError("Controller did not become ready")


def connection():
    from opensandbox.config.connection_sync import ConnectionConfigSync
    from opensandbox.transport import RetryPolicy

    return ConnectionConfigSync(
        domain="127.0.0.1:19080",
        protocol="http",
        api_key=credential(),
        use_server_proxy=True,
        request_timeout=timedelta(seconds=35),
        retry_policy=RetryPolicy.disabled(),
        disable_metrics=True,
        enable_tracing=False,
        follow_redirects=False,
    )


def operation():
    """Issue one isolated operation's daemon token; never expose it to test output."""
    deployed = json.loads((STATE / "deployment.json").read_text(encoding="utf-8"))
    if not deployed.get("hardened"):
        raise RuntimeError("The hardened M4-A deployment has not been prepared")
    token = secrets.token_urlsafe(36)
    config = connection()
    config.headers["X-EXECD-ACCESS-TOKEN"] = token
    env = {
        "EXECD_ACCESS_TOKEN": token,
        "EXECD_ISOLATION_CONFIG": "/opt/omniharness/execd-hardening.toml",
        "EXECD_ENVS": "/opt/omniharness/execd.env",
    }
    return deployed["tool_image"], config, env
