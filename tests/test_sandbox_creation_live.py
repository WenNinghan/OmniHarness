"""Opt-in real service/Worker/SDK/PG checks; no paid model or deployed state.

Only scripts/verify_creation_service.py provisions this environment. The broker
uses real native filtered WorkspaceIO plus a fixture exporter, not the deployed
broker HTTP service. All containers and files belong to the acceptance nonce.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema
from omniharness.cli import run_async
from omniharness.db import make_engine, make_session_factory
from omniharness.m3_worker import _checkpoint_url
from omniharness.m4_services import record_workspace_probe, register_workspace
from omniharness.models import (
    OperationRecord,
    Run,
    ToolOperation,
    Workspace,
    WorkspaceLease,
    WorkspacePolicy,
    WorkspaceSnapshot,
)
from omniharness.sandbox import SandboxConfig, SandboxResultUnknown
from omniharness.sandbox_creation_api import CreationRequest
from omniharness.sandbox_creation_client import CreationServiceClient, creation_service_binding
from omniharness.schemas import SendMessageRequest, WorkspaceRequest
from omniharness.security import create_pairing_code, init_secrets
from omniharness.services import enqueue
from omniharness.settings import Settings
from omniharness.worker import Worker, WorkerLock
from omniharness.workspace_io import WorkspaceGrant, WorkspaceIO

WSL = ["wsl.exe", "-d", "OmniHarnessCheckAlt", "-u", "root", "--exec"]


class LiveEnvironment(dict):
    def __repr__(self):
        return "Isolated creation environment " + self["nonce"]


@pytest.fixture(scope="module")
def event_loop_policy():
    return asyncio.WindowsSelectorEventLoopPolicy() if sys.platform == "win32" else asyncio.get_event_loop_policy()


def settings_for(state, directory):
    return Settings(_env_file=None, database_url=state["database_url"], data_dir=directory / "data",
        secret_dir=directory / "secrets", static_dir=directory / "no-web", allowed_origins="http://testserver",
        instance_id=state["instance_id"], enable_agent=True, enable_tools=True,
        sandbox_endpoint=state["controller_endpoint"], sandbox_key_file=directory / "controller.key",
        sandbox_image=state["tool_image"], sandbox_snapshot_root=state["linux_root"] + "/snapshots",
        sandbox_creation_url=state["service_url"], sandbox_creation_service_id=state["service_id"],
        sandbox_creation_key_file=directory / "creation.key", checkpoint_schema="creation_" + state["nonce"].replace("-", "_"),
        workspace_service_key_file=directory / "workspace.key")


@pytest.fixture(scope="module")
def live():
    configured = os.environ.get("OMNI_CREATION_LIVE_STATE")
    if not configured:
        pytest.skip("Run scripts/verify_creation_service.py for the isolated live profile")
    path = Path(configured).resolve()
    state = LiveEnvironment(json.loads(path.read_text(encoding="utf-8")))
    assert re.fullmatch(r"sol-[a-f0-9]{12}", state["nonce"])
    assert state["instance_id"] == "creation-" + state["nonce"]
    assert path.parent.name == state["nonce"] and path.parent.parent.name == "creation-validation"
    url = make_url(state["database_url"])
    assert url.database == "omniharness_test" and url.host == "127.0.0.1"
    settings = settings_for(state, path.parent)
    settings.workspace_service_key_file.write_text("fixture-broker-key-" * 4)
    init_secrets(settings)
    engine = make_engine(settings)
    with engine.connect() as db:
        assert db.scalar(text("SELECT current_database()")) == "omniharness_test"
    factory = make_session_factory(engine)
    run_async(initialize_checkpoint_schema(_checkpoint_url(settings.database_url), schema=settings.checkpoint_schema))
    state.update(settings=settings, engine=engine, factory=factory, directory=path.parent)
    yield state
    engine.dispose()


class CommandModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "isolated-creation-offline-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, **kwargs):
        results = [message for message in messages if isinstance(message, ToolMessage)]
        if results:
            assert json.loads(results[-1].content)["exit_code"] == 0
            response = AIMessage(content="隔离命令已返回退出码 0。", response_metadata={"finish_reason": "stop"})
        else:
            response = AIMessage(content="", tool_calls=[{"id": "live-command", "name": "run_command",
                "args": {"command": ["python", "-m", "pytest", "-q"]}}])
        response.usage_metadata = {"input_tokens": 30, "output_tokens": 4, "total_tokens": 34,
                                   "input_token_details": {"cache_read": 0}}
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(self, messages, **kwargs):
        return self._generate(messages, **kwargs)


class Registry:
    def __init__(self, factory):
        self.factory = factory

    def get(self, workspace_id, policy_version):
        with self.factory() as db:
            workspace = db.get(Workspace, workspace_id)
            policy = db.get(WorkspacePolicy, (workspace_id, policy_version))
            if workspace is None or policy is None:
                return None
            return WorkspaceGrant(**{**policy.grant, "enabled": workspace.enabled})


class SnapshotBroker:
    def __init__(self, live, io):
        self.live, self.io = live, io

    async def request(self, workspace_id, version, action, **payload):
        assert action == "snapshot"
        original = self.io.snapshot(workspace_id, self.live["instance_id"], version, payload["run_id"])
        workdir = self.live["linux_root"] + "/snapshots/" + original["snapshot_id"] + "/workspace"
        files = {entry["path"]: base64.b64encode((Path(original["workdir"]) / entry["path"]).read_bytes()).decode()
                 for entry in original["files"]}
        program = '''import base64,json,os,pathlib,re,sys
p=json.load(sys.stdin); root=pathlib.Path(p['root']); sid=p['snapshot_id']
assert re.fullmatch(r'[a-f0-9-]{36}',sid) and root==root.resolve() and root.is_dir()
dest=root/sid/'workspace'; dest.mkdir(parents=True,mode=0o755)
for rel,value in p['files'].items():
 path=pathlib.PurePosixPath(rel)
 assert not path.is_absolute() and '..' not in path.parts
 target=dest/rel; target.parent.mkdir(parents=True,exist_ok=True)
 target.write_bytes(base64.b64decode(value))
for directory,_,names in os.walk(dest):
 for name in names: os.chown(pathlib.Path(directory)/name,10001,10001)
 os.chown(directory,10001,10001)
'''
        result = await asyncio.to_thread(subprocess.run, [*WSL, "python3", "-c", program],
            input=json.dumps({"root": self.live["linux_root"] + "/snapshots", "snapshot_id": original["snapshot_id"],
                              "files": files}).encode(), capture_output=True, timeout=20)
        assert result.returncode == 0, "Fixture snapshot export failed"
        return {**original, "workdir": workdir, "manifest_path": workdir.removesuffix("workspace") + "manifest.json"}


def prepare(live, tmp_path, client):
    settings, factory = live["settings"], live["factory"]
    project = tmp_path / "actual-project"
    project.mkdir()
    (project / "calculator.py").write_text("def twice(value):\n    return value * 2\n")
    (project / "test_calculator.py").write_text("from calculator import twice\ndef test_positive():\n    assert twice(7) == 14\n"
        "def test_negative():\n    assert twice(-3) == -6\n")
    io = WorkspaceIO(Registry(factory), tmp_path / "native-broker", protected_roots=(settings.secret_dir,))
    with factory.begin() as db:
        workspace = register_workspace(db, WorkspaceRequest(name="live-fixture", host_path=str(project),
                                        write_allow=["."], command_approval=False), settings)
        workspace_id = workspace.id
    # Probe after the immutable grant exists in the registry's committed view.
    probe = io.probe(workspace_id, settings.instance_id, 1)
    with factory.begin() as db:
        record_workspace_probe(db, workspace_id, probe)
    model = client.post("/api/v1/model-configs", json={"name": "offline-live-" + uuid4().hex[:8],
        "adapter_type": "openai-compatible", "base_url": "http://127.0.0.1:1/v1", "model_name": "fixture-model",
        "context_policy": {"schema_version": 1, "policy": {"version": "context-v3"}}})
    assert model.status_code == 201, model.text
    from omniharness.models import ModelConfig
    with factory.begin() as db:
        db.get(ModelConfig, model.json()["id"]).capabilities = {"tools": {"status": "supported"}}
    conversation = client.post("/api/v1/conversations", json={}).json()
    with factory.begin() as db:
        run_id = enqueue(db, conversation["id"], SendMessageRequest(content="运行两个隔离测试并保存结果。",
            model_config_id=model.json()["id"], client_request_id=uuid4().hex, workflow_type="agent",
            workspace_id=workspace_id, agent_strategy="single"), settings)["run_id"]
        frozen = db.get(Run, run_id).config_snapshot["workspace"]
        assert frozen["creation_service"] == creation_service_binding(settings)
    worker = Worker(settings, live["engine"])
    worker.agent_model_factory = lambda *_: CommandModel()
    worker.workspace_broker = SnapshotBroker(live, io)
    return worker, run_id, frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["normal", "lost_worker_receipt", "cancel_after_create"])
async def test_real_worker_command_and_missing_id_reconciliation(live, tmp_path, monkeypatch, case):
    settings, factory = live["settings"], live["factory"]
    connection = SandboxConfig.connection
    captures = []

    async def capture_response(response):
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            source = response.stream
            entry = {"bytes": "", "headers": {name: response.headers.get(name) for name in (
                "content-type", "content-length", "transfer-encoding")}}
            captures.append(entry)

            class TraceStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    body = bytearray()
                    try:
                        async for chunk in source:
                            if len(body) < 65536:
                                body.extend(chunk[:65536 - len(body)])
                            yield chunk
                    finally:
                        entry["bytes"] = base64.b64encode(body).decode()
                        (live["directory"] / (case + "-sse.json")).write_text(json.dumps(captures), encoding="utf-8")

                async def aclose(self):
                    await source.aclose()

            response.stream = TraceStream()

    def traced_connection(self, **kwargs):
        value = connection(self, **kwargs)
        value.event_hooks["response"].insert(0, capture_response)
        return value

    monkeypatch.setattr(SandboxConfig, "connection", traced_connection)
    with TestClient(create_app(settings, live["engine"]), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, frozen = prepare(live, tmp_path, client)
        actual_create, returned_ids = CreationServiceClient.create, []

        async def create(self, request):
            identity = await actual_create(self, request)
            returned_ids.append(identity)
            if case == "lost_worker_receipt":
                raise SandboxResultUnknown("Injected response loss after durable service receipt")
            if case == "cancel_after_create":
                with factory.begin() as db:
                    db.get(Run, run_id).status = "cancel_requested"
            return identity

        monkeypatch.setattr(CreationServiceClient, "create", create)
        with WorkerLock(live["engine"], settings) as owner:
            worker.lock = owner
            assert await worker.run_once() == run_id
        with factory() as db:
            run = db.get(Run, run_id)
            operation = db.scalar(select(ToolOperation).where(ToolOperation.run_id == run_id))
            assert operation is not None, {"status": run.status, "error": run.error_code, "message": run.error_message}
            assert len(returned_ids) == 1, {"status": run.status, "error": run.error_code, "message": run.error_message}
            request = CreationRequest(run_id=run_id, operation_id=operation.id,
                operation_fingerprint=operation.binding_fingerprint,
                snapshot_path=db.get(WorkspaceSnapshot, operation.snapshot_id).workdir,
                timeout_seconds=operation.parameters["timeout_seconds"])
            operation_id, fingerprint = operation.id, operation.binding_fingerprint
            if case == "normal":
                assert run.status == "completed", {"error": run.error_code, "message": run.error_message}
                assert operation.status == "completed" and operation.result["exit_code"] == 0
                assert "2 passed" in operation.result["stdout_preview"]
                assert not operation.slot_claimed and db.get(WorkspaceLease, run.workspace_id) is None
            else:
                assert operation.status == "uncertain"
                assert operation.command_id is None  # No workload was dispatched.
                assert operation.slot_claimed == (case == "lost_worker_receipt")
                assert (operation.sandbox_id is None) == (case == "lost_worker_receipt")
        if case != "normal":
            assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
            response = client.post(f"/api/v1/tool-operations/{operation_id}/reconcile", json={
                "fingerprint": fingerprint, "acknowledge_unverified": True, "client_request_id": "live-reconcile"})
            assert response.status_code == 200, response.text
            assert response.json()["reconciliation"]["outcome"] == "unknown"
            assert response.json()["operation"]["sandbox_id"] == returned_ids[0]
            assert response.json()["workspace_released"]
            with factory() as db:
                operation = db.get(ToolOperation, operation_id)
                assert not operation.slot_claimed and operation.status == "reconciled"
                assert db.get(OperationRecord, operation.operation_record_id).status == "reconciled"
        remote = CreationServiceClient(settings, frozen["creation_service"])
        status = await remote.status(request)
        assert status["state"] == "CREATED" and status["sandbox_id"] == returned_ids[0]
        assert (await remote.reclaim(request))["evidence"]["resource_reclaimed"]
        # A second create request is rejected, even if its caller lost a reply.
        monkeypatch.setattr(CreationServiceClient, "create", actual_create)
        with pytest.raises(SandboxResultUnknown):
            await remote.create(request.model_copy(update={"execd_token": SecretStr("x" * 32)}))
        value = client.get(f"/api/v1/runs/{run_id}/observation").json()
        assert value["resources"] == {"tool_slots_held": 0, "workspace_lease_held": False}
        if case == "normal":
            assert value["phase"] == "done" and value["evidence"]["command_results"][0]["exit_code"] == 0
            assert value["cache"]["cache_read_input_tokens"]["value"] == 0
            assert value["cache"]["cache_creation_input_tokens"]["value"] is None
        else:
            assert value["evidence"]["command_results"][0]["outcome"] == "unknown"
        report = {"case": case, "run_id": run_id, "operation_id": operation_id, "sandbox_id": returned_ids[0],
            "resource_reclaimed": True, "slot_released": True, "lease_released": True,
            "outcome": "exit_code_0" if case == "normal" else "unknown", "broker": "native-filtered-fixture-export",
            "model": "deterministic_offline", "observation": value}
        (live["directory"] / (case + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


async def worker_at_create_barrier(state_path, project_path, barrier_path):
    """Child process: stop after the service receipt but before Worker ID save."""
    state_path = Path(state_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    settings = settings_for(state, state_path.parent)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    state.update(settings=settings, engine=engine, factory=factory, directory=state_path.parent)
    io = WorkspaceIO(Registry(factory), Path(project_path).parent / "killed-broker", protected_roots=(settings.secret_dir,))
    worker = Worker(settings, engine)
    worker.agent_model_factory = lambda *_: CommandModel()
    worker.workspace_broker = SnapshotBroker(state, io)
    original = CreationServiceClient.create

    async def at_barrier(self, request):
        identity = await original(self, request)
        Path(barrier_path).write_text(json.dumps({"sandbox_id": identity, "operation_id": request.operation_id}), encoding="utf-8")
        await asyncio.Event().wait()

    CreationServiceClient.create = at_barrier
    with WorkerLock(engine, settings) as owner:
        worker.lock = owner
        await worker.run_once()


@pytest.mark.asyncio
async def test_real_worker_process_death_recovers_id_without_replaying(live, tmp_path, monkeypatch):
    from omniharness.m3_worker import recover_agents
    settings, factory = live["settings"], live["factory"]
    with TestClient(create_app(settings, live["engine"]), headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        worker, run_id, _ = prepare(live, tmp_path, client)
        with factory() as db:
            project = db.get(Workspace, db.get(Run, run_id).workspace_id).host_path
        barrier = live["directory"] / ("worker-barrier-" + run_id + ".json")
        program = ("import sys; from omniharness.cli import run_async; "
                   "from test_sandbox_creation_live import worker_at_create_barrier; "
                   "run_async(worker_at_create_barrier(*sys.argv[1:]))")
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONPATH"] = str(Path("tests").resolve())
        with (live["directory"] / "killed-worker.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, "-c", program, str(live["directory"] / "state.json"),
                project, str(barrier)], env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            try:
                deadline = time.monotonic() + 60
                while not barrier.exists() and process.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                assert barrier.exists() and process.poll() is None, "Child Worker did not reach its durable-create barrier"
                receipt = json.loads(barrier.read_text(encoding="utf-8"))
                process.kill()
                await asyncio.to_thread(process.wait, timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=10)
        with factory() as db:
            operation = db.get(ToolOperation, receipt["operation_id"])
            assert operation.run_id == run_id and operation.sandbox_id is None and operation.slot_claimed
        replays = []

        async def never_create(*args):
            replays.append(True)
            raise AssertionError("A restarted Worker must not replay an unknown create")

        monkeypatch.setattr(CreationServiceClient, "create", never_create)
        with WorkerLock(live["engine"], settings) as owner:
            worker.lock = owner
            await recover_agents(worker)
            assert await worker.run_once() is None
        assert not replays
        with factory() as db:
            operation = db.get(ToolOperation, receipt["operation_id"])
            assert db.get(Run, run_id).status == "pending_review"
            # A killed process can leave the request's last saved DISPATCHED
            # state. The uncertain ledger and pending-review Run are the
            # recovery facts; no synthetic tool completion should be written.
            assert operation.status in {"dispatched", "running", "uncertain"}
            assert operation.sandbox_id is None and operation.slot_claimed
            assert db.get(OperationRecord, operation.operation_record_id).status == "uncertain"
            fingerprint = operation.binding_fingerprint
        observation = client.get(f"/api/v1/runs/{run_id}/observation").json()
        assert observation["phase"] == "needs_reconciliation"
        assert observation["execution"]["uncertain_tools"] == 1 and observation["execution"]["active_tools"] == 0
        assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
        response = client.post(f"/api/v1/tool-operations/{receipt['operation_id']}/reconcile", json={
            "fingerprint": fingerprint, "acknowledge_unverified": True, "client_request_id": "worker-killed"})
        assert response.status_code == 200, response.text
        assert response.json()["operation"]["sandbox_id"] == receipt["sandbox_id"]
        assert response.json()["workspace_released"] and response.json()["reconciliation"]["outcome"] == "unknown"
        (live["directory"] / "worker-killed.json").write_text(json.dumps({"actual_process_killed": True,
            "new_owner_create_replays": 0, "missing_id_recovered": True, "slot_and_lease_released": True,
            "command_outcome": "unknown"}), encoding="utf-8")


def service_container(live):
    response = subprocess.run([*WSL, "docker", "inspect", live["service_container"]],
                              capture_output=True, check=True, timeout=20)
    obj = json.loads(response.stdout)[0]
    assert obj["Config"]["Labels"].get(live["label"]) == live["nonce"]
    return obj


def change_service(live, action):
    obj = service_container(live)
    subprocess.run([*WSL, "docker", action, obj["Id"]], capture_output=True, check=True, timeout=25)


def last_creation(live):
    report = json.loads((live["directory"] / "cancel_after_create.json").read_text(encoding="utf-8"))
    with live["factory"]() as db:
        operation = db.get(ToolOperation, report["operation_id"])
        return CreationRequest(run_id=operation.run_id, operation_id=operation.id,
            operation_fingerprint=operation.binding_fingerprint,
            snapshot_path=db.get(WorkspaceSnapshot, operation.snapshot_id).workdir,
            timeout_seconds=operation.parameters["timeout_seconds"])


async def await_status(remote, request):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            return await remote.status(request)
        except SandboxResultUnknown:
            await asyncio.sleep(0.25)
    pytest.fail("Isolated service did not return its durable creation after restart")


@pytest.mark.asyncio
async def test_real_service_checkpoint_and_offline_restore_preserve_replay_barriers(live):
    request = last_creation(live)
    remote = CreationServiceClient(live["settings"], creation_service_binding(live["settings"]))
    before = await remote.status(request)
    root = live["linux_root"]
    checkpoint_path = root + "/paired-checkpoint"

    def command(action, *arguments):
        obj = service_container(live)
        assert obj["State"]["Status"] == "running"
        response = subprocess.run([*WSL, "docker", "exec", obj["Id"], "python", "-m",
            "omniharness.sandbox_creation_service", action, "--directory", checkpoint_path, *arguments],
            capture_output=True, timeout=30)
        assert response.returncode == 0, "Isolated checkpoint CLI refused execution"
        return json.loads(response.stdout)

    captured = await asyncio.to_thread(command, "checkpoint")
    verified = await asyncio.to_thread(command, "verify-checkpoint")
    assert verified == captured and captured["history"]["states"]["CREATED"] >= 4
    rehearsed = await asyncio.to_thread(command, "rehearse-checkpoint", "--target", root + "/offline-rehearsal")
    assert rehearsed["duplicate_claims_refused"] == captured["history"]["records"]
    assert rehearsed["external_requests_sent"] == rehearsed["slots_released"] == 0
    retained = await asyncio.to_thread(command, "verify-retained")
    assert retained["history_retained"] and not retained["live_stores_restored"]
    await asyncio.to_thread(change_service, live, "restart")
    assert await await_status(remote, request) == before
    with pytest.raises(SandboxResultUnknown):
        await remote.create(request.model_copy(update={"execd_token": SecretStr("x" * 32)}))
    (live["directory"] / "checkpoint-rehearsal.json").write_text(json.dumps({
        "captured_history": captured["history"], "offline_rehearsal": rehearsed,
        "retained_history": retained, "receipt_survives_restart": True,
        "duplicate_create_refused_after_rehearsal": True}), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["primary", "witness"])
async def test_service_restart_rejects_a_single_sided_store_restore(live, side):
    request = last_creation(live)
    remote = CreationServiceClient(live["settings"], creation_service_binding(live["settings"]))
    before = await remote.status(request)
    await asyncio.to_thread(change_service, live, "restart")
    assert await await_status(remote, request) == before
    await asyncio.to_thread(change_service, live, "stop")
    source = (live["linux_root"] + "/primary/creation.sqlite" if side == "primary"
              else live["witness_root"] + "/witness.sqlite")
    program = '''import os,pathlib,sqlite3,sys
p=pathlib.Path(sys.argv[1]); backup=p.with_name('complete-before-restore.sqlite')
assert p.is_file() and not p.is_symlink() and not backup.exists()
fd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); os.close(fd)
with sqlite3.connect(p) as src, sqlite3.connect(backup) as dst: src.backup(dst)
with sqlite3.connect(p) as db: db.execute('DELETE FROM sandbox_creation_journal'); db.commit()
'''
    subprocess.run([*WSL, "python3", "-c", program, source], capture_output=True, check=True, timeout=20)
    await asyncio.to_thread(change_service, live, "start")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        obj = await asyncio.to_thread(service_container, live)
        if obj["State"]["Status"] == "exited":
            break
        await asyncio.sleep(0.25)
    assert obj["State"]["Status"] == "exited" and obj["State"]["ExitCode"] != 0
    restore = '''import pathlib,sqlite3,sys
p=pathlib.Path(sys.argv[1]); backup=p.with_name('complete-before-restore.sqlite')
assert p.is_file() and backup.is_file() and not p.is_symlink() and not backup.is_symlink()
with sqlite3.connect(backup) as src, sqlite3.connect(p) as dst: src.backup(dst)
'''
    subprocess.run([*WSL, "python3", "-c", restore, source], capture_output=True, check=True, timeout=20)
    await asyncio.to_thread(change_service, live, "start")
    assert await await_status(remote, request) == before
    (live["directory"] / ("rollback-" + side + ".json")).write_text(json.dumps({
        "side": side, "ordinary_restart_retains_receipt": True, "inconsistent_store_refuses_startup": True,
        "verified_full_store_restore_recovers": True}), encoding="utf-8")


@pytest.mark.asyncio
async def test_real_missing_marker_cannot_be_used_as_reclamation_proof(live):
    import hashlib
    request = last_creation(live)
    name = "omni-create-fence-" + hashlib.sha256(
        (live["instance_id"] + "\0" + request.operation_id).encode()).hexdigest()
    result = subprocess.run([*WSL, "docker", "inspect", name], capture_output=True, check=True, timeout=20)
    obj = json.loads(result.stdout)[0]
    assert obj["Config"]["Labels"].get("omni_fence_instance") == live["instance_id"]
    assert obj["Config"]["Labels"].get("omni_fence_operation") == request.operation_id
    assert obj["Image"] == live["tool_image"] and obj["State"]["Status"] == "created"
    subprocess.run([*WSL, "docker", "rm", obj["Id"]], capture_output=True, check=True, timeout=20)
    remote = CreationServiceClient(live["settings"], creation_service_binding(live["settings"]))
    # Removing the engine marker cannot erase the independent creation receipt.
    assert (await remote.status(request))["state"] == "CREATED"
    with pytest.raises(SandboxResultUnknown):
        await remote.reclaim(request)
    with pytest.raises(SandboxResultUnknown):
        await remote.create(request.model_copy(update={"execd_token": SecretStr("x" * 32)}))
    (live["directory"] / "marker-missing.json").write_text(json.dumps({
        "durable_receipt_retained": True, "reclamation_refused": True, "repeated_create_refused": True}), encoding="utf-8")
