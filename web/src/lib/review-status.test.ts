import assert from 'node:assert/strict'
import test from 'node:test'
import { reviewStatusCopyText, reviewStatusWarning } from './review-status.ts'

test('recognises only allowlisted degraded review codes', () => {
  for (const code of ['INVALID_OUTPUT', 'INCOMPLETE', 'UNKNOWN', 'NOT_RUN_BUDGET'] as const) {
    const warning = reviewStatusWarning(code)
    assert.ok(warning)
    assert.equal(warning.code, code)
    assert.match(warning.label, /^审查未完成/)
  }
  for (const ignored of [null, undefined, '', 'COMPLETED', 'PASS', 'arbitrary', 123, {}, []]) {
    assert.equal(reviewStatusWarning(ignored), null)
    assert.equal(reviewStatusCopyText('answer body', ignored), 'answer body')
  }
})

test('preserves degraded review notice when copying an answer', () => {
  const copied = reviewStatusCopyText('执行结果正文', 'UNKNOWN')
  assert.match(copied, /^【独立审查未完成（UNKNOWN）】/)
  assert.ok(copied.endsWith('\n\n执行结果正文'))
})
