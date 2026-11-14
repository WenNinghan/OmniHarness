import { useEffect, useMemo, useState } from 'react'
import { GitBranch, Info, Layers3, LoaderCircle, Save, SlidersHorizontal } from 'lucide-react'
import { api } from '../lib/api.ts'
import type { ModelConfig, RoutingRecommendation, Stage, StageBindings } from '../lib/types.ts'
import { stages, stageDescriptions, stageLabels } from '../lib/types.ts'
import { Badge, Button } from './ui.tsx'
import { readRoutingEvidence, RoutingEvidence } from './RoutingEvidence.tsx'
import { prepareEvidence } from '../lib/routing-evidence.ts'
import { modelDisplayName } from '../lib/model-picker.ts'
import { ModelPicker } from './ModelPicker.tsx'

const emptyDraft = (): Record<Stage, string> =>
  Object.fromEntries(stages.map(stage => [stage, ''])) as Record<Stage, string>

export function Orchestration({ models, defaultModelId, notify }: {
  models: ModelConfig[]
  defaultModelId: string
  notify: (text: string, error?: boolean) => void
}) {
  const [data, setData] = useState<StageBindings | null>(null)
  const [draft, setDraft] = useState<Record<Stage, string>>(emptyDraft)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [recommendation, setRecommendation] = useState<RoutingRecommendation | null>(null)
  const [recommendationError, setRecommendationError] = useState('')
  const [recommending, setRecommending] = useState(false)
  const [initialEvidence] = useState(readRoutingEvidence)
  const [evidenceDraft, setEvidenceDraft] = useState(initialEvidence)
  const evidence = useMemo(() => {
    try { return prepareEvidence(evidenceDraft, models) } catch { return { assessments: [], stage_requirements: {} } }
  }, [evidenceDraft, models])

  async function load() {
    setLoading(true)
    try {
      const result = await api.stageBindings()
      setData(result)
      setDraft(Object.fromEntries(stages.map(stage => [stage, result.bindings.find(item => item.stage === stage)?.model_config_id ?? ''])) as Record<Stage, string>)
    } catch (reason) {
      setData(null)
      notify(reason instanceof Error ? reason.message : '阶段配置加载失败', true)
    } finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [])
  const enabledModels = useMemo(() => models.filter(model => model.enabled), [models])
  const defaultModel = enabledModels.find(model => model.id === defaultModelId)
  const defaultLabel = defaultModel ? modelDisplayName(defaultModel) : '请先在对话中选择可用模型'
  useEffect(() => {
    let disposed = false
    if (!defaultModel) { setRecommendation(null); return }
    setRecommending(true); setRecommendationError('')
    api.recommendModels(defaultModel.id, enabledModels.map(model => model.id), { ...evidence, assessments: evidence.assessments.filter(item => enabledModels.some(model => model.id === item.model_config_id)) }).then(result => {
      if (!disposed) setRecommendation(result)
    }).catch(reason => {
      if (!disposed) { setRecommendation(null); setRecommendationError(reason instanceof Error ? reason.message : '推荐不可用') }
    }).finally(() => { if (!disposed) setRecommending(false) })
    return () => { disposed = true }
  }, [defaultModel?.id, enabledModels, evidence])

  const configured = useMemo(() => Object.values(draft).filter(Boolean).length, [draft])
  const dirty = Boolean(data && stages.some(stage =>
    (draft[stage] || null) !== (data.bindings.find(item => item.stage === stage)?.model_config_id ?? null)))

  async function save() {
    if (!data) return
    setSaving(true)
    try {
      const result = await api.saveStageBindings(data.version, stages.map(stage => ({ stage, model_config_id: draft[stage] || null })))
      setData(result)
      notify(`阶段配置已保存 · v${result.version}`)
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : '保存失败', true)
    } finally { setSaving(false) }
  }

  return <main className="orch-page">
    <div className="orch-heading">
      <div><span className="eyebrow">ORCHESTRATION / MODEL ROUTING</span><h1>阶段与模型</h1>
        <p>为 Agent 任务的思考、规划、分配、执行和审查分别选择模型；普通聊天直接使用对话中选择的模型。</p></div>
      <span className="orch-version">CONFIG v{data?.version ?? '—'}</span>
    </div>
    <section className="orch-advisor" aria-label="自动推荐">
      <span className="orch-advisor-icon"><GitBranch size={18} /></span>
      <div><h3>规则推荐草案</h3>
        <p>当前默认模型：{defaultLabel}。这里根据已填写的评分和价格提供规则建议；要结合具体问题进行 AI 规划，请在对话输入框旁选择“AI 规划模型”。</p>
        {recommending && <p>正在生成推荐…</p>}
        {recommendationError && <p role="alert">推荐暂不可用：{recommendationError}</p>}
        {recommendation?.limitations.map((item, index) => <p key={index}>{item}</p>)}
      </div>
      {recommendation && <Button size="small" disabled={!data} onClick={() => setDraft(old => {
        const next = { ...old }
        for (const item of recommendation.recommendations) if (stages.includes(item.stage)) next[item.stage] = item.model_config_id === defaultModel?.id ? '' : item.model_config_id
        return next
      })}>采用草案</Button>}
    </section>
    <RoutingEvidence models={models} initial={initialEvidence} onApply={setEvidenceDraft} />
    <section className="orch-section" style={{ marginTop: 28 }}>
      <div className="orch-section-head"><Layers3 size={15} /><h2>阶段配置预览</h2><span className="orch-meta">{configured} / {stages.length} 已单独指定</span></div>
      <div className="orch-flow">{stages.map((stage, index) => {
        const selected = enabledModels.find(model => model.id === draft[stage])
        return <div className={`orch-node ${selected ? 'orch-node-set' : ''}`} key={stage}>
          <span className="orch-node-index">0{index + 1}</span>
          <h3>{stageLabels[stage]}</h3><p>{stageDescriptions[stage]}</p>
          <span className="orch-node-model">{selected ? modelDisplayName(selected) : draft[stage] ? '已指定的模型不可用' : `继承默认：${defaultLabel}`}</span>
        </div>
      })}</div>
    </section>
    <section className="orch-section">
      <div className="orch-section-head"><SlidersHorizontal size={15} /><h2>阶段模型配置</h2><span className="orch-meta">修改后需保存才生效</span></div>
      {loading ? <div className="orch-loading"><LoaderCircle size={18} className="spin" />正在读取配置…</div> : !data ? <p className="orch-note">阶段配置未加载，无法保存。请重试。</p> :
        <div className="orch-grid">{stages.map(stage => {
          const suggestion = recommendation?.recommendations.find(item => item.stage === stage)
          const suggestedModel = enabledModels.find(model => model.id === suggestion?.model_config_id)
          return <article className="orch-card" key={stage}>
            <div className="orch-card-head"><div><span className="orch-card-role">{stage.toUpperCase()}</span><h3>{stageLabels[stage]}</h3></div>
              <Badge tone={draft[stage] ? 'green' : 'neutral'}>{draft[stage] ? '已指定' : '继承默认'}</Badge></div>
            <p>{stageDescriptions[stage]}</p>
            <ModelPicker label={`${stageLabels[stage]}使用的模型`} models={enabledModels} value={draft[stage]} stage={stage}
              inherit={{ modelId: defaultModelId, label: '继承默认' }}
              onChange={id => setDraft(old => ({ ...old, [stage]: id }))} />
            {suggestion && <div className="orch-card-hint">建议：{suggestedModel ? modelDisplayName(suggestedModel) : '模型不可用'}。{suggestion.reason}
              {suggestion.price && <p>基于用户估计：{suggestion.price.expected_input_tokens} 输入 Token、{suggestion.price.expected_output_tokens} 输出 Token，约 {suggestion.price.estimated_cost} {suggestion.price.currency}；价格来源：{suggestion.price.source}。不代表实际账单。</p>}
              {suggestion.evidence && <p>达到评分门槛：{suggestion.evidence.qualified_model_config_ids.length} 个；未提供完整比较依据：{suggestion.evidence.unassessed_model_config_ids.length} 个。{suggestion.evidence.comparison_currency_conflict ? '币种不同，无法比较。' : ''}</p>}
            </div>}
          </article>
        })}</div>}
      <div className="orch-toolbar">
        <span className="orch-note"><Info size={13} />配置只影响未来创建的 Agent 任务；在途任务使用启动时冻结的配置。Verifier 是检查器，不是模型阶段。</span>
        <div className="orch-actions"><Button onClick={() => void load()} disabled={saving}>重新读取</Button>
          <Button variant="primary" busy={saving} disabled={!dirty || loading} onClick={() => void save()}><Save size={14} />保存配置</Button></div>
      </div>
    </section>
  </main>
}
