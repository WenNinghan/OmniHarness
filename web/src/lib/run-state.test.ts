import test from 'node:test'
import assert from 'node:assert/strict'
import { acceptsSnapshot, applyEvent, emptyStream, mergeMessages, mergeRunSnapshot } from './run-state.ts'
import type { Message, Run, RunEvent } from './types.ts'

const event = (sequence: number, delta: string): RunEvent => ({ schema_version: 1, event_id: `e${sequence}`, run_id: 'r1', sequence_number: sequence, event_type: 'message.delta', created_at: '', payload: { message_id: 'a1', delta } })
const history: Message[] = [{ id: 'a1', conversation_id: 'c1', run_id: 'r1', role: 'assistant', content: '你好', status: 'streaming', created_at: '' }]
test('refresh replay does not duplicate saved assistant text and can continue it', () => {
  let state = applyEvent(emptyStream(), event(1, '你'))
  assert.equal(mergeMessages(history, state, 'c1', 'r1')[0].content, '你好')
  state = applyEvent(state, event(2, '好'))
  state = applyEvent(state, event(3, '！'))
  assert.equal(mergeMessages(history, state, 'c1', 'r1')[0].content, '你好！')
})
test('reconnect duplicates and out-of-order delivery produce one ordered response', () => {
  let state = applyEvent(emptyStream(), event(2, 'B'))
  state = applyEvent(state, event(1, 'A'))
  state = applyEvent(state, event(2, 'B'))
  assert.equal(state.text.a1, 'AB')
  assert.equal(state.events.length, 2)
})
test('a different assistant message is not merged into previous run history', () => {
  const next = { ...event(1, '新回答'), payload: { message_id: 'a2', delta: '新回答' } }
  const result = mergeMessages(history, applyEvent(emptyStream(), next), 'c1', 'r2')
  assert.equal(result.length, 2)
  assert.deepEqual(result.map(item => item.content), ['你好', '新回答'])
})

test('reasoning and answer replay remain separate after refresh and out-of-order delivery', () => {
  const thought = { ...event(1, '先分析'), event_type: 'message.reasoning_delta' }
  let state = applyEvent(emptyStream(), event(2, '答案'))
  state = applyEvent(state, thought)
  state = applyEvent(state, thought)
  const saved: Message[] = [{ ...history[0], content: '答案', reasoning_content: '先分析' }]
  assert.equal(mergeMessages(saved, state, 'c1', 'r1')[0].content, '答案')
  assert.equal(mergeMessages(saved, state, 'c1', 'r1')[0].reasoning_content, '先分析')
  state = applyEvent(state, { ...event(3, '完成'), event_type: 'message.reasoning_delta' })
  assert.equal(mergeMessages(saved, state, 'c1', 'r1')[0].reasoning_content, '先分析完成')
  assert.equal(state.events.length, 3)
})

test('reasoning-only stream creates a visible message and preserves its terminal error', () => {
  let state = applyEvent(emptyStream(), { ...event(1, '思考内容'), event_type: 'message.reasoning_delta' })
  state = applyEvent(state, { ...event(2, ''), event_type: 'message.output_state', payload: { message_id: 'a1', output_metadata: { finish_reason: 'length', output_status: 'truncated' } } })
  state = applyEvent(state, { ...event(3, ''), event_type: 'run.failed', payload: { message_id: 'a1', code: 'OUTPUT_TRUNCATED', message: '回答未完成' } })
  const message = mergeMessages([], state, 'c1', 'r1')[0]
  assert.equal(message.content, '')
  assert.equal(message.reasoning_content, '思考内容')
  assert.equal(message.status, 'failed')
  assert.equal(message.error_message, '回答未完成')
  assert.equal(message.output_metadata?.finish_reason, 'length')
  const replayed = mergeMessages([message], state, 'c1', 'r1')[0]
  assert.equal(replayed.reasoning_content, '思考内容')
})

test('command conflict metadata survives terminal events and historical refresh', () => {
  const audit = { status: 'conflict', conflicts: [{ artifact_id: '61b78660-6971-4d7f-99d4-c3d7c975d0b3',
    operation_id: 'a'.repeat(64), claimed_exit_code: 0, actual_exit_code: 1, line: 7 }], truncated: false }
  let state = applyEvent(emptyStream(), event(1, '结果'))
  state = applyEvent(state, { ...event(2, ''), event_type: 'message.output_state',
    payload: { message_id: 'a1', output_metadata: { command_result_audit: audit } } })
  assert.deepEqual(mergeMessages([], state, 'c1', 'r1')[0].output_metadata?.command_result_audit, audit)
  state = applyEvent(state, { ...event(3, ''), event_type: 'run.completed',
    payload: { message_id: 'a1', output_metadata: { output_status: 'complete', command_result_audit: audit } } })
  const live = mergeMessages([], state, 'c1', 'r1')[0]
  assert.deepEqual(live.output_metadata?.command_result_audit, audit)
  assert.equal(live.status, 'completed')
  const saved: Message = { ...live, content: '结果', status: 'completed' }
  assert.deepEqual(mergeMessages([saved], emptyStream(), 'c1', 'r1')[0].output_metadata?.command_result_audit, audit)
  assert.deepEqual(mergeMessages([saved], state, 'c1', 'r1')[0].output_metadata?.command_result_audit, audit)
})

test('incomplete command audit survives streamed completion, replay and historical refresh', () => {
  const audit = { status: 'incomplete', conflicts: [], truncated: true }
  const terminal = { ...event(3, ''), event_type: 'run.completed',
    payload: { message_id: 'a1', output_metadata: { output_status: 'complete', command_result_audit: audit } } }
  let state = applyEvent(emptyStream(), event(1, '结果'))
  state = applyEvent(state, { ...event(2, ''), event_type: 'message.output_state',
    payload: { message_id: 'a1', output_metadata: { output_status: 'streaming' } } })
  state = applyEvent(state, terminal)
  state = applyEvent(state, terminal)
  const live = mergeMessages([], state, 'c1', 'r1')[0]
  assert.equal(live.content, '结果')
  assert.equal(live.status, 'completed')
  assert.deepEqual(live.output_metadata?.command_result_audit, audit)
  const saved: Message = { ...live, content: '结果', status: 'completed' }
  const refreshed = mergeMessages([saved], emptyStream(), 'c1', 'r1')[0]
  const replayed = mergeMessages([saved], state, 'c1', 'r1')[0]
  assert.equal(refreshed.content, '结果')
  assert.equal(replayed.content, '结果')
  assert.deepEqual(refreshed.output_metadata?.command_result_audit, audit)
  assert.deepEqual(replayed.output_metadata?.command_result_audit, audit)
  assert.equal(state.events.length, 3)
})

test('unknown and absent command audit metadata stay unknown after completion and reload', () => {
  for (const audit of [undefined, { status: 'unknown' }]) {
    const metadata = audit === undefined ? { output_status: 'complete' } : { output_status: 'complete', command_result_audit: audit }
    let state = applyEvent(emptyStream(), event(1, '原文'))
    state = applyEvent(state, { ...event(2, ''), event_type: 'run.completed',
      payload: { message_id: 'a1', output_metadata: metadata } })
    const live = mergeMessages([], state, 'c1', 'r1')[0]
    assert.deepEqual(live.output_metadata?.command_result_audit, audit)
    assert.equal(live.content, '原文')
    const refreshed = mergeMessages([live], emptyStream(), 'c1', 'r1')[0]
    assert.deepEqual(refreshed.output_metadata?.command_result_audit, audit)
  }
})

test('a historical failed answer retains its error when a new run streams', () => {
  const failed: Message = { ...history[0], content: '', status: 'failed', error_code: 'EMPTY_RESPONSE', error_message: '未收到回答' }
  const state = applyEvent(emptyStream(), { ...event(1, '新回答'), run_id: 'r2', payload: { message_id: 'a2', delta: '新回答' } })
  const messages = mergeMessages([failed], state, 'c1', 'r2')
  assert.equal(messages[0].error_code, 'EMPTY_RESPONSE')
  assert.equal(messages[0].status, 'failed')
  assert.equal(messages[1].content, '新回答')
})

const run = (id: string, status: Run['status']): Run => ({
  id, conversation_id: 'c1', model_config_id: 'm1', model_label: 'Mock', status, mode: 'mock',
  error_code: null, error_message: null, usage: null, created_at: '', started_at: null, finished_at: null,
})
test('a late cancellation or reconciliation response cannot reverse a terminal run', () => {
  const cancelled = run('r1', 'cancelled')
  assert.equal(mergeRunSnapshot(cancelled, run('r1', 'cancel_requested')), cancelled)
  assert.equal(mergeRunSnapshot(cancelled, run('r1', 'paused')), cancelled)
  const latest = { ...cancelled, finished_at: 'now' }
  assert.equal(mergeRunSnapshot(cancelled, latest), latest)
  const next = run('r2', 'running')
  assert.equal(mergeRunSnapshot(next, cancelled), next)
  assert.equal(mergeRunSnapshot(null, cancelled), null)
})

test('late snapshots cannot overwrite a newer request, conversation, or run', () => {
  assert.equal(acceptsSnapshot(2, 2, 'c1', 'c1', 'r1', 'r1'), true)
  assert.equal(acceptsSnapshot(1, 2, 'c1', 'c1', 'r1', 'r1'), false)
  assert.equal(acceptsSnapshot(2, 2, 'c2', 'c1', 'r1', 'r1'), false)
  assert.equal(acceptsSnapshot(2, 2, 'c1', 'c1', 'r2', 'r1'), false)
  assert.equal(acceptsSnapshot(2, 2, null, 'c1', undefined, 'r1'), false)
})

test('out-of-order paused and cancelled snapshots keep the terminal result and latest history', async () => {
  let current = run('r1', 'running')
  let history = ''
  let version = 0
  let resolvePaused!: (value: { run: Run; text: string }) => void
  let resolveCancelled!: (value: { run: Run; text: string }) => void
  const paused = new Promise<{ run: Run; text: string }>(resolve => { resolvePaused = resolve })
  const cancelled = new Promise<{ run: Run; text: string }>(resolve => { resolveCancelled = resolve })
  async function reconcile(response: Promise<{ run: Run; text: string }>) {
    const requestVersion = ++version
    const result = await response
    if (!acceptsSnapshot(requestVersion, version, 'c1', 'c1', current.id, 'r1')) return
    current = mergeRunSnapshot(current, result.run)!
    history = result.text
  }
  const oldRequest = reconcile(paused)
  const terminalRequest = reconcile(cancelled)
  resolveCancelled({ run: run('r1', 'cancelled'), text: '保留的完整文本' })
  await terminalRequest
  resolvePaused({ run: run('r1', 'paused'), text: '旧文本' })
  await oldRequest
  assert.equal(current.status, 'cancelled')
  assert.equal(history, '保留的完整文本')
})

test('delayed pending commands and budget cannot replace a newer applied snapshot', async () => {
  let version = 0
  let displayed = { command: '', budget: 0 }
  let resolvePending!: (value: { command: string; budget: number }) => void
  let resolveApplied!: (value: { command: string; budget: number }) => void
  const pending = new Promise<{ command: string; budget: number }>(resolve => { resolvePending = resolve })
  const applied = new Promise<{ command: string; budget: number }>(resolve => { resolveApplied = resolve })
  async function refresh(response: Promise<{ command: string; budget: number }>) {
    const requestVersion = ++version
    const result = await response
    if (acceptsSnapshot(requestVersion, version, 'c1', 'c1', 'r1', 'r1')) displayed = result
  }
  const older = refresh(pending)
  const newer = refresh(applied)
  resolveApplied({ command: 'applied', budget: 1 })
  await newer
  resolvePending({ command: 'pending', budget: 0 })
  await older
  assert.deepEqual(displayed, { command: 'applied', budget: 1 })
})
