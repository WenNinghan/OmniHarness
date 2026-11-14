import type { Clarification, Run, RunStatus } from './types.ts'

export const CLARIFICATION_EVENTS = ['clarification.requested', 'clarification.answered', 'run.awaiting_user'] as const
export const CLARIFICATION_ANSWER_MAX_LENGTH = 4000

export function isClarificationRefreshEvent(event: string): boolean {
  return (CLARIFICATION_EVENTS as readonly string[]).includes(event)
}

export function clarificationPendingNotice(status: RunStatus): string {
  if (status === 'running' || status === 'queued' || status === 'pause_requested') return '问题已提出，任务正在准备等待，请稍候。'
  if (status === 'paused') return '任务已暂停，继续任务后可回答。'
  return '当前任务不再等待回答，请刷新查看最新状态。'
}

export function supportsClarifications(version: string | null | undefined): boolean {
  return version === 'stage-agent-interactive-v1' || version === 'stage-agent-v7' || version === 'stage-agent-v8' || version === 'stage-agent-v9' || version === 'stage-agent-v10'
}

export function historicalClarificationRun(runs: Run[], runId: string | null, currentRunId: string | undefined): Run | undefined {
  if (!runId || runId === currentRunId) return undefined
  const match = runs.find(run => run.id === runId)
  return match && supportsClarifications(match.workflow_version) ? match : undefined
}

export function canAnswerClarification(run: Run | null, item: Clarification): boolean {
  return run?.id === item.run_id && canAnswerInStatus(run.status) && item.status === 'pending'
}

export function canAnswerInStatus(status: RunStatus): boolean {
  return status === 'awaiting_user' || status === 'awaiting_approval'
}

export function answerRequest(value: string, previous: { value: string; id: string } | null,
  createId: () => string): { value: string; id: string } {
  return previous?.value === value ? previous : { value, id: createId() }
}
