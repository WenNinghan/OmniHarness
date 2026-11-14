from uuid import uuid4

from conftest import create_chat

from omniharness.history_scope import select_history
from omniharness.models import Clarification, Conversation, Message, Run, Workspace


def scope(workspace=None, additional=()):
    return {"schema_version": 1, "kind": "workspace" if workspace else "unscoped",
            "workspace_id": workspace, "additional_run_ids": list(additional)}


def _add_run(db, conversation_id, model_id, ordinal, workspace, *, status="completed", conversation=None):
    identity = str(uuid4())
    run = Run(id=identity, conversation_id=conversation or conversation_id, model_config_id=model_id,
              workspace_id=workspace, model_label="test", ordinal=ordinal,
              client_request_id=str(uuid4()), status=status, mode="mock",
              config_snapshot={"history_scope": scope(workspace)})
    db.add(run)
    db.flush()
    db.add_all([Message(run_id=identity, conversation_id=run.conversation_id, role="user", content=f"目标-{identity}"),
                Message(run_id=identity, conversation_id=run.conversation_id, role="assistant", content=f"成果-{identity}", status="completed")])
    return run


def _question(db, run_id, label, *, status="answered"):
    row = Clarification(id=str(uuid4()), run_id=run_id, request_key=str(uuid4()), stage="execute",
                        question="问题-" + label, options=[], status=status,
                        answer="答复-" + label if status == "answered" else None)
    db.add(row)


def test_answered_questions_follow_workspace_scope_and_explicit_selection(environment):
    _, _, factory, client = environment
    model, conversation, initial = create_chat(client)
    with factory.begin() as db:
        db.add_all([Workspace(id="A", name="A", host_path="A", enabled=True, probe={}),
                    Workspace(id="B", name="B", host_path="B", enabled=True, probe={})])
        db.flush()
        initial_run = db.get(Run, initial["run_id"])
        initial_run.status = "completed"
        initial_run.workspace_id = "A"
        initial_run.config_snapshot = {"history_scope": scope("A")}
        _question(db, initial_run.id, "same")
        _question(db, initial_run.id, "pending", status="pending")
        other = _add_run(db, conversation["id"], model["id"], 2, "B")
        _question(db, other.id, "other")
        current = _add_run(db, conversation["id"], model["id"], 3, "A", status="queued")
        _question(db, current.id, "current")
        current_id, other_id = current.id, other.id
    with factory() as db:
        selected, _ = select_history(db, db.get(Run, current_id))
        joined = "\n".join(item["content"] for item in selected)
        assert "问题-same" in joined and "答复-same" in joined
        assert all(label not in joined for label in ("问题-pending", "问题-other", "问题-current"))
        qa = [item["role"] for item in selected if item["id"].startswith("clarify-")]
        assert qa == ["assistant", "user"]
    with factory.begin() as db:
        current = db.get(Run, current_id)
        current.config_snapshot = {"history_scope": scope("A", [other_id])}
    with factory() as db:
        selected, _ = select_history(db, db.get(Run, current_id))
        joined = "\n".join(item["content"] for item in selected)
        assert "问题-other" in joined and "答复-other" in joined


def test_no_cross_conversation_or_legacy_semantic_change(environment):
    _, _, factory, client = environment
    model, conversation, initial = create_chat(client)
    with factory.begin() as db:
        db.get(Run, initial["run_id"]).status = "completed"
        _question(db, initial["run_id"], "own")
        foreign = Conversation(id=str(uuid4()), title="other", next_ordinal=2)
        db.add(foreign)
        db.flush()
        origin = _add_run(db, conversation["id"], model["id"], 1, None, conversation=foreign.id)
        _question(db, origin.id, "foreign")
        current = _add_run(db, conversation["id"], model["id"], 2, None, status="queued")
        current_id = current.id
    with factory() as db:
        history, _ = select_history(db, db.get(Run, current_id))
        joined = "\n".join(item["content"] for item in history)
        assert "问题-own" in joined and "问题-foreign" not in joined
    with factory.begin() as db:
        db.get(Run, current_id).config_snapshot = {}
    with factory() as db:
        history, _ = select_history(db, db.get(Run, current_id))
        assert all(not item["id"].startswith("clarify-") for item in history)
