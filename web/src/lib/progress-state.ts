import type { RunEvent } from './types.ts'

const stages: Record<string, string> = { think: '思考', plan: '任务规划', dispatch: '任务分配', execute: '执行', review: '审查', synthesize: '整理答复' }
const labels: Record<string, string> = {
  'run.created': '任务已创建', 'run.started': '任务已开始',
  'agent.model_started': '模型调用开始', 'agent.model_completed': '模型调用完成',
  'agent.stage_result': '阶段结果已保存', 'agent.delegated': '子任务已委派',
  'tool.requested': '工具调用已请求', 'tool.completed': '工具调用已完成', 'tool.failed': '工具调用失败',
  'clarification.requested': '等待你的回复', 'clarification.answered': '已收到回复',
}

/** Only describe saved events; never imply unobserved progress or success. */
export function latestProgress(events: RunEvent[]): string {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i], label = labels[event.event_type]
    if (!label) continue
    const role = event.payload.stage ?? event.payload.role
    return typeof role === 'string' && stages[role] ? `${stages[role]} · ${label}` : label
  }
  return '等待运行事件'
}
