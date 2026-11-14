import test from 'node:test'
import assert from 'node:assert/strict'
import { CLARIFICATION_EVENTS, answerRequest, canAnswerClarification, canAnswerInStatus, clarificationPendingNotice, historicalClarificationRun, isClarificationRefreshEvent, supportsClarifications } from './clarification-state.ts'
import type { Clarification, Run } from './types.ts'

test('only new frozen versions request the clarification API', () => {
  assert.equal(supportsClarifications('stage-agent-v7'), true)
  assert.equal(supportsClarifications('stage-agent-v8'), true)
  assert.equal(supportsClarifications('stage-agent-v9'), true)
  assert.equal(supportsClarifications('stage-agent-v10'), true)
  assert.equal(supportsClarifications('stage-agent-interactive-v1'), true)
  assert.equal(supportsClarifications('stage-agent-v6'), false)
  assert.equal(supportsClarifications('stage-agent-v11'), false)
  assert.equal(supportsClarifications(undefined), false)
})

test('only a pending question of the active waiting run accepts an answer', () => {
  const run = { id: 'run-a', status: 'awaiting_user' } as Run
  const question = { run_id: 'run-a', status: 'pending' } as Clarification
  assert.equal(canAnswerClarification(run, question), true)
  assert.equal(canAnswerClarification({ ...run, status: 'awaiting_approval' }, question), true)
  assert.equal(canAnswerInStatus('awaiting_approval'), true)
  assert.equal(canAnswerClarification({ ...run, id: 'run-b' }, question), false)
  assert.equal(canAnswerClarification({ ...run, status: 'cancelled' }, question), false)
  assert.equal(canAnswerClarification(run, { ...question, status: 'answered' }), false)
})

test('same answer retries reuse its idempotency key, changed answer gets a new one', () => {
  let counter = 0
  const createId = () => String(++counter)
  const first = answerRequest('A', null, createId)
  const retry = answerRequest('A', first, createId)
  const revised = answerRequest('B', retry, createId)
  assert.deepEqual([first.id, retry.id, revised.id], ['1', '1', '2'])
})

test('waiting transition is subscribed and triggers a fresh run and question snapshot', () => {
  for (const event of ['clarification.requested', 'clarification.answered', 'run.awaiting_user']) {
    assert.equal(CLARIFICATION_EVENTS.includes(event as typeof CLARIFICATION_EVENTS[number]), true)
    assert.equal(isClarificationRefreshEvent(event), true)
  }
  assert.equal(isClarificationRefreshEvent('agent.model_started'), false)
})

test('questions visible before interrupt show preparation instead of a closed-task claim', () => {
  assert.match(clarificationPendingNotice('running'), /准备等待/)
  assert.match(clarificationPendingNotice('queued'), /准备等待/)
  assert.match(clarificationPendingNotice('cancelled'), /不再等待/)
})

test('historical lookup uses only an identified supported run, never the active run', () => {
  const old = { id: 'old', workflow_version: 'stage-agent-v7' } as Run
  const legacy = { id: 'legacy', workflow_version: 'stage-agent-v6' } as Run
  const runs = [old, legacy]
  assert.equal(historicalClarificationRun(runs, 'old', 'current'), old)
  assert.equal(historicalClarificationRun(runs, 'old', 'old'), undefined)
  assert.equal(historicalClarificationRun(runs, 'legacy', 'current'), undefined)
  assert.equal(historicalClarificationRun(runs, 'missing', 'current'), undefined)
})
