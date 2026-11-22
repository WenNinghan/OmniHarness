import assert from 'node:assert/strict'
import test from 'node:test'
import { buildStageTimeline, formatDuration } from './stage-timeline.ts'
import type { AgentRun, RunEvent } from './types.ts'

function makeNode(overrides: Partial<AgentRun>): AgentRun {
  return {
    id: 'n1',
    run_id: 'r1',
    parent_agent_run_id: null,
    role: 'execute',
    status: 'completed',
    depth: 0,
    model_config_id: 'm1',
    model_name: 'demo-model',
    selection_source: 'default',
    error_code: null,
    created_at: '2026-11-23T00:00:00.000Z',
    started_at: '2026-11-23T00:00:00.000Z',
    finished_at: '2026-11-23T00:00:02.000Z',
    ...overrides,
  }
}

function makeEvent(event_type: string, payload: Record<string, unknown> = {}): RunEvent {
  return {
    schema_version: 1,
    event_id: `ev-${Math.random()}`,
    run_id: 'r1',
    sequence_number: 1,
    event_type,
    created_at: '2026-11-23T00:00:01.000Z',
    payload,
  }
}

test('formatDuration handles ms, seconds, minutes, and null without fabricating zero', () => {
  assert.equal(formatDuration(null, 'running'), '进行中')
  assert.equal(formatDuration(null, 'completed'), '未知')
  assert.equal(formatDuration(420), '420 ms')
  assert.equal(formatDuration(2500), '2.5 s')
  assert.equal(formatDuration(75_000), '1 m 15 s')
})

test('buildStageTimeline computes duration proportions and revision counters from real events', () => {
  const nodes = [
    makeNode({ id: 'n1', role: 'plan', started_at: '2026-11-23T00:00:00.000Z', finished_at: '2026-11-23T00:00:01.000Z' }),
    makeNode({ id: 'n2', role: 'execute', started_at: '2026-11-23T00:00:01.000Z', finished_at: '2026-11-23T00:00:05.000Z' }),
    makeNode({ id: 'n3', role: 'review', status: 'running', started_at: '2026-11-23T00:00:05.000Z', finished_at: null }),
  ]
  const events = [
    makeEvent('agent.revision_triggered', { revision_round: 1 }),
    makeEvent('agent.revision_skipped', { reason: 'REVISION_BUDGET_EXHAUSTED' }),
  ]
  const summary = buildStageTimeline(nodes, events)
  assert.equal(summary.totalDurationMs, 5000)
  assert.equal(summary.totalDurationLabel, '5.0 s')
  assert.equal(summary.revisionRounds, 1)
  assert.deepEqual(summary.revisionSkippedReasons, ['REVISION_BUDGET_EXHAUSTED'])
  assert.equal(summary.nodes[0].widthPct, 25)
  assert.equal(summary.nodes[1].widthPct, 100)
  assert.equal(summary.nodes[2].durationMs, null)
  assert.equal(summary.nodes[2].durationLabel, '进行中')
  assert.equal(summary.nodes[2].widthPct, 0)
})
