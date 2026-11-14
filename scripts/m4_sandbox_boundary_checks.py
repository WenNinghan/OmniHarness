"""Adversarial checks run only against a disposable M4-A fixture sandbox.

Never outputs credential values. Tests the native execd token separately from
Kubernetes ingress secure_access. A rejected control boundary remains BLOCKED.
"""

import json
import time
import uuid
from datetime import timedelta

from m4_sandbox_checks import python
from m4_sandbox_setup import DATA, SNAPSHOTS, credential, docker, operation, save, wsl
from opensandbox import SandboxSync
from opensandbox.models import Host, NetworkPolicy, Volume
from opensandbox.models.execd import RunCommandOpts


def check():
    snapshot = SNAPSHOTS + "/auth-" + uuid.uuid4().hex
    wsl("install", "-d", "-m", "755", "-o", "10001", "-g", "10001", snapshot)
    tool, config, env = operation()
    token = env["EXECD_ACCESS_TOKEN"]
    result = {"status": "BLOCKED", "timestamp": time.time(), "checks": {}}
    sandbox = None
    try:
        sandbox = SandboxSync.create(
            tool,
            timeout=timedelta(minutes=5),
            ready_timeout=timedelta(seconds=60),
            connection_config=config,
            resource={"cpu": "2", "memory": "2Gi"},
            metadata={"omniharness.m4a": "auth-probe"},
            env=env,
            network_policy=NetworkPolicy(default_action="deny", egress=[]),
            volumes=[Volume(name="workspace", host=Host(path=snapshot), mount_path="/workspace")],
        )
        result["sandbox_id"] = sandbox.id
        checks = result["checks"]
        from opensandbox.exceptions import SandboxApiException

        try:
            uid0 = sandbox.commands.run(
                [
                    "python",
                    "-c",
                    "from pathlib import Path;Path('/workspace/uid0-executed').write_text('ran')",
                ],
                opts=RunCommandOpts(uid=0, gid=0, timeout=timedelta(seconds=3)),
            )
            checks["uid0_rejected"] = {
                "pass": uid0.error is not None or uid0.exit_code not in (0, None),
                "exit_code": uid0.exit_code,
                "error": uid0.error.name if uid0.error else None,
            }
        except SandboxApiException as exc:
            explicit_denial = (
                exc.status_code == 500
                and exc.error.code == "RUNTIME_ERROR"
                and "M4 commands require uid=10001 gid=10001" in (exc.error.message or "")
            )
            checks["uid0_rejected"] = {
                "pass": exc.status_code in (400, 401, 403) or explicit_denial,
                "status_code": exc.status_code,
                "explicit_identity_denial": explicit_denial,
            }
        marker_exists = (
            python(
                sandbox, "from pathlib import Path;print(Path('/workspace/uid0-executed').exists())"
            ).strip()
            == "True"
        )
        checks["uid0_rejected"]["marker_absent"] = not marker_exists
        checks["uid0_rejected"]["pass"] &= not marker_exists
        audit = """from pathlib import Path
import json,os,urllib.request,urllib.error
def request(headers=None):
 try:
  req=urllib.request.Request('http://127.0.0.1:44772/command',data=b'{"command":"true"}',
    headers={'Content-Type':'application/json',**(headers or {})})
  with urllib.request.urlopen(req,timeout=3) as r:return r.status
 except urllib.error.HTTPError as e:return e.code
found=None
for path in [*Path('/proc').glob('[0-9]*/environ'),Path('/opt/opensandbox/.env'),Path('/opt/omniharness/execd.env')]:
 try:
  for entry in path.read_bytes().replace(b'\\n',b'\\x00').split(b'\\x00'):
   name,sep,value=entry.partition(b'=')
   if sep and name==b'EXECD_ACCESS_TOKEN' and value:found=value.decode()
 except OSError:pass
print(json.dumps({'native_auth_status':request(),'own_environment_has_token':'EXECD_ACCESS_TOKEN' in os.environ,
 'proc_or_file_token_readable':found is not None,
 'retrieved_token_accepted':request({'X-EXECD-ACCESS-TOKEN':found})==200 if found else False}))
"""
        values = json.loads(python(sandbox, audit))
        checks["native_token"] = {"pass": values["native_auth_status"] == 401, **values}
        checks["credential_separation"] = {
            "pass": not values["proc_or_file_token_readable"] and not values["own_environment_has_token"]
        }
        paths = """from pathlib import Path
import os,json
print(json.dumps({'docker_socket':Path('/var/run/docker.sock').exists(),
 'host_drive':Path('/mnt/g').exists(),'daemon_env_readable':os.access('/opt/omniharness/execd.env',os.R_OK),
 'runtime_writable':os.access('/opt/opensandbox',os.W_OK),
 'profile_writable':os.access('/opt/omniharness/execd-hardening.toml',os.W_OK)}))
"""
        values = json.loads(python(sandbox, paths))
        checks["filesystem_separation"] = {"pass": not any(values.values()), **values}
        rebind = """import json,urllib.request,urllib.error,hashlib
body={'sandboxId':'omni-m4a-internal-fixture','generation':999,'preserveRuntimeState':True,
      'accessTokenHash':'sha256:'+hashlib.sha256(b'omni-m4a-diagnostic').hexdigest()}
r=urllib.request.Request('http://127.0.0.1:44772/internal/init',data=json.dumps(body).encode(),
 headers={'Content-Type':'application/json'})
try:
 with urllib.request.urlopen(r,timeout=3) as response:status=response.status
except urllib.error.HTTPError as e:status=e.code
r=urllib.request.Request('http://127.0.0.1:44772/command',data=b'{"command":"true"}',
 headers={'Content-Type':'application/json','X-EXECD-ACCESS-TOKEN':'omni-m4a-diagnostic'})
try:
 with urllib.request.urlopen(r,timeout=3) as response:replacement=response.status
except urllib.error.HTTPError as e:replacement=e.code
print(json.dumps({'unauthenticated_init_status':status,'chosen_token_status':replacement}))
"""
        values = json.loads(python(sandbox, rebind))
        checks["internal_init_rejected"] = {
            "pass": values["unauthenticated_init_status"] in (401, 403, 404),
            **values,
        }
        outside = DATA + "/unauthorized-fixture"
        wsl("install", "-d", "-m", "755", outside)
        link = SNAPSHOTS + "/outside-link-" + uuid.uuid4().hex
        wsl("ln", "-s", outside, link)
        rejected = []
        for path in (outside, link):
            extra = None
            try:
                extra = SandboxSync.create(
                    tool,
                    timeout=timedelta(seconds=60),
                    ready_timeout=timedelta(seconds=30),
                    connection_config=config,
                    env=env,
                    resource={"cpu": "1", "memory": "256Mi"},
                    network_policy=NetworkPolicy(default_action="deny", egress=[]),
                    volumes=[Volume(name="workspace", host=Host(path=path), mount_path="/workspace")],
                )
                rejected.append(False)
            except SandboxApiException as exc:
                rejected.append(exc.status_code in (400, 403))
            finally:
                if extra:
                    extra.destroy()
        checks["outside_mount_rejected"] = {
            "pass": all(rejected),
            "direct": rejected[0],
            "symlink": rejected[1],
        }
        result["status"] = "PASS" if all(c["pass"] for c in checks.values()) else "BLOCKED"
    except Exception as exc:
        result["error"] = {
            "type": type(exc).__name__,
            "message": str(exc).replace(token, "[REDACTED]").replace(credential(), "[REDACTED]")[:1200],
        }
    finally:
        if sandbox:
            sandbox.destroy()
            result["container_removed"] = not docker(
                "ps", "-aq", "--filter", "name=^/sandbox-" + sandbox.id + "$"
            )
        save("native-auth-report.json", result)
    return result


if __name__ == "__main__":
    report = check()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(report["status"] != "PASS")
