import test from 'node:test'
import assert from 'node:assert/strict'
import { availableCommands, canStopRun, commandOutcome, outcomeLabel } from './control-state.ts'
import type { RunEvent } from './types.ts'

test('paused runs offer resume and requirements but not another pause', () => {
  assert.deepEqual(availableCommands('paused'), { pause: false, resume: true, append: true })
  assert.deepEqual(availableCommands('pause_requested'), { pause: false, resume: true, append: true })
  assert.deepEqual(availableCommands('running'), { pause: true, resume: false, append: true })
  assert.deepEqual(availableCommands('queued'), { pause: true, resume: false, append: true })
  assert.deepEqual(availableCommands('awaiting_approval'), { pause: true, resume: false, append: true })
  assert.deepEqual(availableCommands('awaiting_user'), { pause: false, resume: false, append: false })
  assert.equal(canStopRun('awaiting_user'), true)
  assert.equal(canStopRun('awaiting_approval'), true)
  assert.deepEqual(availableCommands('completed'), { pause: false, resume: false, append: false })
  assert.deepEqual(availableCommands(undefined), { pause: false, resume: false, append: false })
})

test('runs can be stopped while queued, running, pausing or paused', () => {
  for (const status of ['queued', 'running', 'pause_requested', 'paused', 'pending_review'] as const) assert.equal(canStopRun(status), true)
  for (const status of ['cancel_requested', 'completed', 'cancelled', 'failed', 'interrupted'] as const) assert.equal(canStopRun(status), false)
  assert.equal(canStopRun(undefined), false)
})

const event = (sequence: number, payload: Record<string, unknown>): RunEvent => ({
  schema_version: 1 as const, event_id: `e${sequence}`, run_id: 'r1', sequence_number: sequence,
  event_type: 'run.command', created_at: '', payload,
})

test('a rejection reason is joined from the matching SSE event', () => {
  const events = [
    event(1, { command_id: 'c1', result: 'applied', reason: null }),
    event(2, { command_id: 'c2', result: 'rejected', reason: 'CHECKPOINT_REQUIRED' }),
  ]
  assert.deepEqual(commandOutcome(events, 'c2'), { status: 'rejected', reason: 'CHECKPOINT_REQUIRED' })
  assert.deepEqual(commandOutcome(events, 'c1'), { status: 'applied', reason: null })
  assert.deepEqual(commandOutcome(events, 'missing'), { status: 'pending', reason: null })
})

test('checkpoint rejection is explained in readable Chinese, not hidden', () => {
  assert.equal(outcomeLabel('rejected', 'CHECKPOINT_REQUIRED'), '已拒绝 · 已执行过的任务需要检查点恢复，当前不会重放')
  assert.equal(outcomeLabel('rejected', 'UNKNOWN_CODE'), '已拒绝 · UNKNOWN_CODE')
  assert.equal(outcomeLabel('rejected', null), '已拒绝')
  assert.equal(outcomeLabel('applied', null), '已生效')
  assert.equal(outcomeLabel('applied', null, 'pause'), '已生效')
  assert.equal(outcomeLabel('applied', null, 'append_requirement'), '已记录（尚未传给模型）')
  assert.equal(outcomeLabel('applied', null, 'append_requirement', 'prepared_for_model'), '已准备纳入模型输入')
  assert.equal(outcomeLabel('pending', null, 'append_requirement'), '待执行')
  assert.equal(outcomeLabel('rejected', 'INAPPLICABLE_STATE', 'append_requirement'), '已拒绝 · 当前状态不适用该命令')
  assert.equal(outcomeLabel('pending', null), '待执行')
})
