import type { PendingRoutingPlan, RoutingModelSelection } from './types.ts'
import { stages } from './types.ts'

/** Save only model identities/versions, never a question, reason, credential or URL. */
export function serializeRoutingPlans(plans: Record<string, PendingRoutingPlan>): string {
  const result: Record<string, RoutingModelSelection[]> = {}
  for (const [conversation, plan] of Object.entries(plans)) {
    if (conversation === '__new__') continue
    result[conversation] = plan.stages.map(item => ({ stage: item.stage, model_config_id: item.model_config_id,
      model_version: item.model_version, provider_version: item.provider_version }))
  }
  return JSON.stringify(result)
}

export function restoreRoutingPlans(raw: string | null): Record<string, PendingRoutingPlan> {
  if (!raw) return {}
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const result: Record<string, PendingRoutingPlan> = Object.create(null)
    for (const [id, value] of Object.entries(parsed)) {
      if (!/^[\w-]{1,64}$/.test(id) || id === '__new__' || !Array.isArray(value) || value.length !== 5) continue
      const seen = new Set<string>()
      const valid = value.every(item => {
        if (!item || typeof item !== 'object' || !stages.includes(item.stage) || seen.has(item.stage) ||
            typeof item.model_config_id !== 'string' || !item.model_config_id || item.model_config_id.length > 36 ||
            !Number.isInteger(item.model_version) || item.model_version < 1 ||
            !Number.isInteger(item.provider_version) || item.provider_version < 1) return false
        seen.add(item.stage)
        return true
      })
      if (!valid) continue
      result[id] = { task: '', acceptedTask: null,
        stages: value.map(item => ({ stage: item.stage, model_config_id: item.model_config_id,
          model_version: item.model_version, provider_version: item.provider_version, reason: '已恢复上次选择，请确认后使用。' })),
        planner: { model_config_id: '', model_name: '上次规划', endpoint: '' },
        warnings: ['只恢复模型选择；问题和上次建议理由未保存在浏览器。'],
        usage: { input_tokens: null, output_tokens: null, total_tokens: null, cost: null } }
    }
    return result
  } catch { return {} }
}
