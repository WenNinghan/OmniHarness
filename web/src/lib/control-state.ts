import type { CommandType, RunEvent, RunStatus } from './types.ts'

export interface ControlAvailability {
  pause: boolean
  resume: boolean
  append: boolean
}

const APPENDABLE: ReadonlySet<RunStatus> = new Set<RunStatus>([
  'queued', 'running', 'pause_requested', 'paused', 'awaiting_approval',
])

export function availableCommands(status: RunStatus | undefined): ControlAvailability {
  if (!status) return { pause: false, resume: false, append: false }
  return {
    pause: status === 'queued' || status === 'running' || status === 'awaiting_approval',
    resume: status === 'paused' || status === 'pause_requested',
    append: APPENDABLE.has(status),
  }
}

export function canStopRun(status: RunStatus | undefined): boolean {
  return status === 'queued' || status === 'running' || status === 'pause_requested' || status === 'paused' || status === 'pending_review' || status === 'awaiting_approval' || status === 'awaiting_user'
}

export const commandLabels: Record<string, string> = {
  pause: '暂停', resume: '继续', append_requirement: '补充要求',
}

export interface CommandOutcome { status: string; reason: string | null; delivery?: string }

// GET /commands returns the status but not the reason; the reason lives on the
// matching SSE run.command event and is joined by command_id.
export function commandOutcome(events: RunEvent[], commandId: string): CommandOutcome {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.event_type !== 'run.command' || event.payload.command_id !== commandId) continue
    return {
      status: typeof event.payload.result === 'string' ? event.payload.result : 'pending',
      reason: typeof event.payload.reason === 'string' ? event.payload.reason : null,
      ...(typeof event.payload.delivery === 'string' ? { delivery: event.payload.delivery } : {}),
    }
  }
  return { status: 'pending', reason: null }
}

const reasonLabels: Record<string, string> = {
  CHECKPOINT_REQUIRED: '已执行过的任务需要检查点恢复，当前不会重放',
  INAPPLICABLE_STATE: '当前状态不适用该命令',
}

export function outcomeLabel(status: string, reason: string | null, commandType?: CommandType, delivery?: string): string {
  if (status === 'applied') return commandType === 'append_requirement' ? delivery === 'prepared_for_model' ? '已准备纳入模型输入' : '已记录（尚未传给模型）' : '已生效'
  if (status === 'rejected') return reason ? `已拒绝 · ${reasonLabels[reason] ?? reason}` : '已拒绝'
  return '待执行'
}
