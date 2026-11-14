from datetime import datetime, timezone

from conftest import create_chat

from omniharness.models import RunEvent
from omniharness.services import event_output


def test_api_timestamps_have_explicit_utc_offset(client):
    model, conversation, queued = create_chat(client)
    values = [model["created_at"], conversation["created_at"], conversation["updated_at"]]
    values.append(client.get(f"/api/v1/runs/{queued['run_id']}").json()["created_at"])
    values.extend(m["created_at"] for m in client.get(f"/api/v1/conversations/{conversation['id']}/messages").json())
    assert all(datetime.fromisoformat(value).utcoffset() == timezone.utc.utcoffset(None) for value in values)


def test_sqlite_event_serialization_does_not_use_local_timezone():
    event = RunEvent(id="event", run_id="run", sequence_number=1, event_type="run.created",
                     payload={}, created_at=datetime(2026, 11, 5, 4, 19, 47))
    assert event_output(event)["created_at"] == "2026-11-05T04:19:47+00:00"
