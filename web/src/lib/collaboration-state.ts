import type { AgentStrategy, Stage } from './types.ts'

export const strategyLabels: Record<AgentStrategy, string> = {
  single: '单 Agent', compact: '精简协作', staged: '完整五阶段',
}
export const strategyStages: Record<AgentStrategy, readonly Stage[]> = {
  single: ['execute'], compact: ['plan', 'execute', 'review'],
  staged: ['think', 'plan', 'dispatch', 'execute', 'review'],
}
export function strategyFromStorage(value: string | null): AgentStrategy {
  return value === 'single' || value === 'compact' ? value : 'staged'
}
