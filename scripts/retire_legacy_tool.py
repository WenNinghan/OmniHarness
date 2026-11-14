"""Audited one-case local WSL maintenance. No model calls or operation replay.

inspect/self-test do not stop production. execute requires verified cached
self-tests, quiesces only the existing 8787 profile and restores its old image.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg
from sqlalchemy.engine import make_url

from omniharness.legacy_tool_retirement import LegacyMaintenanceScope, digest, validate_closed_epoch
from omniharness.sandbox import operation_metadata
from scripts import m4_production_deploy as release

RUN = "163a0930-ecfd-4283-8ce9-16f79010010d"
OPERATION = "18821e0e590750eb2f3949a0aff7a70b567489b8b6525548d684bb4b405c4ccf"
HISTORICAL_SANDBOX = "3e7a1353-8f43-4ad7-aef2-2f97512db93b"
APP = "sha256:b22ba966ff9640f3c52ad7cb802a30e5fc03a975da1104a31b9af52621555a7c"
CONTROLLER = "sha256:0fa4658bb6573f7ef8aae710b7dcb14291e1a486013b9377e6555deb330f55b0"
PG = "sha256:018dbecf6a31cfed1f83c218b5e428a52ac389ad46e495f72831641db49d85a9"
ENGINE = "50a6b68a-9495-41d4-98b1-ba017d0ca946"
COMMIT = "bea959c7b793b32a893820b97c4eadc7c87fabb0"
AUDIT_ROOT = release.ROOT / ".omniharness/legacy-retirement"
SOURCES = {
    ("sandbox-controller", "/app/opensandbox_server/services/docker/docker_service.py"):
        "b1ca676becc1c778cb96a5e748cfc9eff40778a00e640be9f0b6656d8f13f31e",
    ("sandbox-controller", "/app/opensandbox_server/services/docker/container_ops.py"):
        "b8eddb41fa395b3e039a6ff2c6b53a329eb5c033b45c49e0e15144be1d18f7f6",
    ("worker", "/app/src/omniharness/m3_worker.py"):
        "74a9c16fce3bd35b66135b679cce2c88471f9e2555d690e20163800d1d520532",
    ("worker", "/app/src/omniharness/sandbox.py"):
        "12dd773d06eb2c1cedde8907a3ec906fce9fe41505edb8b527cc76321dcf2643",
}

EPOCH_PROBE = r'''import datetime,hashlib,json,os,pathlib,subprocess,sys
p=json.load(sys.stdin)
def docker(*args): return subprocess.check_output(['docker',*args],stderr=subprocess.DEVNULL)
info=json.loads(docker('info','--format','{{json .}}')); commit=docker('version','--format','{{.Server.GitCommit}}').decode().strip()
assert info['ID']==p['engine_id'] and info['ServerVersion']=='28.3.3' and commit==p['commit']
assert info['LiveRestoreEnabled'] is False and info['DockerRootDir']=='/var/lib/docker'
assert info['Swarm']['LocalNodeState']=='inactive'
ids=docker('ps','-aq','--no-trunc').decode().split(); objects=json.loads(docker('inspect',*ids))
api={x['Id']:x for x in objects}; root=pathlib.Path(info['DockerRootDir'])/'containers'
disk={}
for folder in root.iterdir():
 if not folder.is_dir(): continue
 assert folder==folder.resolve() and len(folder.name)==64
 saved=json.loads((folder/'config.v2.json').read_text()); assert saved['ID']==folder.name
 disk[folder.name]=saved
assert set(api)==set(disk)
assert all((disk[k].get('Config',{}).get('Labels') or {})==(api[k]['Config'].get('Labels') or {}) for k in api)
taskroot=pathlib.Path('/run/docker/containerd/daemon/io.containerd.runtime.v2.task/moby')
assert taskroot.is_dir(); tasks={x.name for x in taskroot.iterdir()}; assert tasks<=set(api)
related=[]; sidecars=[]
for identity,saved in disk.items():
 labels=saved.get('Config',{}).get('Labels') or {}
 if (labels.get('omni_instance')==p['instance_id'] or labels.get('omni_run')==p['run_id']
     or all(labels.get(k)==v for k,v in p['operation_labels'].items() if k.startswith('omni_operation'))
     or saved.get('Name')=='/sandbox-'+p['historical_sandbox']): related.append(identity)
 if labels.get('opensandbox.io/egress-sidecar-for')==p['historical_sandbox']: sidecars.append(identity)
owned={name:api[identity] for name,identity in p['owners'].items()}
allowed={owned[k]['Id'] for k in ('api','worker')}
own_networks=set(owned['sandbox-controller']['NetworkSettings']['Networks'])
assert own_networks=={'omniharness_default'}
def env(obj): return dict(x.split('=',1) for x in obj['Config'].get('Env',[]) if '=' in x)
def key_digest(obj):
 value=env(obj).get('OMNI_SANDBOX_KEY_FILE'); assert value
 path=pathlib.PurePosixPath(value)
 mounts=sorted(obj['Mounts'],key=lambda x:len(x['Destination']),reverse=True)
 for mount in mounts:
  dest=pathlib.PurePosixPath(mount['Destination'])
  if path==dest or dest in path.parents:
   source=pathlib.Path(mount['Source'])/path.relative_to(dest)
   return hashlib.sha256(source.read_bytes().strip()).hexdigest()
 raise ValueError('Configured controller credential mount is not available')
expected_key=key_digest(owned['api']); assert key_digest(owned['worker'])==expected_key
actual_key=env(owned['sandbox-controller']).get('OPENSANDBOX_SERVER_API_KEY'); assert actual_key
assert hashlib.sha256(actual_key.encode()).hexdigest()==expected_key
for obj in objects:
 values=env(obj)
 if obj['State']['Running'] and values.get('OMNI_INSTANCE_ID')==p['instance_id']: assert obj['Id'] in allowed
 if obj['Id'] not in allowed and values.get('OMNI_ENABLE_TOOLS','').lower()=='true':
  assert key_digest(obj)!=expected_key or obj['State']['Status']=='exited'
 if obj['Id']!=owned['sandbox-controller']['Id'] and values.get('OPENSANDBOX_SERVER_API_KEY'):
  assert hashlib.sha256(values['OPENSANDBOX_SERVER_API_KEY'].encode()).hexdigest()!=expected_key
 if obj['State']['Running'] and obj['Id'] not in allowed and values.get('OMNI_SANDBOX_ENDPOINT'):
  assert not (set(obj['NetworkSettings']['Networks']) & own_networks)
boot=int(next(x.split()[1] for x in pathlib.Path('/proc/stat').read_text().splitlines() if x.startswith('btime ')))
processes={}
for folder in pathlib.Path('/proc').iterdir():
 if not folder.name.isdigit(): continue
 try:
  name=(folder/'comm').read_text().strip()
  if name not in ('dockerd','containerd'): continue
  assert name not in processes
  fields=(folder/'stat').read_text().rsplit(')',1)[1].split()
  processes[name]={'pid':int(folder.name),'started_utc':datetime.datetime.fromtimestamp(boot+int(fields[19])/os.sysconf('SC_CLK_TCK'),datetime.timezone.utc).isoformat()}
  if name=='dockerd': assert b'--host=unix:///var/run/docker.sock' in (folder/'cmdline').read_bytes()
 except FileNotFoundError: continue
assert set(processes)=={'dockerd','containerd'}
stopped=[name for name in ('api','worker','sandbox-controller') if owned[name]['State']['Status']=='exited'
         and owned[name]['State']['Pid']==0 and not owned[name]['State']['Running']]
print(json.dumps({'engine_id':info['ID'],'docker_version':info['ServerVersion'],'git_commit':commit,
 'live_restore':info['LiveRestoreEnabled'],'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 'boot_utc':datetime.datetime.fromtimestamp(boot,datetime.timezone.utc).isoformat(),
 'dockerd_started_utc':processes['dockerd']['started_utc'],'containerd_started_utc':processes['containerd']['started_utc'],
 'processes':processes,'disk_api_agree':True,'runtime_tasks_accounted':True,'exclusive_instance':True,
 'related_containers':related,'related_sidecars':sidecars,'stopped_services':stopped,
 'container_count':len(api),'runtime_task_count':len(tasks),'now_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}))
'''

DATABASE_HELPER = r'''import json,sys
from sqlalchemy import text
from omniharness.db import make_engine,make_session_factory
from omniharness.settings import Settings
from omniharness.legacy_tool_retirement import LegacyMaintenanceScope,digest,legacy_snapshot,scope_for_snapshot,require_only_scope,retire_legacy_tool
p=json.load(sys.stdin); settings=Settings(); engine=make_engine(settings); factory=make_session_factory(engine)
def masked(db,scope,snapshot):
 result={}
 exclusions={'tool_operations':('id',scope.operation_id),'operation_records':('operation_id',scope.ledger_id),
             'workspace_leases':('workspace_id',snapshot['workspace_id']),'runs':('id',scope.run_id)}
 for table,(column,value) in exclusions.items():
  rows=list(db.execute(text('SELECT to_jsonb(t) FROM '+table+' t WHERE '+column+' <> :value'),{'value':value}).scalars())
  result[table]=digest(sorted(json.dumps(r,sort_keys=True,default=str) for r in rows))
 events=list(db.execute(text("SELECT to_jsonb(t) FROM run_events t WHERE NOT (run_id=:run AND event_type='tool.reconciled' AND COALESCE(payload->>'maintenance','')='closed_execution_epoch')"),{'run':scope.run_id}).scalars())
 result['prior_events']=digest(sorted(json.dumps(r,sort_keys=True,default=str) for r in events))
 result['target_run']=digest(db.scalar(text("SELECT to_jsonb(r)-'next_event_sequence' FROM runs r WHERE id=:id"),{'id':scope.run_id}))
 result['target_tool']=digest(db.scalar(text("SELECT to_jsonb(t)-ARRAY['status','result','error_code','finished_at','slot_claimed','reclaimed_at'] FROM tool_operations t WHERE id=:id"),{'id':scope.operation_id}))
 result['target_ledger']=digest(db.scalar(text("SELECT to_jsonb(t)-ARRAY['status','result','error_code','finished_at'] FROM operation_records t WHERE operation_id=:id"),{'id':scope.ledger_id}))
 return result
with factory.begin() as db:
 if p['action']=='inspect': db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
 db.execute(text("SET TIME ZONE 'UTC'")); assert db.scalar(text('SELECT current_database()'))==p['expected_database']
 if p['action']=='inspect':
  snapshot=legacy_snapshot(db,p['operation_id']); scope=scope_for_snapshot(settings,snapshot); require_only_scope(db,scope)
  result={'snapshot':snapshot,'scope':scope.manifest(),'masked':masked(db,scope,snapshot)}
 else:
  scope=LegacyMaintenanceScope(**p['scope']); snapshot=p['snapshot']; before=masked(db,scope,snapshot)
  assert before==p['masked']; result=retire_legacy_tool(db,settings,scope,snapshot,p['evidence'],request_id=p['request_id'])
  db.flush(); assert masked(db,scope,snapshot)==before
  count=db.scalar(text("SELECT count(*) FROM run_events WHERE run_id=:run AND event_type='tool.reconciled' AND payload->>'maintenance'='closed_execution_epoch'"),{'run':scope.run_id})
  assert count==1 and result['slot_released'] and result['workspace_released']
  result['unrelated_records_unchanged']=True; result['audit_events']=count
print(json.dumps(result)); engine.dispose()
'''


def inspect_object(identity):
    return json.loads(release.docker("inspect", identity))[0]


def new_audit():
    directory = AUDIT_ROOT / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4))
    release.private_directory(directory)
    return directory


def source_audit(owners, directory):
    if owners["api"]["image"] != APP or owners["worker"]["image"] != APP or owners["sandbox-controller"]["image"] != CONTROLLER:
        raise ValueError("Legacy maintenance applies only to the reviewed deployed images")
    hashes = {}
    for (role, path), expected in SOURCES.items():
        target = directory / (role + "-" + Path(path).name)
        release.docker("cp", owners[role]["id"] + ":" + path, release.mapped(target))
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError("Deployed recovery source differs from the reviewed legacy profile")
        hashes[role + ":" + path] = actual
    report = {"actual_sources": hashes, "docker_commit": COMMIT,
              "moby_restore_review": f"https://github.com/moby/moby/blob/{COMMIT}/daemon/daemon.go#L198",
              "moby_create_review": f"https://github.com/moby/moby/blob/{COMMIT}/daemon/create.go#L48"}
    release.write_json(directory / "source-audit.json", report)
    return digest(report)


def environment_file(owners, directory):
    environment = inspect_object(owners["api"]["id"])["Config"]["Env"]
    path = directory / "private-database.env"
    with path.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(value for value in environment if value.startswith("OMNI_")))
        stream.write("\nPYTHONDONTWRITEBYTECODE=1\nPYTHONPATH=/candidate\n")
    return path


def database_action(env_file, payload, *, network=None, extra_env=None):
    args = ["run", "--rm", "-i", "--network", network or release.PROJECT + "_default",
            "--env-file", release.mapped(env_file)]
    if extra_env:
        args.extend(["--env-file", release.mapped(extra_env)])
    arguments = [*args, "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--mount",
        "type=bind,source=" + release.mapped(release.ROOT / "src") + ",target=/candidate,readonly",
        APP, "python", "-c", DATABASE_HELPER]
    result = subprocess.run(["wsl.exe", "-d", release.DISTRO, "-u", "root", "--exec", "docker", *arguments],
                            input=json.dumps(payload).encode(), capture_output=True, timeout=120)
    if result.returncode:
        diagnostic = result.stderr.decode("utf-8", errors="replace")
        for path in (env_file, extra_env):
            if path:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("OMNI_DATABASE_URL="):
                        password = make_url(line.split("=", 1)[1]).password
                        if password:
                            diagnostic = diagnostic.replace(password, "[redacted]")
        (env_file.parent / "database-helper.stderr").write_text(diagnostic, encoding="utf-8")
        raise RuntimeError("Database helper refused; private diagnostic retained")
    return json.loads(result.stdout)


def collect_epoch(scope, snapshot, owners, source_hash, *, stopped, diagnostics=None):
    payload = {"engine_id": ENGINE, "commit": COMMIT, "instance_id": scope.instance_id,
               "run_id": scope.run_id, "operation_labels": operation_metadata(scope.instance_id, scope.run_id, scope.operation_id),
               "historical_sandbox": HISTORICAL_SANDBOX, "owners": {name: row["id"] for name, row in owners.items()}}
    probe = subprocess.run(["wsl.exe", "-d", release.DISTRO, "-u", "root", "--exec", "python3", "-c", EPOCH_PROBE],
                           input=json.dumps(payload).encode(), capture_output=True, timeout=45)
    if probe.returncode:
        if diagnostics:
            (diagnostics / "epoch-probe.stderr").write_bytes(probe.stderr)
        raise RuntimeError("Host epoch/ownership preflight refused; private diagnostic retained")
    epoch = json.loads(probe.stdout)
    if abs((datetime.now(timezone.utc) - datetime.fromisoformat(epoch["now_utc"])).total_seconds()) > 5:
        raise ValueError("Host clock differs from the maintenance collector")
    epoch.update(broker_stopped=not release.broker_processes(), source_audit_sha256=source_hash)
    evidence = {"kind": "local_closed_execution_epoch_v1", "scope": scope.manifest(),
                "snapshot_sha256": scope.snapshot_sha256, "observed_at": datetime.now(timezone.utc).isoformat(),
                "resource_reclaimed": True, "outcome": "unknown", "replayed": False, "epoch": epoch}
    if stopped:
        validate_closed_epoch(scope, snapshot, evidence)
    else:
        if (epoch["related_containers"] or epoch["related_sidecars"]
                or datetime.fromisoformat(snapshot["tool"]["started_at"]) >= datetime.fromisoformat(epoch["boot_utc"])):
            raise ValueError("Old execution epoch or resource absence is not established")
    return evidence


def inspect_legacy(directory):
    owners = release.ownership()
    source_hash = source_audit(owners, directory)
    env_file = environment_file(owners, directory)
    try:
        plan = database_action(env_file, {"action": "inspect", "operation_id": OPERATION,
                                         "expected_database": release.active_database(owners)})
        scope = LegacyMaintenanceScope(**plan["scope"])
        if scope.run_id != RUN or scope.instance_id != release.INSTANCE:
            raise ValueError("Unexpected legacy production binding")
        evidence = collect_epoch(scope, plan["snapshot"], owners, source_hash, stopped=False, diagnostics=directory)
        plan["preflight_evidence"] = evidence
        release.write_json(directory / "plan.json", plan)
        return owners, plan, source_hash
    finally:
        env_file.unlink(missing_ok=True)


def code_manifest():
    names = ["src/omniharness/legacy_tool_retirement.py", "scripts/retire_legacy_tool.py",
             "scripts/m4_production_deploy.py", "tests/test_legacy_tool_retirement.py",
             "tests/test_legacy_tool_retirement_postgres.py", "tests/test_legacy_maintenance.py", "tests/test_m4_release.py"]
    return {name: hashlib.sha256((release.ROOT / name).read_bytes()).hexdigest() for name in names}


def self_test():
    tested_sources = code_manifest()
    directory = new_audit()
    nonce = "legacy-test-" + secrets.token_hex(5)
    credential = directory / "test-pg.env"
    password = secrets.token_hex(32)
    credential.write_text("POSTGRES_USER=omniharness\nPOSTGRES_DB=omniharness_test\nPOSTGRES_PASSWORD=" + password + "\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    started = False
    try:
        release.image_id(PG)
        release.docker("run", "-d", "--name", nonce, "--label", "omni.legacy.test=" + nonce,
                       "--env-file", release.mapped(credential), "--tmpfs", "/var/lib/postgresql/data",
                       "-p", f"127.0.0.1:{port}:5432", PG)
        started = True
        for _ in range(80):
            try:
                with psycopg.connect(host="127.0.0.1", port=port, user="omniharness", password=password,
                                      dbname="omniharness_test", connect_timeout=1):
                    break
            except psycopg.OperationalError:
                time.sleep(.25)
        else:
            raise RuntimeError("Independent legacy test database did not become ready")
        print("Independent PostgreSQL ready; testing legacy retirement and release guards.", flush=True)
        environment = os.environ.copy()
        environment.update(OMNI_TEST_POSTGRES_URL=f"postgresql+psycopg://omniharness@127.0.0.1:{port}/omniharness_test",
                           PGPASSWORD=password, PYTHONUTF8="1")
        command = [sys.executable, "-m", "pytest", "-q", "-x", "--tb=short", "tests/test_legacy_tool_retirement.py",
                   "tests/test_legacy_tool_retirement_postgres.py", "tests/test_legacy_maintenance.py",
                   "tests/test_m4_release.py", "tests/test_m4_reconciliation.py", "tests/test_m4_reconciliation_postgres.py"]
        result = subprocess.run(command, cwd=release.ROOT, env=environment, capture_output=True, timeout=240)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace").replace(password, "[redacted]")
        (directory / "pytest.txt").write_text(output, encoding="utf-8")
        print(output[-12000:], flush=True)
        if result.returncode:
            raise RuntimeError("Legacy maintenance self-test failed; production was not stopped")
        release.private_directory(AUDIT_ROOT)
        if code_manifest() != tested_sources:
            raise ValueError("Maintenance sources changed during self-test; rerun the exact final sources")
        release.write_json(AUDIT_ROOT / "self-test.json", {"passed": True, "code_manifest": tested_sources,
            "report": str(directory / "pytest.txt"), "test_database": "omniharness_test",
            "at": datetime.now(timezone.utc).isoformat()})
    finally:
        if started:
            obj = inspect_object(nonce)
            if obj["Config"]["Labels"].get("omni.legacy.test") != nonce:
                raise ValueError("Test PostgreSQL ownership changed; no cleanup")
            release.docker("stop", "--time", "10", obj["Id"])
            release.docker("rm", obj["Id"])
        credential.unlink(missing_ok=True)


def assert_same_epoch(before, after):
    for key in ("engine_id", "boot_id", "processes", "git_commit"):
        if before["epoch"][key] != after["epoch"][key]:
            raise ValueError("Execution epoch changed during legacy maintenance")


def other_container_states():
    ids = release.docker("ps", "-aq", "--no-trunc").decode().split()
    objects = json.loads(release.docker("inspect", *ids))
    return {obj["Id"]: {"name": obj["Name"], "image": obj["Image"], "status": obj["State"]["Status"],
                        "started_at": obj["State"]["StartedAt"]} for obj in objects
            if (obj["Config"]["Labels"] or {}).get("com.docker.compose.project") != release.PROJECT}


def execute(directory, owners, plan, source_hash):
    gate = release.read_json(AUDIT_ROOT / "self-test.json")
    if not gate.get("passed") or gate.get("code_manifest") != code_manifest():
        raise ValueError("Run self-test against these exact maintenance sources before execution")
    if any(owners[name]["state"] != "running" for name in ("api", "worker", "sandbox-controller")):
        raise ValueError("This maintenance expects the existing three production services running")
    scope = LegacyMaintenanceScope(**plan["scope"])
    before = release.inventory()
    scope.validate_inventory(before)
    other_before = other_container_states()
    env_file = environment_file(owners, directory)
    request_id = "legacy-" + directory.name

    def stage(value):
        release.write_json(directory / "stage.json", {"stage": value, "at": datetime.now(timezone.utc).isoformat()})
        print(value, flush=True)

    def evidence():
        value = collect_epoch(scope, plan["snapshot"], owners, source_hash, stopped=True, diagnostics=directory)
        assert_same_epoch(plan["preflight_evidence"], value)
        return value

    def payload(proof, database):
        return {**plan, "action": "retire", "evidence": proof, "request_id": request_id,
                "expected_database": database}

    try:
        release.write_json(directory / "maintenance-baseline.json", {"owners": owners, "inventory": before,
            "other_containers": other_before, "plan_sha256": digest(plan), "code_manifest": code_manifest()})
        stage("Quiescing only the owned 8787 API, Worker, broker and controller.")
        release.quiesce(maintenance_scope=scope)
        proof = evidence()
        release.write_json(directory / "closed-epoch-before-backup.json", proof)
        stage("Backing up the complete stopped state, including the original unknown record.")
        backup = release.backup(stopped=True, maintenance_scope=scope)

        def rehearsal(context):
            name = "omni_legacy_rehearsal"
            container = context["database_container"]
            obj = inspect_object(container)
            if not obj["Config"]["Labels"].get("omni.m4.restore"):
                raise ValueError("Legacy rehearsal requires an owned isolated restore database")
            release.docker("exec", container, "createdb", "-U", "omniharness", "-O", "omniharness", name)
            release.docker("exec", "-i", container, "pg_restore", "-U", "omniharness", "-d", name,
                           "--no-owner", "--no-acl", "--exit-on-error", data=(backup / "database.pgdump").read_bytes())
            original = context["credential"].read_text(encoding="utf-8")
            connection = next(line.split("=", 1)[1] for line in original.splitlines() if line.startswith("OMNI_DATABASE_URL="))
            target = make_url(connection).set(database=name).render_as_string(hide_password=False)
            extra = directory / "private-rehearsal.env"
            extra.write_text("OMNI_DATABASE_URL=" + target + "\n", encoding="utf-8")
            try:
                body = payload(evidence(), name)
                first = database_action(env_file, body, network=context["network"], extra_env=extra)
                second = database_action(env_file, body, network=context["network"], extra_env=extra)
                if first["already_retired"] or not second["already_retired"]:
                    raise ValueError("Restored legacy retirement was not idempotent")
                result = {"restored_original_database": True, "retirement": first, "repeat_is_idempotent": True}
                release.write_json(directory / "restored-database-rehearsal.json", result)
                return result
            finally:
                extra.unlink(missing_ok=True)

        stage("Verifying full restore, then rehearsing retirement on a third independent database.")
        release.verify(backup, APP, maintenance_probe=rehearsal)
        if release.stopped_inventory(owners) != before:
            raise ValueError("Production database changed during the independent recovery rehearsal")
        proof = evidence()
        release.write_json(directory / "closed-epoch-before-retirement.json", proof)
        stage("Retiring the exact legacy operation; result remains unknown and no command is replayed.")
        retired = database_action(env_file, payload(proof, release.active_database(owners)))
        release.write_json(directory / "retirement.json", retired)
        after = release.stopped_inventory(owners)
        release.require_quiet(after)
        allowed = {"public.tool_operations", "public.operation_records", "public.workspace_leases", "public.runs", "public.run_events"}
        if (set(before["tables"]) != set(after["tables"])
                or any(before["tables"][name] != after["tables"][name] for name in before["tables"] if name not in allowed)
                or before["output_limits"] != after["output_limits"]):
            raise ValueError("Retirement modified records outside its approved audit/resource scope")
        assert_same_epoch(proof, evidence())
        stage("Saving and restore-verifying the final quiet state before restarting 8787.")
        final_backup = release.backup(stopped=True)
        release.verify(final_backup, APP)
        stage("Restoring the original controller, broker, API and Worker.")
        release.resume_owned(owners)
        for _ in range(60):
            try:
                if httpx.get("http://127.0.0.1:8787/health", timeout=2, trust_env=False).json().get("status") == "ok":
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.5)
        else:
            raise RuntimeError("Original 8787 services did not become healthy")
        current = release.inventory()
        release.require_quiet(current)
        if current != after:
            raise ValueError("Data changed while restoring the old production services")
        if other_container_states() != other_before:
            raise ValueError("An unrelated container changed during maintenance; audit its independent lifecycle")
        result = {"passed": True, "retirement": retired, "original_backup": str(backup),
                  "quiet_backup": str(final_backup), "url": "http://127.0.0.1:8787",
                  "application_image": APP, "real_model_calls": 0, "model_and_budget_tables_unchanged": True,
                  "other_containers_unchanged": len(other_before),
                  "active_runs": current["active_runs"], "unsafe_tools": current["unsafe_tools"],
                  "audit": str(directory)}
        release.write_json(directory / "result.json", result)
        print(json.dumps(result), flush=True)
    finally:
        env_file.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "self-test", "execute"])
    args = parser.parse_args()
    if args.action == "self-test":
        self_test()
        return
    with release.deployment_lock():
        directory = new_audit()
        owners, plan, source_hash = inspect_legacy(directory)
        if args.action == "inspect":
            print(json.dumps({"preflight_passed": True, "audit": str(directory), "operation_id": OPERATION}))
        else:
            execute(directory, owners, plan, source_hash)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "inspect private evidence"
        print("Legacy maintenance stopped: " + message + ". No unknown command was replayed.", file=sys.stderr)
        raise SystemExit(1) from None
