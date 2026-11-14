import priceSnapshot from '../../../src/omniharness/data/reference-prices-v1.json' with { type: 'json' }
import type { ModelConfig } from './types.ts'
import type { ModelPrice } from './model-picker.ts'

// Public reference snapshot, NOT account billing. Never evaluate remote expressions.
// Reviewed against the deployed pricing UI: ratio prices use input = 2 * ratio,
// output = input * completion_ratio; expression p/c coefficients share those units.
// All entries use the highest published band, before the common group multiplier.
export const quoteSource = priceSnapshot.source
export const quoteObservedAt = priceSnapshot.observed_at
export const quoteVersion = priceSnapshot.version
const quotes = priceSnapshot.quotes as Record<string, { input: string; output: string; note?: string }>

export function referencePrice(model: ModelConfig, at = new Date()): ModelPrice | null {
  if (model.adapter_type !== 'openai-compatible') return null
  try {
    const url = new URL(model.base_url ?? '')
    if (url.origin !== 'https://e-flowcode.cc' || url.pathname.replace(/\/$/, '') !== '/v1' || url.search || url.hash) return null
  } catch { return null }
  const rate = quotes[model.model_name]
  if (!rate || !Number.isFinite(at.getTime())) return null
  if (at.getTime() - Date.parse(quoteObservedAt) > 7 * 24 * 60 * 60 * 1000) return null
  return {
    input: Number(rate.input), output: Number(rate.output),
    comparisonKey: `eflow:1_vip_china:highest-band:${quoteVersion}`,
    source: quoteSource, observedAt: quoteObservedAt, basis: 'public_same_group',
    note: `最高档参考 · 假定 1_vip_china 同组、无缓存；共同分组倍率在比较时抵消，不代表实扣金额。${rate.note ?? '当前公开报价只有一个档位。'}报价快照有效期 7 天。`,
  }
}
