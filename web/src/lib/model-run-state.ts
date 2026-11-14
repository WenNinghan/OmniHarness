import type { Run } from './types.ts'

/** The run label and ID are frozen at submission; current catalog names may change later. */
export function displayedRunModel(active: boolean, run: Run | null, draftId: string): {
  id: string; frozenLabel?: string; frozenSubtitle?: string; stageNote: boolean
} {
  if (!active) return { id: draftId, stageNote: false }
  const id = typeof run?.model_config_id === 'string' ? run.model_config_id : ''
  const label = typeof run?.model_label === 'string' && run.model_label.trim()
    ? run.model_label.trim() : id ? `模型名称未记录（ID：${id}）` : '本次任务模型未记录'
  const stageNote = run?.workflow_type === 'agent' && run.agent_strategy !== 'single'
  return { id, frozenLabel: label,
    frozenSubtitle: stageNote ? '任务默认模型 · 各阶段可能使用其他模型' : '本次任务默认模型 · 已冻结', stageNote }
}
