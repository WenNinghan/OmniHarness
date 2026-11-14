"""Stage bindings remain separate from old roles and are frozen per Run."""

from omniharness.models import Run
from omniharness.schemas import SendMessageRequest
from omniharness.services import enqueue, freeze_stage_run_config, validate_agent_run_versions
from omniharness.stage_contract import STAGE_KEYS, STAGE_VERSIONS, STAGE_WORKFLOW_VERSION


def test_stage_configuration_optimistic_version_and_snapshot(environment):
    settings, _, factory, client = environment
    default = client.post('/api/v1/model-configs', json={
        'name': 'default', 'adapter_type': 'mock', 'model_name': 'a'}).json()
    other = client.post('/api/v1/model-configs', json={
        'name': 'alternate', 'adapter_type': 'mock', 'model_name': 'b'}).json()
    before = client.get('/api/v1/stage-bindings').json()
    assert before['version'] == 0
    selection = [{'stage': stage, 'model_config_id': other['id'] if stage == 'review' else None}
                 for stage in STAGE_KEYS]
    saved = client.put('/api/v1/stage-bindings', json={'expected_version': 0, 'bindings': selection})
    assert saved.status_code == 200 and saved.json()['version'] == 1
    assert client.put('/api/v1/stage-bindings', json={'expected_version': 0, 'bindings': selection}).status_code == 409
    conversation = client.post('/api/v1/conversations', json={}).json()
    with factory.begin() as db:
        created = enqueue(db, conversation['id'], SendMessageRequest(content='test',
                          model_config_id=default['id'], client_request_id='stage-snapshot'))
        run = db.get(Run, created['run_id'])
        run.config_snapshot = {**run.config_snapshot, 'workflow_type': 'agent'}
        freeze_stage_run_config(db, run, settings)
    selection[-1]['model_config_id'] = None
    assert client.put('/api/v1/stage-bindings', json={'expected_version': 1, 'bindings': selection}).status_code == 200
    with factory() as db:
        run = db.get(Run, created['run_id'])
        agent = run.config_snapshot['agent']
        assert agent['role_binding_version'] == 1
        assert agent['roles']['review']['model']['id'] == other['id']
        assert agent['roles']['think']['model']['id'] == default['id']
        validate_agent_run_versions(run, STAGE_WORKFLOW_VERSION, role_versions=STAGE_VERSIONS, plugin_versions={})
    assert client.get('/api/v1/orchestration/bindings').json()['version'] == 0


def test_stage_bindings_reject_duplicates_and_missing_models(client):
    bindings = [{'stage': 'think', 'model_config_id': None}] * 5
    assert client.put('/api/v1/stage-bindings', json={'expected_version': 0, 'bindings': bindings}).status_code == 422
    bindings = [{'stage': stage, 'model_config_id': 'missing' if stage == 'think' else None}
                for stage in STAGE_KEYS]
    assert client.put('/api/v1/stage-bindings', json={'expected_version': 0, 'bindings': bindings}).status_code == 404
