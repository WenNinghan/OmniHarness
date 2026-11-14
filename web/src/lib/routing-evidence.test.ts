import { test } from 'node:test'
import assert from 'node:assert/strict'
import { emptyEvidence, prepareEvidence } from './routing-evidence.ts'
import type { ModelConfig } from './types.ts'
const models = [
  { id: 'one', name: 'One', enabled: true },
  { id: 'two', name: 'Two', enabled: true },
  { id: 'off', name: 'Off', enabled: false },
] as ModelConfig[]

test('missing evidence remains unknown, not zero-priced or zero-scored', () => {
  assert.deepEqual(prepareEvidence(emptyEvidence(), models), { assessments: [], stage_requirements: {} })
})
test('explicit evidence retains price precision, scores and usage assumptions', () => {
  const draft = emptyEvidence()
  draft.models.one = { input: '0.00000123', output: '2.5', currency: 'CNY', source: '自填报价', scores: { plan: '70', execute: '0' } }
  draft.stages.plan = { minimum: '60', input: '5000', output: '1000' }
  const prepared = prepareEvidence(draft, models)
  assert.deepEqual(prepared.assessments[0], { model_config_id: 'one', input_price_per_million: '0.00000123', output_price_per_million: '2.5', currency: 'CNY', source: '自填报价', stage_scores: { plan: 70, execute: 0 } })
  assert.deepEqual(prepared.stage_requirements.plan, { minimum_score: 60, expected_input_tokens: 5000, expected_output_tokens: 1000 })
})
test('partial prices or invalid requirements cannot silently become evidence', () => {
  const draft = emptyEvidence()
  draft.models.one = { input: '1', output: '', currency: 'CNY', source: '自填报价', scores: { plan: '70' } }
  assert.throws(() => prepareEvidence(draft, models), /单价/)
  draft.models.one.output = '2'
  draft.stages.plan = { minimum: '80', input: '0', output: '0' }
  assert.throws(() => prepareEvidence(draft, models), /大于零/)
  draft.stages.plan.output = '20'
  draft.models.one.scores.plan = '101'
  assert.throws(() => prepareEvidence(draft, models), /0 至 100/)
})
test('disabled models are not sent as candidates or assessments', () => {
  const draft = emptyEvidence()
  draft.models.off = { input: '1', output: '2', currency: 'CNY', source: 'test', scores: { plan: '70' } }
  assert.equal(prepareEvidence(draft, models).assessments.length, 0)
})
