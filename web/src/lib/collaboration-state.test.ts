import assert from 'node:assert/strict'
import test from 'node:test'
import { strategyFromStorage, strategyStages } from './collaboration-state.ts'

test('only actual model stages are shown, unknown persisted strategy keeps old default', () => {
  assert.deepEqual(strategyStages.compact, ['plan', 'execute', 'review'])
  assert.deepEqual(strategyStages.single, ['execute'])
  assert.equal(strategyStages.staged.length, 5)
  assert.equal(strategyFromStorage('arbitrary'), 'staged')
  assert.equal(strategyFromStorage('compact'), 'compact')
})
