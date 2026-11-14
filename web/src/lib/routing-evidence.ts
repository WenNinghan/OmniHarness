import type { ModelConfig, Stage } from './types.ts'
import { stages, stageLabels } from './types.ts'

export type ModelEvidenceDraft = { input: string; output: string; currency: string; source: string; scores: Partial<Record<Stage, string>> }
export type StageEvidenceDraft = { minimum: string; input: string; output: string }
export type EvidenceDraft = { models: Record<string, ModelEvidenceDraft>; stages: Partial<Record<Stage, StageEvidenceDraft>> }
export type EvidenceRequest = {
  assessments: { model_config_id: string; stage_scores: Partial<Record<Stage, number>>; input_price_per_million: string; output_price_per_million: string; currency: string; source: string }[]
  stage_requirements: Partial<Record<Stage, { minimum_score: number; expected_input_tokens: number; expected_output_tokens: number }>>
}
export const emptyEvidence = (): EvidenceDraft => ({ models: {}, stages: {} })
export const emptyModelEvidence = (): ModelEvidenceDraft => ({ input: '', output: '', currency: '', source: '', scores: {} })
export const emptyStageEvidence = (): StageEvidenceDraft => ({ minimum: '', input: '', output: '' })
const validScore = (s: string) => s.trim() !== '' && Number.isFinite(Number(s)) && Number(s) >= 0 && Number(s) <= 100
const validPrice = (s: string) => /^(?:0|[1-9]\d{0,9})(?:\.\d{1,8})?$/.test(s.trim())
const validTokens = (s: string) => /^\d+$/.test(s.trim()) && Number(s) <= 10_000_000

/** Only explicitly filled values enter comparisons; unknown is never zero. */
export function prepareEvidence(draft: EvidenceDraft, models: ModelConfig[]): EvidenceRequest {
  const assessments: EvidenceRequest['assessments'] = []
  const stage_requirements: EvidenceRequest['stage_requirements'] = {}
  for (const model of models.filter(item => item.enabled)) {
    const row = draft.models[model.id]
    if (!row) continue
    const present = [row.input, row.output, row.currency, row.source, ...Object.values(row.scores)].some(value => value?.trim())
    if (!present) continue
    if (!validPrice(row.input) || !validPrice(row.output) || !/^[A-Z]{3}$/.test(row.currency.trim()) || !row.source.trim() || row.source.trim().length > 200)
      throw new Error(`${model.name}：请输入有效的输入和输出单价、三位大写币种及来源。`)
    const stage_scores: Partial<Record<Stage, number>> = {}
    for (const stage of stages) {
      const value = row.scores[stage]?.trim() ?? ''
      if (!value) continue
      if (!validScore(value)) throw new Error(`${model.name}的${stageLabels[stage]}评分应在 0 至 100 之间。`)
      stage_scores[stage] = Number(value)
    }
    assessments.push({ model_config_id: model.id, stage_scores, input_price_per_million: row.input.trim(), output_price_per_million: row.output.trim(), currency: row.currency.trim(), source: row.source.trim() })
  }
  for (const stage of stages) {
    const row = draft.stages[stage]
    if (!row || ![row.minimum, row.input, row.output].some(value => value.trim())) continue
    if (!validScore(row.minimum) || !validTokens(row.input) || !validTokens(row.output) || !(Number(row.input) || Number(row.output)))
      throw new Error(`${stageLabels[stage]}：请完整填写 0–100 的评分门槛和非负整数用量，至少一项用量需大于零。`)
    stage_requirements[stage] = { minimum_score: Number(row.minimum), expected_input_tokens: Number(row.input), expected_output_tokens: Number(row.output) }
  }
  if (assessments.length > 100) throw new Error('最多比较 100 个模型。')
  return { assessments, stage_requirements }
}
