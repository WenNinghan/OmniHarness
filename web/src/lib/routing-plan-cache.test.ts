import assert from 'node:assert/strict'
import { test } from 'node:test'
import { restoreRoutingPlans, serializeRoutingPlans } from './routing-plan-cache.ts'
import { stages } from './types.ts'
import type { PendingRoutingPlan } from './types.ts'

test('refresh cache contains only model identities and cannot apply a plan without confirmation', () => {
  const plan: PendingRoutingPlan = { task: 'private user question', acceptedTask: 'private user question',
    stages: stages.map(stage => ({ stage, model_config_id: `id-${stage}`, model_version: 1, provider_version: 2,
      reason: 'private generated reason' })), warnings: ['private warning'],
    planner: { model_config_id: 'planner', model_name: 'private model name', endpoint: 'private endpoint' },
    usage: { input_tokens: 11, output_tokens: 2, total_tokens: 13, cost: null } }
  const raw = serializeRoutingPlans({ conv123: plan, __new__: plan })
  assert.doesNotMatch(raw, /private|__new__/)
  const restored = restoreRoutingPlans(raw).conv123
  assert.equal(restored.acceptedTask, null)
  assert.equal(restored.task, '')
  assert.equal(restored.stages.length, 5)
})

test('invalid and duplicate stage cache is ignored', () => {
  const item = { stage: 'think', model_config_id: 'id', model_version: 1, provider_version: 1 }
  assert.deepEqual(Object.keys(restoreRoutingPlans(JSON.stringify({ one: Array(5).fill(item) }))), [])
  assert.deepEqual(Object.keys(restoreRoutingPlans('not json')), [])
})
