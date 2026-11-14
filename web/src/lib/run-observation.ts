import type { CacheObservation, ObservedPhase, RunObservation } from './types.ts'

export const observedPhaseLabels: Record<ObservedPhase, string> = {
  accepted: '已接收，等待执行', executing: '正在执行', verifying: '正在审查', needs_input: '等待你的处理',
  needs_reconciliation: '结果或资源需要核对', done: '流程已结束', incomplete: '流程未完成',
  stopping: '正在停止或暂停', paused: '已暂停',
}

export function cacheDescription(value: CacheObservation): string {
  if (value.source === 'provider_reported' && value.value !== null) return `${value.value.toLocaleString()} tokens（供应商返回）`
  if (value.observed !== null) return `已观测 ${value.observed.toLocaleString()} tokens，仅 ${value.reported_calls} / ${value.total_finished_calls} 次结束调用有数据，总量未知`
  return '未知（供应商未返回完整数据）'
}

export function observationIsCurrent(value: RunObservation, runId: string, previousWatermark: number): boolean {
  return value.schema_version === 1 && value.run_id === runId && Object.hasOwn(observedPhaseLabels, value.phase)
    && Number.isSafeInteger(value.event_watermark) && value.event_watermark >= previousWatermark
    && Number.isFinite(Date.parse(value.observed_at))
}

export function elapsedEvidenceTime(at: string | null, observedAt: string): string {
  if (at === null) return '尚无事件'
  const delta = Date.parse(observedAt) - Date.parse(at)
  if (!Number.isFinite(delta)) return '时间未知'
  const seconds = Math.max(0, Math.floor(delta / 1000))
  return seconds < 60 ? `${seconds} 秒前` : `${Math.floor(seconds / 60)} 分钟前`
}
