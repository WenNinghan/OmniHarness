import test from 'node:test'
import assert from 'node:assert/strict'
import { displayedRunModel } from './model-run-state.ts'
import type { Run } from './types.ts'

test('running and waiting runs display their frozen model rather than the next task choice', () => {
  const run = { model_config_id: 'glm-id', model_label: 'GLM · glm-5.3-flash', workflow_type: 'agent', agent_strategy: 'compact' } as Run
  for (const status of ['running', 'awaiting_user', 'awaiting_approval', 'paused'] as const) {
    const shown = displayedRunModel(true, { ...run, status }, 'hy3-id')
    assert.equal(shown.id, 'glm-id')
    assert.equal(shown.frozenLabel, 'GLM · glm-5.3-flash')
    assert.equal(shown.stageNote, true)
    assert.match(shown.frozenSubtitle ?? '', /阶段可能使用其他模型/)
  }
})

test('missing or removed configurations still use only frozen run identity', () => {
  const old = { model_config_id: 'removed-id', model_label: '当时的连接', workflow_type: 'agent' } as Run
  assert.equal(displayedRunModel(true, old, 'hy3-id').frozenLabel, '当时的连接')
  assert.equal(displayedRunModel(true, { ...old, model_label: '' }, 'hy3-id').frozenLabel, '模型名称未记录（ID：removed-id）')
  assert.equal(displayedRunModel(true, {} as Run, 'hy3-id').frozenLabel, '本次任务模型未记录')
})

test('single or chat runs do not claim stage overrides and terminal restores draft selection', () => {
  const run = { model_config_id: 'glm-id', model_label: 'GLM', workflow_type: 'agent', agent_strategy: 'single' } as Run
  assert.equal(displayedRunModel(true, run, 'hy3-id').stageNote, false)
  assert.equal(displayedRunModel(true, { ...run, workflow_type: 'chat' }, 'hy3-id').stageNote, false)
  assert.deepEqual(displayedRunModel(false, { ...run, status: 'completed' }, 'hy3-id'), { id: 'hy3-id', stageNote: false })
})
