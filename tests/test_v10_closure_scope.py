"""Scope checks are necessary diagnostics, not a complete closure proof."""
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from conftest import create_chat
from sqlalchemy import select

from omniharness.action_contract import validate_explicit_actions
from omniharness.models import Message, Run, Workspace, WorkspaceLease, WorkspacePolicy
from omniharness.v10_closure import screen_zero_tool_correction_scope
from omniharness.v10_owner_auth import make_run_origin, new_owner_identity
from omniharness.v10_owner_runtime import V10OwnerListener
from omniharness.worker import Worker
from omniharness.workspace_broker import create_broker_app
from omniharness.workspace_io import DictWorkspaceRegistry, WorkspaceGrant, WorkspaceIO, native_path_identity


async def test_scope_rechecks_owner_actions_and_workspace_policy(environment, tmp_path):
    settings, engine, factory, client = environment
    settings.enable_tools = True
    _, _, created = create_chat(client)
    run_id = created["run_id"]
    worker = Worker(settings, engine)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    owner = new_owner_identity(42, started, settings.instance_id)
    listener = V10OwnerListener(worker, None, owner, 43)
    worker._v10_owner_listener = listener
    root = tmp_path / "project"
    root.mkdir()
    if os.name == "nt":
        physical = native_path_identity(str(root))
    else:
        physical = {"device": root.stat().st_dev, "file_id": root.stat().st_ino, "filesystem": "posix"}
    ticket = SimpleNamespace(thread_id=run_id, owner_pid=42, owner_started=started, epoch=1)
    sandbox = {"endpoint": settings.sandbox_endpoint, "image": settings.sandbox_image,
        "instance_id": settings.instance_id, "snapshot_root": settings.sandbox_snapshot_root,
        "max_command_seconds": settings.sandbox_max_command_seconds}
    # The UI submits executable parameters without a model-generated step ID.
    requirements = [{"kind": "workspace_file_write", "path": "out.txt"}]
    with factory.begin() as db:
        workspace = Workspace(name="fixture", host_path=str(root), physical_key="fixture",
                              policy_version=1, enabled=True, probe={"identity": physical})
        db.add(workspace)
        db.flush()
        grant = {"workspace_id": workspace.id, "instance_id": settings.instance_id, "root": str(root), "version": 1,
                 "read_allow": ["."], "write_allow": ["."], "excludes": [], "enabled": True,
                 "expected_identity": physical}
        policy = WorkspacePolicy(workspace_id=workspace.id, version=1, grant=grant,
                                 tool_policy={"tools": ["workspace_patch"]})
        db.add(policy)
        run = db.get(Run, run_id)
        run.workspace_id, run.workflow_version, run.status = workspace.id, "stage-agent-v10", "running"
        db.add(WorkspaceLease(workspace_id=workspace.id, run_id=run.id))
        explicit = validate_explicit_actions(requirements, run_id=run.id, workspace_id=workspace.id)
        run.config_snapshot = {**run.config_snapshot, "workspace": {"id": workspace.id, "policy_version": 1,
            "grant": policy.grant, "tool_policy": policy.tool_policy, "probe_identity": physical,
            "sandbox": sandbox, "broker_endpoint": settings.workspace_broker_url}, "action_contract": {
                "version": 1, "request": {"requirements": requirements}, "explicit": explicit,
                "auto_correction_enabled": True}}
        run.outcome = {"v10_actions": {**explicit, "actions": [{**explicit["actions"][0], "task_id": "t1"}]}}
        user = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
        origin = make_run_origin(settings, owner, run, user)
        run.config_snapshot = {**run.config_snapshot, "v10_owner_origin": origin}
        listener.accepted[run_id] = origin
    registry = DictWorkspaceRegistry([WorkspaceGrant(**grant)])
    io = WorkspaceIO(registry, tmp_path / "broker-storage", protected_roots=())
    app = create_broker_app(io, instance_id=settings.instance_id, service_key="x" * 64)

    class Broker:
        async def request(self, identity, version, action):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker",
                    headers={"Authorization": "Bearer " + "x" * 64, "X-Omni-Instance": settings.instance_id}) as client:
                response = await client.post(f"/workspaces/{identity}/{action}", json={"policy_version": version})
                response.raise_for_status()
                return response.json()
    worker.workspace_broker = Broker()
    task = {"id": "t1", "step_id": "s1", "write_paths": ["out.txt"]}

    class Graph:
        async def aget_state(self, _config):
            return SimpleNamespace(config={"configurable": {"checkpoint_id": "cp"}}, next=(), interrupts=(),
                                   values={"dispatch": {"tasks": [task]}})

    async def check():
        with factory.begin() as db:
            return await screen_zero_tool_correction_scope(db, worker, ticket, Graph(), "cp")

    assert (await check())[0]
    moved = tmp_path / "moved-project"
    root.rename(moved)
    root.mkdir()
    assert await check() == (False, "workspace_probe_unavailable")
    root.rmdir()
    moved.rename(root)
    task["write_paths"] = ["different.txt"]
    assert await check() == (False, "action_not_granted")
    task["write_paths"] = ["out.txt"]
    with factory.begin() as db:
        run = db.get(Run, run_id)
        policy = db.get(WorkspacePolicy, (workspace.id, 1))
        new_grant = {**policy.grant, "write_allow": []}
        policy.grant = new_grant
        run.config_snapshot = {**run.config_snapshot, "workspace": {**run.config_snapshot["workspace"], "grant": new_grant}}
        user = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
        changed_origin = make_run_origin(settings, owner, run, user)
        run.config_snapshot = {**run.config_snapshot, "v10_owner_origin": changed_origin}
        listener.accepted[run_id] = changed_origin
    assert await check() == (False, "action_not_granted")
    with factory.begin() as db:
        run = db.get(Run, run_id)
        policy = db.get(WorkspacePolicy, (workspace.id, 1))
        new_grant = {**policy.grant, "write_allow": ["."], "excludes": ["out.txt"]}
        policy.grant = new_grant
        run.config_snapshot = {**run.config_snapshot, "workspace": {**run.config_snapshot["workspace"], "grant": new_grant}}
        user = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
        changed_origin = make_run_origin(settings, owner, run, user)
        run.config_snapshot = {**run.config_snapshot, "v10_owner_origin": changed_origin}
        listener.accepted[run_id] = changed_origin
    assert await check() == (False, "action_not_granted")
    with factory.begin() as db:
        run = db.get(Run, run_id)
        policy = db.get(WorkspacePolicy, (workspace.id, 1))
        policy.grant = grant
        run.config_snapshot = {**run.config_snapshot, "workspace": {**run.config_snapshot["workspace"], "grant": grant}}
        user = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
        changed_origin = make_run_origin(settings, owner, run, user)
        run.config_snapshot = {**run.config_snapshot, "v10_owner_origin": changed_origin}
        listener.accepted[run_id] = changed_origin
    with factory.begin() as db:
        run = db.get(Run, run_id)
        run.outcome = {**run.outcome, "v10_action_outcome": {"status": "ACTIONS_RECORDED", "actions": []}}
    assert await check() == (False, "action_already_recorded")
    with factory.begin() as db:
        run = db.get(Run, run_id)
        run.outcome = {key: value for key, value in run.outcome.items() if key != "v10_action_outcome"}
    ordered = [
        {"kind": "workspace_file_write", "path": "out.txt", "client_id": "write", "depends_on": []},
        {"kind": "sandbox_command", "argv": ["pytest", "-q"], "cwd": ".", "client_id": "test",
         "depends_on": ["write"]},
    ]
    with factory.begin() as db:
        run = db.get(Run, run_id)
        policy = db.get(WorkspacePolicy, (workspace.id, 1))
        policy.tool_policy = {"tools": ["workspace_patch", "run_command"]}
        explicit = validate_explicit_actions(ordered, run_id=run_id, workspace_id=workspace.id,
                                             require_dependencies=True)
        assert explicit["version"] == 2
        contract = {"version": 1, "request": {"requirements": ordered}, "explicit": explicit,
                    "auto_correction_enabled": True}
        run.config_snapshot = {**run.config_snapshot,
            "workspace": {**run.config_snapshot["workspace"], "tool_policy": policy.tool_policy},
            "action_contract": contract}
        saved = {**explicit, "actions": [{**item, "task_id": "t1"} for item in explicit["actions"]]}
        run.outcome = {**run.outcome, "v10_actions": saved}
        user = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "user"))
        changed_origin = make_run_origin(settings, owner, run, user)
        run.config_snapshot = {**run.config_snapshot, "v10_owner_origin": changed_origin}
        listener.accepted[run_id] = changed_origin
    assert (await check())[0]
    with factory.begin() as db:
        run = db.get(Run, run_id)
        run.outcome = {**run.outcome, "v10_actions": {**saved, "dependency_graph": {}}}
    assert await check() == (False, "action_task_binding_changed")
    with factory.begin() as db:
        run = db.get(Run, run_id)
        run.outcome = {**run.outcome, "v10_actions": saved}
    with factory.begin() as db:
        policy = db.get(WorkspacePolicy, (workspace.id, 1))
        policy.tool_policy = {"tools": []}
    assert await check() == (False, "workspace_policy_changed")
    listener.accepted.clear()
    assert await check() == (False, "owner_origin_unavailable")
