import type { EvidenceDraft } from './routing-evidence.ts'
import type { ModelConfig, Stage } from './types.ts'

export interface ModelPrice {
  input: number
  output: number
  comparisonKey: string
  source: string
  observedAt?: string
  basis: 'user_estimate' | 'public_same_group'
  note?: string
}
export interface PricePreferences { baselineId: string; input: number; output: number }
export const defaultPricePreferences = (): PricePreferences => ({ baselineId: '', input: 3, output: 1 })
export const pricePreferencesKey = 'omniharness.price-comparison.v1'

export function parsePricePreferences(raw: string | null): PricePreferences {
  try {
    const value: unknown = JSON.parse(raw || 'null')
    if (!value || typeof value !== 'object' || Array.isArray(value)) return defaultPricePreferences()
    const item = value as Record<string, unknown>
    if (typeof item.baselineId !== 'string' || !validMix(item.input, item.output)) return defaultPricePreferences()
    return { baselineId: item.baselineId, input: item.input as number, output: item.output as number }
  } catch { return defaultPricePreferences() }
}

export function validMix(input: unknown, output: unknown): boolean {
  return typeof input === 'number' && typeof output === 'number' && Number.isFinite(input) && Number.isFinite(output)
    && input >= 0 && output >= 0 && input <= 10_000_000 && output <= 10_000_000 && input + output > 0
}

export function modelFamily(model: Pick<ModelConfig, 'model_name' | 'adapter_type'>): string {
  if (model.adapter_type === 'mock') return '离线演示'
  const name = model.model_name.toLowerCase()
  if (/^deepseek(?:[-/]|$)/.test(name)) return 'DeepSeek'
  if (/^kimi(?:[-/]|$)/.test(name)) return 'Kimi'
  if (/^glm(?:[-/]|$)/.test(name)) return 'GLM'
  if (/^qwen(?:\d|[-/]|$)/.test(name)) return 'Qwen'
  if (/^hy\d/.test(name)) return 'HY'
  if (/^minimax(?:[-/]|$)/.test(name)) return 'MiniMax'
  if (/^mimo(?:[-/]|$)/.test(name)) return 'MiMo'
  if (/^(gpt|o[134])(?:\d|[-/.]|$)/.test(name)) return 'OpenAI'
  if (/^claude(?:[-/]|$)/.test(name)) return 'Claude'
  if (/^gemini(?:[-/]|$)/.test(name)) return 'Gemini'
  return '其他模型'
}

export function modelDisplayName(model: Pick<ModelConfig, 'name' | 'model_name' | 'adapter_type'>): string {
  if (model.name.trim() && model.name.trim().toLowerCase() !== model.model_name.toLowerCase()) return model.name.trim()
  const family = modelFamily(model)
  if (!['DeepSeek', 'Kimi', 'GLM', 'Qwen', 'HY', 'MiniMax', 'MiMo'].includes(family)) return model.model_name
  if (family === 'HY') return model.model_name.toUpperCase()
  const prefix = family === 'Qwen' ? /^qwen/i : new RegExp(`^${family}[-/]?`, 'i')
  const suffix = model.model_name.replace(prefix, '').split('-').map(part => {
    if (/^\d/.test(part)) return part
    if (/^[kmv]\d/i.test(part)) return part[0].toUpperCase() + part.slice(1)
    return part ? part[0].toUpperCase() + part.slice(1) : ''
  }).join(' ')
  return `${family} ${suffix}`.trim()
}

export function modelHost(model: Pick<ModelConfig, 'base_url' | 'adapter_type'>): string {
  if (!model.base_url) return model.adapter_type === 'mock' ? '本地演示' : '原生连接'
  try { return new URL(model.base_url).host } catch { return '自定义连接' }
}

export function formatOutputLimit(tokens: number): string {
  if (!Number.isSafeInteger(tokens) || tokens <= 0) return '未知'
  return tokens >= 1000 && tokens % 1000 === 0 ? `${tokens / 1000}K` : tokens.toLocaleString('en-US')
}

export function filterModels(models: ModelConfig[], query: string): ModelConfig[] {
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  return models.filter(model => {
    const text = `${modelDisplayName(model)} ${model.model_name} ${modelFamily(model)} ${modelHost(model)}`.toLocaleLowerCase()
    return words.every(word => text.includes(word))
  })
}

export function groupModels(models: ModelConfig[]): { family: string; models: ModelConfig[] }[] {
  const groups = new Map<string, ModelConfig[]>()
  for (const model of models) {
    const family = modelFamily(model)
    const rows = groups.get(family) ?? []
    rows.push(model)
    groups.set(family, rows)
  }
  return [...groups.entries()].map(([family, items]) => ({ family, models: items }))
}

export function evidencePrice(model: ModelConfig, evidence: EvidenceDraft): ModelPrice | null {
  const row = evidence.models[model.id]
  if (!row || !/^(?:0|[1-9]\d{0,9})(?:\.\d{1,8})?$/.test(row.input.trim())
      || !/^(?:0|[1-9]\d{0,9})(?:\.\d{1,8})?$/.test(row.output.trim())
      || !/^[A-Z]{3}$/.test(row.currency.trim()) || !row.source.trim()) return null
  return { input: Number(row.input), output: Number(row.output), comparisonKey: `user:${row.currency.trim()}:million-tokens`,
    source: row.source.trim(), basis: 'user_estimate', note: `用户填写的 ${row.currency.trim()} / 百万 Token 估计；不是实际账单。` }
}

export function comparisonMix(preferences: PricePreferences, evidence: EvidenceDraft, stage?: Stage): { input: number; output: number; stageBased: boolean } {
  const row = stage ? evidence.stages[stage] : undefined
  if (row && /^\d+$/.test(row.input.trim()) && /^\d+$/.test(row.output.trim()) && validMix(Number(row.input), Number(row.output))) {
    return { input: Number(row.input), output: Number(row.output), stageBased: true }
  }
  return { input: preferences.input, output: preferences.output, stageBased: false }
}

export function relativePrice(price: ModelPrice | null, baseline: ModelPrice | null, input: number, output: number): number | null {
  if (!price || !baseline || price.comparisonKey !== baseline.comparisonKey || price.basis !== baseline.basis || !validMix(input, output)) return null
  if (![price.input, price.output, baseline.input, baseline.output].every(value => Number.isFinite(value) && value >= 0)) return null
  const divisor = baseline.input * input + baseline.output * output
  if (!(divisor > 0)) return null
  const ratio = (price.input * input + price.output * output) / divisor
  return Number.isFinite(ratio) ? ratio : null
}

export function formatPriceRatio(ratio: number | null): string {
  if (ratio === null || !Number.isFinite(ratio) || ratio < 0) return '价格未知'
  if (ratio > 0 && ratio < 0.01) return '<0.01×'
  return `≈${ratio.toLocaleString('en-US', { maximumFractionDigits: 2, minimumFractionDigits: ratio === 1 ? 1 : 0 })}×`
}
