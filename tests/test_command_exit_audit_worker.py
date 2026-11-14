"""The final answer, durable metadata and completion event stay consistent."""
from conftest import create_chat
from langchain_core.messages import AIMessage
from sqlalchemy import select
from test_command_exit_evidence import setup_command

from omniharness.m3_worker import _complete
from omniharness.models import Message, Run, RunEvent, Workspace
from omniharness.worker import Worker

ARTIFACT = "61b78660-6971-4d7f-99d4-c3d7c975d0b3"
ANSWER = f"| 检查 | 退出码 | 结果 |\n|---|---|---|\n| 扩展检查 | 0 | 工件 `{ARTIFACT}`，该次 exit 1 |"


def prepare(environment, *, already_streamed):
    settings, engine, factory, client = environment
    _, _, created = create_chat(client)
    run_id = created["run_id"]
    with factory.begin() as db:
        workspace = Workspace(name="audit-fixture", host_path="G:/isolated-audit", enabled=True, probe={})
        db.add(workspace)
        db.flush()
        run = db.get(Run, run_id)
        run.status, run.workspace_id = "running", workspace.id
        message = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
        if already_streamed:
            message.content = ANSWER
            message.output_metadata = {"output_status": "streaming", "stage": "execute"}
    return Worker(settings, engine), factory, run_id


def test_conflict_publishes_exact_answer_and_warning_in_history_and_terminal_event(environment, monkeypatch):
    worker, factory, run_id = prepare(environment, already_streamed=True)
    monkeypatch.setattr("omniharness.command_exit_evidence.collect_command_exit_evidence",
                        lambda db, identity: ({ARTIFACT: ("a" * 64, 1)}, False))
    _complete(worker, run_id, ANSWER, started=None)
    with factory() as db:
        message = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
        event = db.scalar(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "run.completed"))
        assert message.content == ANSWER and message.status == "completed"
        assert message.output_metadata["output_status"] == "streaming"  # Existing precise state is preserved.
        audit = message.output_metadata["command_result_audit"]
        assert audit["status"] == "conflict" and audit["conflicts"][0]["actual_exit_code"] == 1
        assert audit["conflicts"][0]["claimed_exit_code"] == 0
        assert event.payload["output_metadata"]["command_result_audit"] == audit
        assert db.query(RunEvent).filter_by(run_id=run_id, event_type="message.delta").count() == 0


def test_output_is_unchanged_without_a_proven_conflict_and_truncation_is_explicit(environment, monkeypatch):
    worker, factory, run_id = prepare(environment, already_streamed=False)
    monkeypatch.setattr("omniharness.command_exit_evidence.collect_command_exit_evidence",
                        lambda db, identity: ({ARTIFACT: ("a" * 64, 0)}, True))
    _complete(worker, run_id, ANSWER, started=None, source_message=AIMessage(content=ANSWER))
    with factory() as db:
        message = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
        assert message.content == ANSWER
        assert message.output_metadata["command_result_audit"] == {
            "status": "incomplete", "conflicts": [], "truncated": True}
        event = db.scalar(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "run.completed"))
        assert event.payload["output_metadata"] == message.output_metadata


def test_real_database_binding_flows_through_worker_into_terminal_message(environment):
    worker, factory, run_id = prepare(environment, already_streamed=False)
    _, operation_id, _, artifact_id = setup_command(factory, run_id=run_id, operation_id="b" * 64, exit_code=1)
    answer = f"| 检查 | 退出码 | 结果 |\n|---|---|---|\n| 测试 | 0 | 日志 `{artifact_id}` |"
    _complete(worker, run_id, answer, started=None, source_message=AIMessage(content=answer))
    with factory() as db:
        message = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
        event = db.scalar(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "run.completed"))
        assert message.content == answer
        audit = message.output_metadata["command_result_audit"]
        assert audit["conflicts"] == [{"artifact_id": artifact_id, "operation_id": operation_id,
            "claimed_exit_code": 0, "actual_exit_code": 1, "line": 3}]
        assert event.payload["output_metadata"]["command_result_audit"] == audit
