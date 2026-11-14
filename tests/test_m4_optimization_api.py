"""Public contracts needed to compare frozen v4 runs without provider bodies."""
from conftest import create_chat

from omniharness.models import OperationRecord, Run
from omniharness.schemas import SendMessageRequest
from omniharness.services import enqueue, freeze_stage_run_config, run_output
from omniharness.stage_contract import STAGE_KEYS


def test_context_v2_export_and_run_freeze_preserve_main_output(environment):
    _, _, factory, client = environment
    model = client.post('/api/v1/model-configs', json={
        'name': 'v2', 'adapter_type': 'mock', 'model_name': 'demo',
        'max_output_tokens': 100000,
        'context_policy': {'policy': {'version': 'context-v2'}},
    }).json()
    assert model['context_policy']['policy']['summary_recovery_attempts'] == 1
    conversation = client.post('/api/v1/conversations', json={}).json()
    with factory.begin() as db:
        result = enqueue(db, conversation['id'], SendMessageRequest(
            content='test', model_config_id=model['id'], client_request_id='v2-freeze'))
        frozen = db.get(Run, result['run_id']).config_snapshot
        assert frozen['max_output_tokens'] == 100000
        assert frozen['context_policy'] == model['context_policy']
    exported = client.get('/api/v1/configuration/export').json()
    assert exported['model_configs'][0]['context_policy'] == model['context_policy']


def test_single_ignores_global_role_overrides_and_v4_reports_actual_stages(environment):
    settings, _, factory, client = environment
    selected = client.post('/api/v1/model-configs', json={
        'name': 'selected', 'adapter_type': 'mock', 'model_name': 'selected'}).json()
    alternate = client.post('/api/v1/model-configs', json={
        'name': 'alternate', 'adapter_type': 'mock', 'model_name': 'alternate'}).json()
    assert client.put('/api/v1/stage-bindings', json={'expected_version': 0,
        'bindings': [{'stage': role, 'model_config_id': alternate['id']} for role in STAGE_KEYS]
    }).status_code == 200
    conversation = client.post('/api/v1/conversations', json={}).json()
    with factory.begin() as db:
        result = enqueue(db, conversation['id'], SendMessageRequest(
            content='test', model_config_id=selected['id'], client_request_id='single-freeze'))
        run = db.get(Run, result['run_id'])
        run.config_snapshot = {**run.config_snapshot, 'workflow_type': 'agent'}
        freeze_stage_run_config(db, run, settings, strategy='single')
        assert all(item['model']['id'] == selected['id'] for item in run.config_snapshot['agent']['roles'].values())
        run.workflow_version = 'stage-agent-v4'
        run.config_snapshot = {**run.config_snapshot, 'workflow_type': 'agent',
            'agent': {**run.config_snapshot['agent'], 'strategy': 'compact'}}
        output = run_output(run)
        assert output['active_stages'] == ['plan', 'execute', 'review']
        assert output['workflow_version'] == output['execution_config']['workflow_version'] == 'stage-agent-v4'


def test_operation_usage_keeps_identity_for_uncertain_and_hides_reasoning(environment):
    _, _, factory, client = environment
    _, _, created = create_chat(client)
    binding = {'model_config_id': 'frozen-id', 'model_version': 4, 'model_name': 'deepseek-flash',
               'adapter_type': 'openai-compatible', 'base_url': 'https://example.test/v1'}
    with factory.begin() as db:
        for identity, status, response in [('a', 'completed', {'messages': [{
            'content': 'private response', 'reasoning_content': 'private reasoning',
            'usage_metadata': {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30,
                'input_token_details': {'cache_read': 5}, 'output_token_details': {'reasoning': 8}},
        }]}), ('b', 'uncertain', {})]:
            db.add(OperationRecord(operation_id=identity * 64, run_id=created['run_id'],
                node_id='agent:execute:model_calls:t1:1', parameters_hash='c' * 64, status=status,
                workflow_version='stage-agent-v4', result={'model_binding': binding,
                    'purpose': 'task', 'response': response}))
    response = client.get(f"/api/v1/runs/{created['run_id']}/operations")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2 and all(row['model_binding'] == binding for row in rows)
    usage = next(row for row in rows if row['status'] == 'completed')['normalized_usage'][0]
    assert usage['complete'] and usage['output_tokens'] == 10 and usage['reasoning_output_tokens'] == 8
    assert usage['input_includes_cache'] is True
    assert 'private response' not in response.text and 'private reasoning' not in response.text
