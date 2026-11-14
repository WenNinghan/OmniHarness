"""Actual isolated sandbox contract checks."""

import json
import time
import uuid
from datetime import timedelta

from m4_sandbox_setup import (
    CANARY,
    NAME,
    NETWORK,
    SNAPSHOTS,
    STATE,
    credential,
    docker,
    mapped,
    operation,
    owned,
    save,
    wsl,
)


def python(sandbox, code, timeout=20):
    from opensandbox.models.execd import RunCommandOpts

    result = sandbox.commands.run(
        ["python", "-c", code],
        opts=RunCommandOpts(
            timeout=timedelta(seconds=timeout), working_directory="/workspace", uid=10001, gid=10001
        ),
    )
    if result.exit_code != 0:
        raise RuntimeError(f"Probe execution exit {result.exit_code}")
    return "".join(x.text for x in result.logs.stdout)


def probe():
    import httpx
    from opensandbox import SandboxSync
    from opensandbox.models import Host, NetworkPolicy, Volume
    from opensandbox.models.execd import RunCommandOpts

    result = {
        "status": "BLOCKED",
        "timestamp": time.time(),
        "secure_access": False,
        "native_auth": True,
        "checks": {},
    }
    checks = result["checks"]
    sandbox = None
    env = {}
    snapshot = SNAPSHOTS + "/probe-" + uuid.uuid4().hex
    wsl("install", "-d", "-m", "755", "-o", "10001", "-g", "10001", snapshot)
    fixture = STATE / "fixture.txt"
    fixture.write_text("中文 fixture stays unchanged\n", encoding="utf-8")
    wsl("cp", mapped(fixture), snapshot + "/输入 文件.txt")
    wsl("chown", "10001:10001", snapshot + "/输入 文件.txt")
    try:
        with httpx.Client(trust_env=False, timeout=5) as client:
            denied = client.get("http://127.0.0.1:19080/v1/sandboxes")
            allowed = client.get(
                "http://127.0.0.1:19080/v1/sandboxes", headers={"OPEN-SANDBOX-API-KEY": credential()}
            )
        checks["controller_auth"] = {
            "pass": denied.status_code in (401, 403) and allowed.status_code == 200,
            "without_key": denied.status_code,
            "with_key": allowed.status_code,
        }
        tool, config, env = operation()
        sandbox = SandboxSync.create(
            tool,
            timeout=timedelta(minutes=10),
            ready_timeout=timedelta(seconds=60),
            connection_config=config,
            resource={"cpu": "2", "memory": "2Gi"},
            env=env,
            metadata={"omniharness.m4a": "probe"},
            secure_access=False,
            network_policy=NetworkPolicy(default_action="deny", egress=[]),
            volumes=[Volume(name="workspace", host=Host(path=snapshot), mount_path="/workspace")],
        )
        result["sandbox_id"] = sandbox.id
        save("last-report.json", result)
        info = json.loads(docker("inspect", "sandbox-" + sandbox.id))[0]
        host = info["HostConfig"]
        checks["resources"] = {
            "pass": host["Memory"] == 2147483648
            and host["NanoCpus"] == 2000000000
            and host["PidsLimit"] == 128,
            "memory": host["Memory"],
            "cpu": host["NanoCpus"],
            "pids": host["PidsLimit"],
        }
        checks["mounts"] = {
            "pass": all(m["Destination"] in ("/workspace", "/opt/opensandbox") for m in info["Mounts"]),
            "mounts": [
                {"source": m["Source"], "target": m["Destination"], "rw": m["RW"]} for m in info["Mounts"]
            ],
        }
        identity = json.loads(
            python(sandbox, "import os,json; print(json.dumps({'uid':os.getuid(),'gid':os.getgid()}))")
        )
        checks["non_root"] = {"pass": identity == {"uid": 10001, "gid": 10001}, **identity}
        cap_code = "from pathlib import Path; import json; print(json.dumps({k:v.strip() for k,_,v in (line.partition(':') for line in Path('/proc/self/status').read_text().splitlines()) if k.startswith('Cap') or k in ('NoNewPrivs','Seccomp','Seccomp_filters')}))"
        workload_caps = json.loads(python(sandbox, cap_code))
        daemon_status = docker("exec", "sandbox-" + sandbox.id, "cat", "/proc/1/status")
        daemon_caps = {
            k: v.strip()
            for k, _, v in (line.partition(":") for line in daemon_status.splitlines())
            if k.startswith("Cap")
        }
        checks["caps"] = {
            "pass": all(int(v, 16) == 0 for k, v in workload_caps.items() if k.startswith("Cap"))
            and workload_caps.get("NoNewPrivs") == "1"
            and workload_caps.get("Seccomp") == "2"
            and int(workload_caps.get("Seccomp_filters", "0")) >= 2
            and int(daemon_caps["CapEff"], 16) == 480
            and int(daemon_caps["CapBnd"], 16) == 480
            and not host.get("CapAdd"),
            "workload": workload_caps,
            "daemon": daemon_caps,
            "security_options": host.get("SecurityOpt"),
        }
        text = python(
            sandbox,
            "from pathlib import Path; p=Path('/workspace/输入 文件.txt'); print(p.read_text(),end=''); p.write_text('sandbox edit')",
        )
        checks["snapshot_io"] = {
            "pass": text == fixture.read_text(encoding="utf-8")
            and wsl("cat", snapshot + "/输入 文件.txt") == "sandbox edit",
            "original_unchanged": fixture.read_text(encoding="utf-8") == text,
        }
        text = python(sandbox, "import sys; sys.stdout.write('first\\n\\n中文\\nlast')")
        checks["stdout"] = {"pass": text == "first\n\n中文\nlast", "observed": text}
        code = """import json,socket,urllib.request,urllib.error
r={}
for name,host,port in [('direct_ip','1.1.1.1',443),('canary','172.17.0.1',19081),('metadata','169.254.169.254',80),('ipv6','2606:4700:4700::1111',443)]:
 try:
  s=socket.create_connection((host,port),timeout=2);s.close();r[name]=True
 except OSError:r[name]=False
try:socket.getaddrinfo('example.com',443);r['dns']=True
except OSError:r['dns']=False
for name,url in [('execd','http://127.0.0.1:44772/command'),('egress','http://127.0.0.1:18080/policy')]:
 try:
  req=urllib.request.Request(url,data=b'{"command":"true"}',headers={'Content-Type':'application/json'}) if name=='execd' else urllib.request.Request(url)
  with urllib.request.urlopen(req,timeout=2) as resp:r[name]=resp.status
 except urllib.error.HTTPError as e:r[name]=e.code
 except OSError:r[name]=None
print(json.dumps(r))
"""
        net = json.loads(python(sandbox, code))
        checks["network"] = {
            "pass": not any(net[k] for k in ("direct_ip", "canary", "metadata", "ipv6", "dns")),
            **net,
        }
        checks["local_auth"] = {
            "pass": net["execd"] in (401, 403) and net["egress"] in (401, 403),
            "execd": net["execd"],
            "egress": net["egress"],
        }
        before = time.monotonic()
        timed = sandbox.commands.run(
            ["python", "-c", "import time;time.sleep(20)"],
            opts=RunCommandOpts(timeout=timedelta(seconds=2), uid=10001, gid=10001),
        )
        elapsed = time.monotonic() - before
        checks["timeout"] = {
            "pass": elapsed < 8 and timed.exit_code != 0,
            "seconds": round(elapsed, 2),
            "exit_code": timed.exit_code,
        }
        running = sandbox.commands.run(
            ["python", "-c", "import subprocess,time;subprocess.Popen(['sleep','90']);time.sleep(90)"],
            opts=RunCommandOpts(background=True, timeout=timedelta(seconds=100), uid=10001, gid=10001),
        )
        sandbox.commands.interrupt(running.id)
        time.sleep(1)
        status = sandbox.commands.get_command_status(running.id)
        remaining = json.loads(
            python(
                sandbox,
                "from pathlib import Path; import json; print(json.dumps([p.parent.name for p in Path('/proc').glob('[0-9]*/cmdline') if p.read_bytes()==b'sleep\\x0090\\x00']))",
            )
        )
        checks["cancel"] = {
            "pass": not status.running and not remaining,
            "running": status.running,
            "children": remaining,
        }
        # Additional adversarial checks must pass before production enablement.
        result["status"] = "BASELINE_PASS" if all(v["pass"] for v in checks.values()) else "BLOCKED"
    except Exception as exc:
        message = str(exc).replace(credential(), "[REDACTED]")
        if env.get("EXECD_ACCESS_TOKEN"):
            message = message.replace(env["EXECD_ACCESS_TOKEN"], "[REDACTED]")
        result["error"] = {"type": type(exc).__name__, "message": message[:1800]}
    finally:
        if sandbox:
            try:
                sandbox.destroy()
                remaining = docker("ps", "-aq", "--filter", "name=^/sandbox-" + sandbox.id + "$")
                result["cleanup"] = "destroyed" if not remaining else "container_still_exists"
                if remaining:
                    result["status"] = "BLOCKED"
            except Exception as exc:
                result["cleanup"] = type(exc).__name__
                result["status"] = "BLOCKED"
        save("last-report.json", result)
    return result


def down():
    for name in (NAME, CANARY):
        if docker("ps", "-aq", "--filter", "name=^/" + name + "$"):
            owned(name)
            docker("rm", "-f", name)
    if docker("network", "ls", "-q", "--filter", "name=^" + NETWORK + "$"):
        net = json.loads(docker("network", "inspect", NETWORK))[0]
        if net.get("Labels", {}).get("omniharness.m4a") != "probe" or net.get("Containers"):
            raise RuntimeError("Refusing to remove nonempty/unowned network")
        docker("network", "rm", NETWORK)
    return {"stopped": True, "snapshots": "retained"}
