import { Activity } from 'lucide-react'
import { useEffect, useState } from 'react'
import { api } from '../lib/api.ts'
import { cacheDescription, elapsedEvidenceTime, observationIsCurrent, observedPhaseLabels } from '../lib/run-observation.ts'
import type { Run, RunObservation as Observation } from '../lib/types.ts'
import { terminalStatuses } from '../lib/types.ts'
import { Button } from './ui.tsx'

export function RunObservation({ run }: { run: Run }) {
  const [observation, setObservation] = useState<Observation | null>(null)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)

  useEffect(() => {
    let stopped = false, watermark = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    setObservation(null); setError('')
    async function load() {
      let poll = true
      try {
        const value = await api.runObservation(run.id, controller.signal)
        if (stopped) return
        if (!observationIsCurrent(value, run.id, watermark)) throw new Error('观察记录的任务或事件水位不一致。')
        watermark = value.event_watermark
        setObservation(value); setError('')
        poll = !terminalStatuses.has(value.run_status) || value.phase === 'needs_reconciliation'
      } catch (reason) {
        if (!stopped) setError(reason instanceof Error ? reason.message : '读取状态失败。')
      } finally {
        if (!stopped && poll) timer = setTimeout(() => void load(), 5000)
      }
    }
    void load()
    return () => { stopped = true; controller.abort(); if (timer) clearTimeout(timer) }
  }, [run.id, run.status, refresh])

  return <section className="detail-section">
    <h3><Activity size={14} />任务与资源状态</h3>
    <div className="control-row"><strong>{observation ? observedPhaseLabels[observation.phase] : '正在读取持久记录…'}</strong>
      <Button size="small" onClick={() => setRefresh(value => value + 1)}>刷新</Button></div>
    {error && <p className="detail-hint" role="alert">{error} 下方上次读取的状态可能已过期。</p>}
    {observation && <>
      <p className="detail-hint">读取于 {new Date(observation.observed_at).toLocaleTimeString('zh-CN', { hour12: false })}，截至事件 {observation.event_watermark}。
        最后事件：{elapsedEvidenceTime(observation.last_event_at, observation.observed_at)}。没有新事件不能单独证明任务卡死。</p>
      <p className="detail-hint">模型调用中 {observation.execution.active_model_calls} · 工具在途 {observation.execution.active_tools} · 未知工具 {observation.execution.uncertain_tools} · 未知执行记录 {observation.execution.uncertain_ledger_entries}。</p>
      <p className="detail-hint">占用工具槽 {observation.resources.tool_slots_held} · 工作区租约{observation.resources.workspace_lease_held ? '仍持有' : '未持有'}。</p>
      <p className="detail-hint">待处理输入 {observation.inputs.pending} · 已应用补充要求 {observation.inputs.applied_requirements}；应用记录不证明模型已读取或遵从。</p>
      <details className="stage-result"><summary>命令证据 · {observation.evidence.command_total} 条记录</summary>
        <p className="detail-hint">这里展示宿主记录的退出码；独立验收未在此视图记录，流程结束也不代表目标已经达成。</p>
        {observation.evidence.command_total === 0 && <p className="detail-hint">没有命令执行证据。</p>}
        {observation.evidence.command_results.map(result => <p className="detail-hint" key={result.operation_id}>
          操作 <code title={result.operation_id}>{result.operation_id.slice(0, 12)}</code> · {result.exit_code === null ? '命令结果未知' : `退出码 ${result.exit_code}`} · {result.resource_reclaimed ? '资源已回收' : '回收尚未确认'} · 日志工件 {result.log_artifact_ids.length} 个。
        </p>)}
        {observation.evidence.truncated && <p className="detail-hint">此处仅列最近 20 条；完整内容见工具记录。</p>}
      </details>
      <details className="stage-result"><summary>上下文处理与缓存</summary>
        <p className="detail-hint">输入来源记录 {observation.context.input_records} · 摘要工件 {observation.context.summary_artifacts} · 工具结果缩略 {observation.context.mask_events} 次 · 摘要降级 {observation.context.degradation_events} 次。</p>
        <p className="detail-hint">缓存读取：{cacheDescription(observation.cache.cache_read_input_tokens)}</p>
        <p className="detail-hint">缓存写入：{cacheDescription(observation.cache.cache_creation_input_tokens)}</p>
        <p className="detail-hint">摘要、输入估算和供应商缓存是不同的记录；这里不推测缓存命中率。</p>
      </details>
    </>}
  </section>
}
