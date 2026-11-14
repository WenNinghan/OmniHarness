import test from 'node:test'
import assert from 'node:assert/strict'
import { quoteObservedAt, quoteSource, referencePrice } from './reference-prices.ts'
import { relativePrice } from './model-picker.ts'
import type { ModelConfig } from './types.ts'

const model = (model_name: string, base_url = 'https://e-flowcode.cc/v1') => ({ model_name, base_url, adapter_type: 'openai-compatible' } as ModelConfig)
const at = new Date('2026-11-08T14:00:00Z')

test('highest-band snapshot covers all 25 configured models with a shared source and basis', () => {
  const names = ['deepseek-flash', 'deepseek-v4-pro', 'glm-5', 'glm-5.1', 'glm-5.2', 'glm-5.2-fast-preview',
    'glm-5.3', 'glm-5.3-flash', 'glm-5.3-prime', 'hy3', 'hy4', 'kimi-k2.6', 'kimi-k2.7', 'kimi-k2.7-code',
    'kimi-k2.8-preview', 'kimi-k3', 'kimi-k3.1-pro', 'mimo-v2.5-pro', 'MiniMax-M2.5', 'qwen3.5-plus',
    'qwen3.6-plus', 'qwen3.7-max', 'qwen3.8-flash', 'qwen3.8-max', 'qwen3.8-max-0902']
  assert.equal(names.length, 25)
  const baseline = referencePrice(model('deepseek-flash'), at)
  for (const name of names) {
    const quote = referencePrice(model(name), at)
    assert.ok(quote, name)
    assert.equal(quote.source, quoteSource)
    assert.equal(quote.observedAt, quoteObservedAt)
    assert.equal(quote.comparisonKey, baseline?.comparisonKey)
    assert.equal(quote.basis, 'public_same_group')
    assert.match(quote.note ?? '', /最高档参考/)
    assert.ok(quote.input > 0 && quote.output > 0, name)
    assert.notEqual(relativePrice(quote, baseline, 3, 1), null, name)
  }
})

test('DeepSeek uses the highest time band at both day and night', () => {
  const day = new Date('2026-11-08T12:00:00Z')
  const night = new Date('2026-11-08T15:00:00Z')
  const flash = model('deepseek-flash'), pro = model('deepseek-v4-pro'), glm = model('glm-5.3-flash')
  assert.deepEqual(referencePrice(flash, day), referencePrice(flash, night))
  assert.equal(referencePrice(flash, night)?.input, 3)
  assert.equal(referencePrice(flash, night)?.output, 9)
  assert.equal(referencePrice(pro, night)?.input, 9)
  assert.equal(referencePrice(pro, night)?.output, 27)
  assert.equal(relativePrice(referencePrice(glm, day), referencePrice(flash, day), 3, 1), 5.2 / 18)
  assert.equal(relativePrice(referencePrice(pro, night), referencePrice(flash, night), 3, 1), 3)
})

test('GLM 5.1 uses its highest context band instead of its lower ratio or short-context price', () => {
  const quote = referencePrice(model('glm-5.1'), at)
  assert.equal(quote?.input, 8)
  assert.equal(quote?.output, 28)
  assert.match(quote?.note ?? '', /32,000/)
})

test('ordinary ratios use the same per-million units as expression prices', () => {
  const rows: [string, number, number][] = [
    ['glm-5', 2, 4.5], ['glm-5.2', 4, 3.5], ['glm-5.2-fast-preview', 8, 3.5],
    ['mimo-v2.5-pro', .174, 2.5], ['MiniMax-M2.5', 1.05, 4],
    ['qwen3.5-plus', .4, 6], ['qwen3.6-plus', 1, 6], ['qwen3.7-max', 6, 3],
    ['qwen3.8-flash', .4, 3.375], ['qwen3.8-max', 6, 3], ['qwen3.8-max-0902', 6, 3],
  ]
  for (const [name, inputRatio, completionRatio] of rows) {
    const quote = referencePrice(model(name), at)
    assert.ok(quote, name)
    assert.equal(quote.input, inputRatio * 2, name)
    assert.ok(Math.abs(quote.output - inputRatio * 2 * completionRatio) < 1e-12, name)
  }
  assert.equal(relativePrice(referencePrice(model('glm-5.2'), at), referencePrice(model('glm-5.3'), at), 3, 1), 1)
  assert.equal(relativePrice(referencePrice(model('qwen3.8-max-0902'), at), referencePrice(model('deepseek-flash'), at), 3, 1), 4)
})

test('unknown models, other endpoints, other adapters and expired quotations stay unknown', () => {
  assert.equal(referencePrice(model('unreviewed-model'), at), null)
  assert.equal(referencePrice(model('deepseek-flash', 'https://another.example/v1'), at), null)
  assert.equal(referencePrice(model('deepseek-flash', 'https://e-flowcode.cc/v1/other'), at), null)
  assert.equal(referencePrice(model('deepseek-flash', 'https://e-flowcode.cc/v1?group=other'), at), null)
  assert.equal(referencePrice({ ...model('deepseek-flash'), adapter_type: 'openai' }, at), null)
  assert.equal(referencePrice(model('deepseek-flash'), new Date('2026-12-01T00:00:00Z')), null)
  assert.equal(referencePrice(model('deepseek-flash'), new Date(NaN)), null)
  assert.ok(referencePrice(model('deepseek-flash', 'https://e-flowcode.cc/v1/'), at))
})
