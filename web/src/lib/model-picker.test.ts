import { test } from 'node:test'
import assert from 'node:assert/strict'
import { comparisonMix, evidencePrice, filterModels, formatOutputLimit, formatPriceRatio, groupModels, modelDisplayName, parsePricePreferences, relativePrice } from './model-picker.ts'
import type { ModelPrice } from './model-picker.ts'
import { emptyEvidence } from './routing-evidence.ts'
import type { ModelConfig } from './types.ts'

const model = (id: string, name = id): ModelConfig => ({ id, name, model_name: id, adapter_type: 'openai-compatible', base_url: 'https://example.test/v1', enabled: true } as ModelConfig)
const quote: ModelPrice = { input: 1.5, output: 4.5, comparisonKey: 'same:CNY', source: 'A dated quote', basis: 'user_estimate' }

test('output limits show 100K exactly and preserve custom token counts', () => {
  assert.equal(formatOutputLimit(100000), '100K')
  assert.equal(formatOutputLimit(128000), '128K')
  assert.equal(formatOutputLimit(4096), '4,096')
  assert.equal(formatOutputLimit(131072), '131,072')
  assert.equal(formatOutputLimit(16), '16')
  assert.equal(formatOutputLimit(0), '未知')
  assert.equal(formatOutputLimit(NaN), '未知')
})

test('model titles remove duplicate IDs but keep real user aliases', () => {
  assert.equal(modelDisplayName(model('deepseek-v4-pro')), 'DeepSeek V4 Pro')
  assert.equal(modelDisplayName(model('kimi-k3.1-pro')), 'Kimi K3.1 Pro')
  assert.equal(modelDisplayName(model('qwen3.8-max-0902')), 'Qwen 3.8 Max 0902')
  assert.equal(modelDisplayName(model('hy4')), 'HY4')
  assert.equal(modelDisplayName(model('glm-5.3-flash', '我的快速模型')), '我的快速模型')
})

test('search matches real IDs, aliases and endpoint while grouping by series', () => {
  const models = [model('glm-5.3'), model('hy3'), model('glm-5.3-flash', '便捷模型')]
  assert.deepEqual(filterModels(models, 'glm flash').map(item => item.id), ['glm-5.3-flash'])
  assert.deepEqual(filterModels(models, '便捷 example.test').map(item => item.id), ['glm-5.3-flash'])
  assert.deepEqual(groupModels(models).map(group => [group.family, group.models.length]), [['GLM', 2], ['HY', 1]])
})

test('price comparisons require a shared basis and nonzero baseline', () => {
  assert.equal(relativePrice({ ...quote, input: .8, output: 2.8 }, quote, 3, 1), 5.2 / 9)
  assert.equal(relativePrice(quote, { ...quote, comparisonKey: 'different:USD' }, 3, 1), null)
  assert.equal(relativePrice(quote, { ...quote, basis: 'public_same_group' }, 3, 1), null)
  assert.equal(relativePrice(null, quote, 3, 1), null)
  assert.equal(relativePrice(quote, { ...quote, input: 0, output: 0 }, 3, 1), null)
  assert.equal(relativePrice({ ...quote, input: NaN }, quote, 3, 1), null)
  assert.equal(relativePrice(quote, quote, 0, 0), null)
  assert.equal(formatPriceRatio(null), '价格未知')
  assert.equal(formatPriceRatio(.0003), '<0.01×')
  assert.equal(formatPriceRatio(1), '≈1.0×')
})

test('partial user evidence remains unknown and is never zero by coercion', () => {
  const evidence = emptyEvidence()
  evidence.models.one = { input: '', output: '1', currency: 'CNY', source: 'my estimate', scores: {} }
  assert.equal(evidencePrice(model('one'), evidence), null)
  evidence.models.one.input = '0.5'
  assert.equal(evidencePrice(model('one'), evidence)?.input, .5)
  evidence.models.one.currency = 'USD'
  assert.notEqual(evidencePrice(model('one'), evidence)?.comparisonKey, 'user:CNY:million-tokens')
})

test('invalid stored preferences fall back and stage volume overrides generic mix', () => {
  assert.deepEqual(parsePricePreferences('{"baselineId":"old","input":null,"output":1}'), { baselineId: '', input: 3, output: 1 })
  assert.deepEqual(parsePricePreferences('{"baselineId":"gone","input":2,"output":1}'), { baselineId: 'gone', input: 2, output: 1 })
  const evidence = emptyEvidence()
  evidence.stages.execute = { minimum: '60', input: '1000', output: '5000' }
  assert.deepEqual(comparisonMix({ baselineId: '', input: 3, output: 1 }, evidence, 'execute'), { input: 1000, output: 5000, stageBased: true })
  evidence.stages.execute.input = ''
  assert.equal(comparisonMix({ baselineId: '', input: 3, output: 1 }, evidence, 'execute').stageBased, false)
})
