import assert from 'node:assert/strict'
import test from 'node:test'
import { cacheDescription, elapsedEvidenceTime, observationIsCurrent } from './run-observation.ts'
import type { RunObservation } from './types.ts'

test('unknown and partially reported cache usage never become a complete zero', () => {
  assert.match(cacheDescription({ source: 'unknown', value: null, observed: null, reported_calls: 0, total_finished_calls: 2 }), /未知/)
  assert.match(cacheDescription({ source: 'partial', value: null, observed: 0, reported_calls: 1, total_finished_calls: 2 }), /总量未知/)
  assert.match(cacheDescription({ source: 'provider_reported', value: 0, observed: 0, reported_calls: 2, total_finished_calls: 2 }), /0 tokens（供应商返回）/)
})

test('a stale or different Run snapshot cannot overwrite the current observation', () => {
  const value = { schema_version: 1, run_id: 'run', phase: 'needs_reconciliation', event_watermark: 8,
    observed_at: '2026-11-14T12:00:00+00:00' } as RunObservation
  assert.equal(observationIsCurrent(value, 'run', 8), true)
  assert.equal(observationIsCurrent(value, 'other', 8), false)
  assert.equal(observationIsCurrent(value, 'run', 9), false)
  assert.equal(observationIsCurrent({ ...value, schema_version: 2 } as unknown as RunObservation, 'run', 0), false)
})

test('event age describes only observed evidence time', () => {
  assert.equal(elapsedEvidenceTime(null, '2026-11-14T12:00:00Z'), '尚无事件')
  assert.equal(elapsedEvidenceTime('2026-11-14T11:58:00Z', '2026-11-14T12:00:00Z'), '2 分钟前')
  assert.equal(elapsedEvidenceTime('invalid', '2026-11-14T12:00:00Z'), '时间未知')
})
