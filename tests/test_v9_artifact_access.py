"""Review v2 artifacts are visible only in their own v9 workspace run."""
from uuid import uuid4

from test_m4_collaboration import prepare_v4

from omniharness.artifacts import ArtifactStore
from omniharness.models import Run


def test_v9_review_artifact_access_is_bound_to_run_workspace_and_version(environment, tmp_path):
    worker, run_id, _, _, _, _ = prepare_v4(environment, tmp_path)
    with worker.sessions.begin() as db:
        run = db.get(Run, run_id)
        run.workflow_version = 'stage-agent-v8'
        artifact = ArtifactStore(worker.settings.data_dir / 'artifacts').put(
            db, run_id=run_id, workspace_id=run.workspace_id, kind='review_reference_report_v2',
            name='review.json', content='{"schema_version":2}')
    client = environment[3]
    path = f'/api/v1/runs/{run_id}/artifacts/{artifact["id"]}/content'
    assert client.get(path).status_code == 404
    with worker.sessions.begin() as db:
        db.get(Run, run_id).workflow_version = 'stage-agent-v9'
    response = client.get(path)
    assert response.status_code == 200 and response.json()['schema_version'] == 2
    assert client.get(f'/api/v1/runs/{uuid4()}/artifacts/{artifact["id"]}/content').status_code == 404
