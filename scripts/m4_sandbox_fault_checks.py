"""Controlled fault checks, limited to one disposable M4-A sandbox and canary."""

import json
import time
import uuid
from datetime import timedelta

from m4_sandbox_setup import CANARY, NAME, SNAPSHOTS, credential, docker, operation, owned, save, wsl
from opensandbox import SandboxSync
from opensandbox.exceptions import SandboxApiException
from opensandbox.models import Host, NetworkPolicy, Volume
from opensandbox.models.execd import RunCommandOpts


def check():
    snapshot = SNAPSHOTS + "/fault-" + uuid.uuid4().hex
    wsl("install", "-d", "-m", "755", "-o", "10001", "-g", "10001", snapshot)
    result = {"status": "BLOCKED", "timestamp": time.time(), "snapshot": snapshot, "checks": {}}
    sandbox = None
    env = {}
    killed = False
    try:
        tool, config, env = operation()
        sandbox = SandboxSync.create(
            tool,
            timeout=timedelta(minutes=5),
            ready_timeout=timedelta(seconds=60),
            connection_config=config,
            env=env,
            resource={"cpu": "2", "memory": "2Gi"},
            metadata={"omniharness.m4a": "fault-probe"},
            network_policy=NetworkPolicy(default_action="deny", egress=[]),
            volumes=[Volume(name="workspace", host=Host(path=snapshot), mount_path="/workspace")],
        )
        result["sandbox_id"] = sandbox.id
        checks = result["checks"]
        info = json.loads(docker("inspect", "sandbox-" + sandbox.id))[0]
        pid = info["State"]["Pid"]
        sidecar = json.loads(docker("inspect", "sandbox-egress-" + sandbox.id))[0]
        if info["HostConfig"]["NetworkMode"] != "container:" + sidecar["Id"]:
            raise RuntimeError("Unexpected sandbox network namespace; no fault injected")
        ip = sidecar["NetworkSettings"]["IPAddress"]
        owned(CANARY)
        canary_command = (
            "import urllib.request; print(urllib.request.urlopen('http://172.17.0.1:19081',timeout=3).status)"
        )
        canary_before = docker("exec", NAME, "python", "-c", canary_command)
        if canary_before != "200":
            raise RuntimeError("Positive network canary is unavailable before fault")
        code = (
            """import json,urllib.request,urllib.error
url='http://%s:44772/command'
try:
 r=urllib.request.urlopen(urllib.request.Request(url,data=b'{"command":"true"}',headers={'Content-Type':'application/json'}),timeout=3)
 print(r.status)
except urllib.error.HTTPError as e:print(e.code)
except OSError:print('unreachable')
"""
            % ip
        )
        peer_status = docker("exec", CANARY, "python", "-c", code)
        checks["peer_execd_auth"] = {
            "pass": peer_status in ("401", "403"),
            "unauthenticated_status": peer_status,
        }
        monitor = """import socket,time,json
from pathlib import Path
p=Path('/workspace/network-monitor.jsonl')
deadline=time.monotonic()+20
while time.monotonic()<deadline and not Path('/workspace/monitor-stop').exists():
 try:
  s=socket.create_connection(('172.17.0.1',19081),timeout=.35);s.close();ok=True
 except OSError:ok=False
 with p.open('a') as f:f.write(json.dumps({'time':time.time(),'connected':ok})+'\\n')
 time.sleep(.05)
"""
        sandbox.commands.run(
            ["python", "-c", monitor],
            opts=RunCommandOpts(background=True, timeout=timedelta(seconds=20), uid=10001, gid=10001),
        )
        time.sleep(1)
        injected_at = time.time()
        docker("stop", "--time", "3", sidecar["Id"])
        stopped_at = time.time()
        stopped = json.loads(docker("inspect", sidecar["Id"]))[0]["State"]
        main_running = json.loads(docker("inspect", "sandbox-" + sandbox.id))[0]["State"]["Running"]
        time.sleep(4)
        canary_after = docker("exec", NAME, "python", "-c", canary_command)
        wsl("touch", snapshot + "/monitor-stop")
        rows = [json.loads(line) for line in wsl("cat", snapshot + "/network-monitor.jsonl").splitlines()]
        before = [r for r in rows if r["time"] < injected_at]
        after = [r for r in rows if r["time"] >= injected_at]
        confirmed_after = [r for r in rows if r["time"] >= stopped_at]
        sustained = (
            len(confirmed_after) >= 3 and confirmed_after[-1]["time"] - confirmed_after[0]["time"] >= 2
        )
        checks["egress_sidecar_stop"] = {
            "pass": bool(before)
            and sustained
            and main_running
            and not stopped["Running"]
            and stopped["ExitCode"] in (0, 143)
            and canary_after == "200"
            and not any(r["connected"] for r in rows),
            "before_attempts": len(before),
            "after_attempts": len(after),
            "after_successes": sum(r["connected"] for r in after),
            "fault_injected_at": injected_at,
            "sidecar_stopped_at": stopped_at,
            "confirmed_stopped_attempts": len(confirmed_after),
            "canary_before": canary_before,
            "canary_after": canary_after,
            "main_still_running": main_running,
            "sidecar_exit_code": stopped["ExitCode"],
            "observations": rows,
        }
        started = time.monotonic()
        sandbox.kill()
        killed = True
        try:
            sandbox.get_info()
            missing = False
        except SandboxApiException as exc:
            missing = exc.status_code == 404
        main_exists = bool(docker("ps", "-aq", "--filter", "name=^/sandbox-" + sandbox.id + "$"))
        sidecar_exists = bool(docker("ps", "-aq", "--filter", "name=^/sandbox-egress-" + sandbox.id + "$"))
        pid_exists = bool(wsl("sh", "-c", "if [ -e /proc/" + str(pid) + "/stat ]; then echo present; fi"))
        checks["kill_cleanup"] = {
            "pass": missing and not main_exists and not sidecar_exists and not pid_exists,
            "get_info_404": missing,
            "container_exists": main_exists,
            "sidecar_exists": sidecar_exists,
            "original_pid_exists": pid_exists,
            "seconds": round(time.monotonic() - started, 3),
        }
        result["status"] = "PASS" if all(c["pass"] for c in checks.values()) else "BLOCKED"
    except Exception as exc:
        message = str(exc).replace(credential(), "[REDACTED]")
        if env.get("EXECD_ACCESS_TOKEN"):
            message = message.replace(env["EXECD_ACCESS_TOKEN"], "[REDACTED]")
        result["error"] = {"type": type(exc).__name__, "message": message[:1200]}
    finally:
        if sandbox:
            try:
                sandbox.close() if killed else sandbox.destroy()
            except SandboxApiException as exc:
                if exc.status_code != 404:
                    result["cleanup_error"] = type(exc).__name__
            result["container_removed"] = not docker(
                "ps", "-aq", "--filter", "name=^/sandbox-" + sandbox.id + "$"
            )
        save("fault-report.json", result)
    return result


if __name__ == "__main__":
    report = check()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(report["status"] != "PASS")
