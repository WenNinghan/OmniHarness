import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowRight, Check, CircleAlert, GitBranch, Info, LoaderCircle, RotateCcw, Sparkles, X } from 'lucide-react'
import { api } from '../lib/api.ts'
import { prepareEvidence } from '../lib/routing-evidence.ts'
import type { AgentStrategy, AIRoutingPreview, ModelConfig, PendingRoutingPlan, Stage } from '../lib/types.ts'
import { stageDescriptions, stageLabels, stages } from '../lib/types.ts'
import { Button } from './ui.tsx'
import { readRoutingEvidence } from './RoutingEvidence.tsx'
import { ModelPicker } from './ModelPicker.tsx'
import './ModelRoutingPlanner.css'
import { strategyStages, strategyLabels } from '../lib/collaboration-state.ts'

type Props = {
  strategy?: AgentStrategy; task: string; conversationKey: string; models: ModelConfig[]; defaultModelId: string
  plan: PendingRoutingPlan | null; active: boolean; agentAvailable: boolean
  onChange: (plan: PendingRoutingPlan | null) => void
  onApply: () => void; onClose: () => void
}

function isRoutingCandidate(model: ModelConfig) {
  if (!model.enabled || model.adapter_type === 'mock') return false
  if (model.key_configured) return true
  if (model.adapter_type !== 'openai-compatible' || !model.base_url) return false
  try { return ['localhost', '127.0.0.1', '[::1]', 'host.docker.internal'].includes(new URL(model.base_url).hostname) }
  catch { return false }
}

/** Only explicit clicks make a model request. The preview endpoint is read-only. */
export function ModelRoutingPlanner({ strategy = 'staged', task, conversationKey, models, defaultModelId, plan, active, agentAvailable, onChange, onApply, onClose }: Props) {
  const [plannerId, setPlannerId] = useState(defaultModelId)
  const [preview, setPreview] = useState<AIRoutingPreview | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const enabled = useMemo(() => models.filter(isRoutingCandidate), [models])
  const planner = enabled.find(item => item.id === plannerId)
  const revision = enabled.map(item => `${item.id}:${item.version}:${item.provider_version ?? '?'}`).sort().join('|')
  const current = useRef({ conversationKey, task, plannerId, revision })
  current.current = { conversationKey, task, plannerId, revision }
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  useEffect(() => {
    let cancelled = false
    setPreview(null); setPreviewError('')
    if (!plannerId || !planner) return
    void api.aiPlanPreview(plannerId).then(result => { if (!cancelled) setPreview(result) }).catch(reason => {
      if (!cancelled) setPreviewError(reason instanceof Error ? reason.message : '无法读取请求地址。')
    })
    return () => { cancelled = true }
  }, [plannerId, planner?.version, planner?.provider_version])

  const taskText = task.trim()
  const accepted = Boolean(plan && plan.acceptedTask === taskText && taskText)
  const taskChanged = Boolean(plan && plan.task !== taskText)
  const selectionsAvailable = Boolean(plan && plan.stages.every(stage => {
    const model = enabled.find(item => item.id === stage.model_config_id)
    return model && model.version === stage.model_version && model.provider_version === stage.provider_version
  }))

  async function generate() {
    if (!preview || !planner || !taskText || busy) return
    const captured = { ...current.current }
    setBusy(true); setError('')
    try {
      let assessments: ReturnType<typeof prepareEvidence>['assessments'] = []
      try { assessments = prepareEvidence(readRoutingEvidence(), enabled).assessments } catch { /* Partial entries remain unknown. */ }
      const result = await api.aiPlan({ task: taskText, planner_model_config_id: plannerId,
        planner_model_version: preview.planner.model_version, planner_provider_version: preview.planner.provider_version,
        candidate_model_config_ids: enabled.map(item => item.id), ...(assessments.length ? { assessments } : {}) })
      const fresh = current.current
      if (!mounted.current || captured.conversationKey !== fresh.conversationKey || captured.task !== fresh.task ||
          captured.plannerId !== fresh.plannerId || captured.revision !== fresh.revision) {
        if (mounted.current) setError('对话、问题或模型配置已改变，刚生成的草案未应用，请重新规划。')
        return
      }
      if (result.stages.length !== stages.length || new Set(result.stages.map(item => item.stage)).size !== stages.length ||
          result.stages.some(item => !stages.includes(item.stage))) throw new Error('规划结果缺少必要阶段，请重试。')
      onChange({ task: taskText, stages: result.stages, planner: result.planner, warnings: result.warnings, usage: result.usage, acceptedTask: null })
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : '规划失败，原配置未改变。')
    } finally { if (mounted.current) setBusy(false) }
  }

  function updateStage(stage: Stage, id: string) {
    if (!plan) return
    const model = enabled.find(item => item.id === id)
    if (!model || model.provider_version === undefined) return
    const providerVersion = model.provider_version
    onChange({ ...plan, acceptedTask: null, stages: plan.stages.map(item => item.stage === stage
      ? { ...item, model_config_id: model.id, model_version: model.version, provider_version: providerVersion,
          reason: '已由你手动调整。' } : item) })
  }

  return <>
    <button type="button" className="routing-backdrop" aria-label="关闭模型规划" onClick={onClose} />
    <aside className="routing-panel" aria-label="AI 模型规划">
      <header className="routing-panel-header"><div className="routing-panel-title"><span className="routing-logo"><GitBranch size={18} /></span>
        <div><span className="routing-kicker">MODEL ROUTING</span><h2>AI 模型规划</h2></div></div>
        <button className="routing-close" type="button" aria-label="关闭模型规划" onClick={onClose}><X size={18} /></button>
      </header>
      <div className="routing-panel-content">
        <div className="routing-intro"><span className="routing-intro-icon"><Sparkles size={18} /></span><div><h3>为当前问题选择合适的分工</h3><p>先生成建议，再由你决定各阶段使用的模型。</p></div></div>
        {taskText && <div className="routing-task"><span>当前问题</span><p>{taskText}</p></div>}
        <section className="routing-section" aria-label="规划模型配置"><div className="routing-section-heading"><span>01</span><h3>规划设置</h3></div>
          <label className="routing-field-label" htmlFor="routing-planner-model">使用哪个模型生成方案</label>
          <ModelPicker id="routing-planner-model" label="选择规划模型" models={enabled} value={plannerId} onChange={setPlannerId} disabled={busy} />
          <div className="routing-request"><div><span>请求地址</span><code>{preview?.planner.endpoint ?? (previewError ? '无法确定' : planner ? '正在读取…' : '请选择规划模型')}</code></div>
            <div><span>发送内容</span><p>当前问题、候选模型及已填写的评分和价格；不发送对话历史。密钥仅用于向该服务认证，不放入问题内容。</p></div>
            <div><span>额外调用</span><p>生成方案单独调用一次规划模型，不计入后续任务预算；实际费用未知。</p></div></div>
          {previewError && <p className="routing-error" role="alert"><CircleAlert size={15} />{previewError}</p>}
          {enabled.length === 1 && <p className="routing-note"><Info size={14} />当前只有一个可用于规划的模型，无法比较不同模型。可先在“模型连接”中添加。</p>}
          {enabled.length < models.filter(item => item.enabled).length && <p className="routing-note"><Info size={14} />Mock 模型和缺少密钥的远程连接不会作为规划候选。</p>}
          {!taskText && <p className="routing-note"><Info size={14} />请先在输入框填写要处理的问题。</p>}
          <Button className="routing-generate" variant="primary" type="button" busy={busy} disabled={!preview || !planner || !taskText || !enabled.length} onClick={() => void generate()}>
            {!busy && <Sparkles size={16} />}{busy ? '正在规划…' : plan ? '重新生成规划' : '生成规划'}<ArrowRight size={15} />
          </Button>
          {error && <p className="routing-error" role="alert"><CircleAlert size={15} />{error}</p>}
        </section>
        <section className="routing-section routing-results" aria-label="阶段模型方案"><div className="routing-section-heading"><span>02</span><h3>阶段模型方案</h3>{plan && <span className="routing-status">{accepted ? '已应用' : '待确认'}</span>}</div>
          {!plan ? <div className="routing-empty"><span><GitBranch size={20} /></span><strong>方案会显示在这里</strong><p>思考、规划、任务分配、执行和审查都可以单独调整。</p></div> : <>
            <p className="routing-subtitle">当前：{strategyLabels[strategy]}。仅下列阶段会调用模型，其他绑定保留。</p><p className="routing-subtitle">由 {plan.planner.model_name} 生成 · 每个阶段均可修改</p>
            <p className="routing-subtitle">规划调用用量：{plan.usage.total_tokens == null ? '未知' : `${plan.usage.total_tokens} tokens`}；费用未知，与任务预算分开。</p>
            {taskChanged && <p className="routing-note"><Info size={14} />当前问题与生成方案时不同；你可以重新规划，或确认沿用此方案。</p>}
            {!selectionsAvailable && <p className="routing-error"><CircleAlert size={15} />方案中的模型已停用或配置发生变化，请重新选择。</p>}
            <div className="routing-stage-list">{strategyStages[strategy].map((stage, index) => {
              const item = plan.stages.find(entry => entry.stage === stage)
              if (!item) return null
              return <article className="routing-stage" key={stage}><div className="routing-stage-top"><span className="routing-stage-number">{String(index + 1).padStart(2, '0')}</span><div><strong>{stageLabels[stage]}</strong><small>{stageDescriptions[stage]}</small></div></div>
                <ModelPicker label={`${stageLabels[stage]}阶段模型`} models={enabled} value={item.model_config_id} stage={stage} onChange={id => updateStage(stage, id)} /><p>{item.reason}</p></article>
            })}</div>
            <div className="routing-disclaimer"><Info size={14} /><p>以上是模型建议，不代表能力已实测；未提供的价格和费用保持未知。</p></div>
            {plan.warnings.map((warning, index) => <p className="routing-note" key={index}><Info size={14} />{warning}</p>)}
            <div className="routing-actions"><Button type="button" variant="primary" disabled={!taskText || !selectionsAvailable || !agentAvailable} onClick={onApply}><Check size={15} />{accepted ? '已用于当前问题' : taskChanged ? '沿用方案到当前问题' : active ? '用于下一次任务' : '用于本次任务'}</Button>
              <Button type="button" variant="ghost" onClick={() => onChange(null)}><RotateCcw size={15} />放弃</Button></div>
            {!agentAvailable && <p className="routing-note"><CircleAlert size={14} />Agent 当前不可用，方案暂不能用于任务。</p>}
            <p className="routing-footnote">{active ? '正在运行的任务不会改变；方案只应用于下一次任务。' : '只应用于你确认的任务，不修改全局阶段配置。'}</p>
          </>}
        </section>
      </div>
    </aside>
  </>
}
