import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_m4_api import workspace  # noqa: F401 - reuse the existing workspace API fixture
from test_m4_collaboration import graph_v4, prepare_v4

from omniharness.artifacts import ArtifactStore
from omniharness.evidence_view import _project_review
from omniharness.handoff_contract import build_handoff_v8, build_handoff_v9
from omniharness.models import (
    ArtifactVersion,
    OperationRecord,
    Run,
    RunBudgetReservation,
    ToolOperation,
    WorkspaceSnapshot,
)
from omniharness.review_contract import CandidateReferenceV9
from omniharness.review_evidence import (
    _bound_review_input,
    _review_artifact,
    _v9_tool_binding,
    _validate_review_reference,
    build_review_evidence,
    build_review_evidence_v9,
    review_input_on_reserved,
    v9_request_mac,
    v9_response_mac,
    v9_snapshot_mac,
)
from omniharness.worker import WorkerLock


@pytest.mark.asyncio
async def test_v9_command_dimensions_and_reference_scope(environment, tmp_path, monkeypatch):
    worker, run_id, workspace, broker, sandbox, _ = prepare_v4(environment, tmp_path)  # noqa: F811
    with WorkerLock(worker.engine, worker.settings) as owner:
        worker.lock = owner
        graph, scheduler = graph_v4(worker, run_id, workspace, broker, sandbox, InMemorySaver(), strategy="single")
        try:
            await graph.ainvoke({"messages": [("user", "fix and test")]},
                                {"configurable": {"thread_id": run_id}, "recursion_limit": 100})
        finally:
            await scheduler.aclose()
    store = ArtifactStore(worker.settings.data_dir / "artifacts")
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        old = build_review_evidence(db, run, store)
        old_command = next(x for x in old["operations"] if x["tool"] == "run_command")
        assert old_command["evidence_status"] == "UNVERIFIED"
        run.workflow_version = "stage-agent-v9"
        # The captured fixture was executed as v4; rebind the isolated ledger
        # versions to model the matching v9 records used by this unit test.
        for operation in db.scalars(select(ToolOperation).where(ToolOperation.run_id == run_id)):
            ledger_row = db.get(OperationRecord, operation.operation_record_id)
            ledger_row.workflow_version = "stage-agent-v9"
            new_binding = _v9_tool_binding(run, operation, ledger_row.result["purpose"])
            reservation = next(r for r in db.scalars(select(RunBudgetReservation).where(
                RunBudgetReservation.run_id == run_id, RunBudgetReservation.kind == "tool_calls"))
                if r.binding_fingerprint == ledger_row.result["binding_fingerprint"])
            reservation.binding_fingerprint = new_binding
            snap = db.get(WorkspaceSnapshot, operation.snapshot_id) if operation.snapshot_id else None
            request_mac = v9_request_mac(worker.settings, run, operation)
            ledger_row.result = {**ledger_row.result, "binding_fingerprint": new_binding,
                                 "v9_request_mac": request_mac,
                                 "v9_snapshot_mac": v9_snapshot_mac(worker.settings, run, operation, snap),
                                 "v9_response_mac": v9_response_mac(worker.settings, run, operation.id,
                                                                     request_mac, operation.result)}
        packet = build_review_evidence_v9(db, run, store, settings=worker.settings)
        command = next(x for x in packet["operations"] if x["tool"] == "run_command")
        assert command["dimensions"] == {"execution": "RECORDED", "exit_code": "RECORDED",
            "initial_snapshot": "MATCHED", "runtime_source": "UNVERIFIED", "claim_support": "UNVERIFIED"}
        assert command["exit_code"] == 0 and packet["assessment"] == "UNVERIFIED"
        log = next(a for a in command["artifacts"] if a["kind"] == "command_log")
        assert log["log_integrity"] == "VERIFIED"
        oversized = store.put(db, run_id=run_id, workspace_id=run.workspace_id,
                              operation_id=command["operation_id"], kind="command_log", name="large.log",
                              content="x", metadata={"snapshot_id": command["snapshot_id"], "complete": True})
        large_row = db.get(ArtifactVersion, oversized["id"])
        # Actual file growth is rejected before hashing even if DB size stays tiny.
        (store.root / large_row.id).write_bytes(b"x" * (1024 * 1024 + 1))
        read_sizes = []
        real_read = os.read
        def counted_read(fd, size):
            data = real_read(fd, size)
            read_sizes.append(len(data))
            return data
        with monkeypatch.context() as patcher:
            patcher.setattr(os, "read", counted_read)
            limited = build_review_evidence_v9(db, run, store, settings=worker.settings)
        assert sum(read_sizes) <= 8 * 1024 * 1024
        large_item = next(a for x in limited["operations"] for a in x["artifacts"]
                          if a["artifact_id"] == oversized["id"])
        assert large_item["log_integrity"] == "UNVERIFIED" and "excerpt" not in large_item
        db.delete(large_row)
        db.flush()
        raced = store.put(db, run_id=run_id, workspace_id=run.workspace_id,
                          operation_id=command["operation_id"], kind="command_log", name="raced.log",
                          content="q" * 137, metadata={"snapshot_id": command["snapshot_id"], "complete": True})
        race_path = store.root / raced["id"]
        race_triggered = []
        def growing_read(fd, size):
            data = real_read(fd, size)
            if os.fstat(fd).st_size == 137 and not race_triggered:
                with race_path.open("ab") as output:
                    output.write(b"growth")
                race_triggered.append(True)
            return data
        with monkeypatch.context() as patcher:
            patcher.setattr(os, "read", growing_read)
            raced_packet = build_review_evidence_v9(db, run, store, settings=worker.settings)
        assert race_triggered
        raced_item = next(a for x in raced_packet["operations"] for a in x["artifacts"]
                          if a["artifact_id"] == raced["id"])
        assert raced_item["log_integrity"] == "UNVERIFIED" and "excerpt" not in raced_item
        db.delete(db.get(ArtifactVersion, raced["id"]))
        db.flush()
        fake = store.put(db, run_id=run_id, workspace_id=run.workspace_id,
                         operation_id=command["operation_id"], kind="command_log", name="fake.log",
                         content="fabricated", metadata={"snapshot_id": command["snapshot_id"], "complete": True})
        packet = build_review_evidence_v9(db, run, store, settings=worker.settings)
        command = next(x for x in packet["operations"] if x["tool"] == "run_command")
        unbound = next(a for a in command["artifacts"] if a["artifact_id"] == fake["id"])
        assert unbound["log_integrity"] == "UNVERIFIED" and "excerpt" not in unbound
        identity = SimpleNamespace(invocation_id="a" * 64, binding_fingerprint="b" * 64)
        host = SimpleNamespace(run_id=run_id, worker=worker)
        message = HumanMessage(id="omni-evidence:review", content=json.dumps({"host_evidence": packet}))
        review_input_on_reserved(host, identity, [message])(db, None)
        manifest = _bound_review_input(db, run, store, identity)
        manifest_row = db.scalar(select(ArtifactVersion).where(
            ArtifactVersion.run_id == run_id, ArtifactVersion.kind == "review_input_manifest_v2"))
        manifest_path = store.root / manifest_row.id
        manifest_bytes = manifest_path.read_bytes()
        manifest_path.write_bytes(manifest_bytes + b"x" * (1024 * 1024 + 1))
        with pytest.raises(ValueError):
            _bound_review_input(db, run, store, identity)
        manifest_path.write_bytes(manifest_bytes)
        saved_review = _review_artifact(db, run, store, identity, kind="review_model_result_v2", content="review")
        review_path = store.root / saved_review["id"]
        review_path.write_bytes(b"review" + b"x" * (1024 * 1024 + 1))
        with pytest.raises(ValueError):
            _review_artifact(db, run, store, identity, kind="review_model_result_v2", content="review")
        review_path.write_bytes(b"review")
        assert _review_artifact(db, run, store, identity, kind="review_model_result_v2", content="review") == saved_review
        def verify(dimension, **extra):
            candidate = CandidateReferenceV9(operation_id=command["operation_id"], dimension=dimension, **extra)
            return _validate_review_reference(db, run, store, manifest, candidate,
                                              {"remaining": 8 * 1024 * 1024, "checked": set()}, settings=worker.settings)
        assert verify("execution")["reference_status"] == "VALIDATED"
        assert verify("exit_code")["reference_status"] == "VALIDATED"
        assert verify("log", artifact_id=log["artifact_id"], expected_sha256=log["sha256"])["reference_status"] == "VALIDATED"
        assert verify("runtime_source")["reference_status"] == "UNVERIFIED"
        assert verify("log", artifact_id=fake["id"], expected_sha256=fake["sha256"])["reference_status"] == "UNVERIFIED"
        assert build_review_evidence_v9(db, run, store, settings=worker.settings, operation_ids=["foreign-operation"])["operations"] == []
        op = db.get(ToolOperation, command["operation_id"])
        snapshot = db.get(WorkspaceSnapshot, op.snapshot_id)
        original_parameters = op.parameters
        op.parameters = {**op.parameters, "command": ["fabricated"]}
        forged = next(x for x in build_review_evidence_v9(db, run, store, settings=worker.settings)["operations"]
                      if x["operation_id"] == op.id)
        assert forged["dimensions"]["execution"] == "UNVERIFIED"
        assert "command" not in forged and forged["exit_code"] is None
        op.parameters = original_parameters
        patch = db.scalar(select(ToolOperation).where(ToolOperation.run_id == run_id,
                                                      ToolOperation.tool_name == "workspace_patch"))
        patch_ledger = db.get(OperationRecord, patch.operation_record_id)
        old_patch_result = patch_ledger.result
        patch_ledger.result = {**old_patch_result, "response": {**old_patch_result["response"], "sha256": "0" * 64}}
        assert verify("initial_snapshot")["reference_status"] == "UNVERIFIED"
        assert verify("execution")["reference_status"] == "VALIDATED"
        patch_ledger.result = old_patch_result
        original_patch_params = patch.parameters
        patch.parameters = {**patch.parameters, "path": "forged.py"}
        forged_packet = build_review_evidence_v9(db, run, store, settings=worker.settings)
        forged_patch = next(x for x in forged_packet["operations"] if x["operation_id"] == patch.id)
        assert "path" not in forged_patch and "resulting_source" not in forged_patch
        assert forged_packet["target_versions"] == {} and forged_packet["target_versions_status"] == "UNVERIFIED"
        patch.parameters = original_patch_params
        original_manifest = snapshot.manifest
        snapshot.manifest = {**original_manifest, "files": []}
        assert verify("initial_snapshot")["reference_status"] == "UNVERIFIED"
        assert verify("execution")["reference_status"] == "VALIDATED"
        assert verify("exit_code")["reference_status"] == "VALIDATED"
        forged_snapshot_packet = build_review_evidence_v9(db, run, store, settings=worker.settings)
        forged_snapshot_cmd = next(x for x in forged_snapshot_packet["operations"] if x["operation_id"] == op.id)
        assert forged_snapshot_cmd["dimensions"]["execution"] == "RECORDED"
        assert forged_snapshot_cmd["dimensions"]["exit_code"] == "RECORDED"
        assert forged_snapshot_cmd["dimensions"]["initial_snapshot"] == "UNVERIFIED"
        op.parameters = {**op.parameters, "command": ["also forged"]}
        doubly_forged = next(x for x in build_review_evidence_v9(db, run, store, settings=worker.settings)["operations"]
                             if x["operation_id"] == op.id)
        assert doubly_forged["dimensions"]["execution"] == "UNVERIFIED" and "command" not in doubly_forged
        op.parameters = original_parameters
        snapshot.manifest = original_manifest
        artifact = db.get(ArtifactVersion, log["artifact_id"])
        path = store.root / artifact.id
        original_log = path.read_bytes()
        path.write_bytes(b"tampered")
        assert verify("log", artifact_id=log["artifact_id"], expected_sha256=log["sha256"])["reference_status"] == "UNVERIFIED"
        path.write_bytes(original_log)
        ledger = db.get(OperationRecord, op.operation_record_id)
        original_op_result, original_ledger_result = op.result, ledger.result
        ledger.result = {k: v for k, v in ledger.result.items() if k != "v9_request_mac"}
        without_mac = next(x for x in build_review_evidence_v9(db, run, store, settings=worker.settings)["operations"]
                           if x["operation_id"] == op.id)
        assert without_mac["dimensions"]["execution"] == "UNVERIFIED" and "command" not in without_mac
        ledger.result = original_ledger_result
        op.result = {**op.result, "exit_code": 42}
        ledger.result = {**ledger.result, "response": op.result}
        assert verify("exit_code")["reference_status"] == "UNVERIFIED"
        assert verify("execution")["reference_status"] == "VALIDATED"
        forged_result = next(x for x in build_review_evidence_v9(db, run, store, settings=worker.settings)["operations"]
                             if x["operation_id"] == op.id)
        assert forged_result["exit_code"] is None and forged_result["dimensions"]["execution"] == "RECORDED"
        assert all("excerpt" not in artifact for artifact in forged_result["artifacts"])
        op.result = {**original_op_result, "artifacts": [*original_op_result["artifacts"], fake]}
        ledger.result = {**original_ledger_result, "response": op.result}
        fabricated_log_packet = build_review_evidence_v9(db, run, store, settings=worker.settings)
        fabricated_op = next(x for x in fabricated_log_packet["operations"] if x["operation_id"] == op.id)
        fabricated_ref = next(a for a in fabricated_op["artifacts"] if a["artifact_id"] == fake["id"])
        assert fabricated_ref["log_integrity"] == "UNVERIFIED" and "excerpt" not in fabricated_ref
        op.result, ledger.result = original_op_result, original_ledger_result
        ledger.result = {**ledger.result, "response": {**ledger.result["response"], "exit_code": 42}}
        changed = build_review_evidence_v9(db, run, store, settings=worker.settings)
        result = next(x for x in changed["operations"] if x["tool"] == "run_command")
        assert result["dimensions"]["execution"] == "RECORDED"
        assert result["dimensions"]["exit_code"] == "UNVERIFIED" and result["exit_code"] is None
        assert verify("execution")["reference_status"] == "VALIDATED"


def test_v9_handoff_domain_and_projection():
    args = dict(run_id="run", workspace_id="workspace", source_task_id="s", target_task_id="t",
                goal_message_id="goal", task_fingerprint="a" * 64, artifact_id="artifact",
                artifact_sha256=hashlib.sha256(b"report").hexdigest(), report_text="report", operations=[])
    v8 = build_handoff_v8(workflow_version="stage-agent-v8", **args)
    v9 = build_handoff_v9(workflow_version="stage-agent-v9", **args)
    assert v8.handoff_id != v9.handoff_id and v8.binding_fingerprint != v9.binding_fingerprint
    with pytest.raises(ValueError):
        build_handoff_v9(workflow_version="stage-agent-v8", **args)
    with pytest.raises(ValueError):
        CandidateReferenceV9(operation_id="id", dimension="log")
    assert _project_review({"schema_version": 1}, "stage-agent-v9") is None
    assert _project_review({"schema_version": 2, "workflow_version": "stage-agent-v9"}, "stage-agent-v8") is None



def test_pending_binding_is_hidden_from_api_and_event_payload(environment, workspace):  # noqa: F811
    from test_m4_api import tool_run

    from omniharness.m4_services import tool_output
    from omniharness.models import RunEvent
    _, _, factory, client = environment
    run_id, operation_id, approval_id = tool_run(environment, workspace)
    canary = "c" * 64
    with factory.begin() as db:
        op = db.get(ToolOperation, operation_id)
        op.result = {"v9_request_mac": canary, "v9_snapshot_mac": canary}
        assert tool_output(op)["result"] is None
    for path in (f"/api/v1/runs/{run_id}/tools", f"/api/v1/runs/{run_id}/approvals"):
        response = client.get(path)
        assert response.status_code == 200
        assert canary not in response.text and "v9_request_mac" not in response.text and "v9_snapshot_mac" not in response.text
    response = client.post(f"/api/v1/approvals/{approval_id}/decision",
                           json={"decision": "approve", "fingerprint": "a" * 64, "client_request_id": "v9-canary"})
    assert response.status_code == 202 and canary not in response.text
    with factory.begin() as db:
        db.get(Run, run_id).status = "completed"
    stream = client.get(f"/api/v1/runs/{run_id}/events")
    assert stream.status_code == 200 and canary not in stream.text and "v9_request_mac" not in stream.text and "v9_snapshot_mac" not in stream.text
    with factory() as db:
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
        assert all(canary not in json.dumps(event.payload) and "v9_request_mac" not in json.dumps(event.payload)
                   for event in events)



@pytest.mark.asyncio
async def test_v9_handoff_rejects_actual_task_result_growth(environment, tmp_path, monkeypatch):
    from omniharness.m3_agents import AgentBoundaryError
    from omniharness.review_evidence import prepare_dependency_handoffs
    worker, run_id, workspace, broker, sandbox, _ = prepare_v4(environment, tmp_path)  # noqa: F811
    store = ArtifactStore(worker.settings.data_dir / "artifacts")
    report = "verified prior task result"
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        run.workflow_version = "stage-agent-v9"
        artifact = store.put(db, run_id=run_id, workspace_id=run.workspace_id, kind="task_result",
                             name="task-t1.md", content=report,
                             metadata={"authority": "model_claim", "logical_task_id": "t1",
                                       "workflow_version": "stage-agent-v9"})
    worker.lock = object()
    class Host:
        serial = asyncio.Lock()
        def __init__(self):
            self.worker = worker
            self.run_id = run_id
        def _run(self, db, role, **kwargs):
            assert role == "execute"
            return db.get(Run, run_id), None
    worker.transaction = worker.sessions.begin
    tasks = [{"id": "t1", "step_id": "s1", "depends_on": []},
             {"id": "t2", "step_id": "s2", "depends_on": ["t1"]}]
    results = [{"task_id": "t1", "step_id": "s1", "content": report,
                "result_artifact": {"artifact_id": artifact["id"], "sha256": artifact["sha256"],
                                    "size_bytes": artifact["size_bytes"]}}]
    async def handoff():
        return await prepare_dependency_handoffs(Host(), task=tasks[1], tasks=tasks, task_results=results,
                                                 task_goal="target", constraints=[], messages=[])
    assert len(await handoff()) == 1
    path = store.root / artifact["id"]
    path.write_bytes(report.encode() + b"x" * (1024 * 1024 + 1))
    with pytest.raises(AgentBoundaryError, match="cannot be verified"):
        await handoff()
    path.write_bytes(report.encode())
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        other = store.put(db, run_id=run_id, workspace_id=run.workspace_id, kind="task_result",
                          name="task-t2.md", content=report,
                          metadata={"authority": "model_claim", "logical_task_id": "t2",
                                    "workflow_version": "stage-agent-v9"})
    tasks = [{"id": "t1", "step_id": "s1", "depends_on": []},
             {"id": "t2", "step_id": "s2", "depends_on": []},
             {"id": "t3", "step_id": "s3", "depends_on": ["t1", "t2"]}]
    results.append({"task_id": "t2", "step_id": "s2", "content": report,
                    "result_artifact": {"artifact_id": other["id"], "sha256": other["sha256"],
                                        "size_bytes": other["size_bytes"]}})
    import omniharness.review_evidence as evidence_module
    original_reader = evidence_module._BoundedEvidenceStore
    class SmallBudgetReader(original_reader):
        def __init__(self, delegate):
            super().__init__(delegate)
            self.remaining = len(report.encode()) + 1
    monkeypatch.setattr(evidence_module, "_BoundedEvidenceStore", SmallBudgetReader)
    with pytest.raises(AgentBoundaryError, match="cannot be verified"):
        await prepare_dependency_handoffs(Host(), task=tasks[2], tasks=tasks, task_results=results,
                                          task_goal="target", constraints=[], messages=[])
