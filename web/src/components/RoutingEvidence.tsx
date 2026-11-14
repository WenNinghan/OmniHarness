import { useState } from 'react'
import type { ModelConfig, Stage } from '../lib/types.ts'
import { stages, stageLabels } from '../lib/types.ts'
import { emptyEvidence, emptyModelEvidence, emptyStageEvidence, prepareEvidence } from '../lib/routing-evidence.ts'
import type { EvidenceDraft, ModelEvidenceDraft, StageEvidenceDraft } from '../lib/routing-evidence.ts'
import { Button } from './ui.tsx'

const storageKey = 'omniharness.routing-estimates.v1'
// These are user-entered estimates, not provider credentials or validated evidence.
export function readRoutingEvidence(): EvidenceDraft {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(storageKey) || 'null')
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return emptyEvidence()
    const obj = raw as Record<string, unknown>
    if (!obj.models || typeof obj.models !== 'object' || Array.isArray(obj.models) || !obj.stages || typeof obj.stages !== 'object' || Array.isArray(obj.stages)) return emptyEvidence()
    const result = emptyEvidence()
    for (const [id, value] of Object.entries(obj.models)) {
      if (!value || typeof value !== 'object' || Array.isArray(value)) continue
      const row = value as Record<string, unknown>
      if (![row.input, row.output, row.currency, row.source].every(item => typeof item === 'string') || !row.scores || typeof row.scores !== 'object' || Array.isArray(row.scores)) continue
      const scores: ModelEvidenceDraft['scores'] = {}
      for (const stage of stages) { const score = (row.scores as Record<string, unknown>)[stage]; if (typeof score === 'string') scores[stage] = score }
      result.models[id] = { input: row.input as string, output: row.output as string, currency: row.currency as string, source: row.source as string, scores }
    }
    for (const stage of stages) {
      const value = (obj.stages as Record<string, unknown>)[stage]
      if (!value || typeof value !== 'object' || Array.isArray(value)) continue
      const row = value as Record<string, unknown>
      if ([row.minimum, row.input, row.output].every(item => typeof item === 'string')) result.stages[stage] = row as StageEvidenceDraft
    }
    return result
  } catch { return emptyEvidence() }
}

export function RoutingEvidence({ models, initial, onApply }: { models: ModelConfig[]; initial: EvidenceDraft; onApply: (value: EvidenceDraft) => void }) {
  const [draft, setDraft] = useState(initial)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  function updateModel(id: string, patch: Partial<ModelEvidenceDraft>) {
    setDraft(old => ({ ...old, models: { ...old.models, [id]: { ...(old.models[id] ?? emptyModelEvidence()), ...patch } } }))
  }
  function updateStage(stage: Stage, patch: Partial<StageEvidenceDraft>) {
    setDraft(old => ({ ...old, stages: { ...old.stages, [stage]: { ...(old.stages[stage] ?? emptyStageEvidence()), ...patch } } }))
  }
  function apply() {
    try {
      prepareEvidence(draft, models)
      onApply(draft)
      try { localStorage.setItem(storageKey, JSON.stringify(draft)); setMessage('已保存到此浏览器并刷新推荐。') }
      catch { setMessage('推荐已刷新，但浏览器无法保存这些填写内容。') }
      setError('')
    } catch (reason) { setMessage(''); setError(reason instanceof Error ? reason.message : '输入有误。') }
  }
  return <details className="evidence-editor">
    <summary>填写推荐依据（可选）</summary>
    <p>以下评分、单价、来源和用量均由你自行填写，是估计值，不能当作实测能力或服务商账单。输入与输出单价的单位均为每百万 Token；请填写相同币种的数据才可比较。</p>
    <p>未填内容保持未知；某阶段至少两个模型有完整同币种价格且评分达到门槛时，系统才按填写的用量估算比较，否则继承默认模型。信息仅保存在本浏览器；发送给本机服务生成推荐，不包含 API Key。</p>
    <h3>阶段最低评分与预计用量</h3>
    {stages.map(stage => { const row = draft.stages[stage] ?? emptyStageEvidence(); return <div className="evidence-row" key={stage}>
      <strong>{stageLabels[stage]}</strong>
      <label>最低分<input aria-label={`${stageLabels[stage]}最低分`} type="number" min="0" max="100" step="any" placeholder="未知" value={row.minimum} onChange={event => updateStage(stage, { minimum: event.target.value })} /></label>
      <label>预计输入 Token<input aria-label={`${stageLabels[stage]}预计输入 Token`} type="number" min="0" max="10000000" step="1" placeholder="未知" value={row.input} onChange={event => updateStage(stage, { input: event.target.value })} /></label>
      <label>预计输出 Token<input aria-label={`${stageLabels[stage]}预计输出 Token`} type="number" min="0" max="10000000" step="1" placeholder="未知" value={row.output} onChange={event => updateStage(stage, { output: event.target.value })} /></label>
    </div> })}
    <h3>模型价格与阶段评分</h3>
    {models.filter(model => model.enabled).map(model => { const row = draft.models[model.id] ?? emptyModelEvidence(); return <details className="evidence-model" key={model.id}>
      <summary>{model.name} · {model.model_name}</summary>
      <div className="evidence-row">
        <label>输入单价 / 百万 Token<input type="number" min="0" step="any" placeholder="未知" value={row.input} onChange={event => updateModel(model.id, { input: event.target.value })} /></label>
        <label>输出单价 / 百万 Token<input type="number" min="0" step="any" placeholder="未知" value={row.output} onChange={event => updateModel(model.id, { output: event.target.value })} /></label>
        <label>币种<input placeholder="如 CNY" maxLength={3} value={row.currency} onChange={event => updateModel(model.id, { currency: event.target.value.toUpperCase() })} /></label>
        <label>价格来源<input placeholder="如服务商报价日期" maxLength={200} value={row.source} onChange={event => updateModel(model.id, { source: event.target.value })} /></label>
      </div>
      <div className="evidence-row">{stages.map(stage => <label key={stage}>{stageLabels[stage]}评分<input type="number" min="0" max="100" step="any" placeholder="未知" value={row.scores[stage] ?? ''} onChange={event => updateModel(model.id, { scores: { ...row.scores, [stage]: event.target.value } })} /></label>)}</div>
    </details> })}
    {error && <p className="evidence-error" role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <Button size="small" onClick={apply}>保存填写并刷新推荐</Button>
  </details>
}
