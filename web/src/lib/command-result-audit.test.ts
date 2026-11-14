import test from 'node:test'
import assert from 'node:assert/strict'
import { commandResultAuditCopyText, commandResultAuditWarning } from './command-result-audit.ts'

const id = '61b78660-6971-4d7f-99d4-c3d7c975d0b3'
const conflict = { artifact_id: id, operation_id: 'a'.repeat(64), claimed_exit_code: 0, actual_exit_code: 1, line: 7 }

test('an incomplete audit is distinct from a conflict and is retained in copied text', () => {
  const value = { status: 'incomplete', conflicts: [], truncated: true }
  assert.equal(commandResultAuditWarning(value)?.kind, 'incomplete')
  const answer = '原始答复\n'
  const copied = commandResultAuditCopyText(answer, value)
  assert.match(copied, /命令退出码核对未完成/)
  assert.doesNotMatch(copied, /不一致|存在冲突|已通过/)
  assert.ok(copied.endsWith('\n\n' + answer))
  assert.deepEqual(commandResultAuditWarning({ status: 'incomplete', conflicts: [conflict], truncated: true }),
    { kind: 'incomplete', conflicts: [], truncated: true })
})

test('a confirmed conflict displays the full artifact id and both exit codes', () => {
  assert.deepEqual(commandResultAuditWarning({ status: 'conflict', conflicts: [conflict], truncated: false }), {
    kind: 'conflict', conflicts: [{ artifactId: id, claimedExitCode: 0, actualExitCode: 1 }], truncated: false,
  })
})

test('missing, unknown and other statuses never become a passed claim or conflict warning', () => {
  for (const value of [undefined, null, [], 'conflict', {}, { status: 'unknown' }, { status: 'checked' }, { status: 'no_conflict_detected' },
    { status: 'clear', conflicts: [conflict] }, { status: 'CONFLICT', conflicts: [conflict] }]) {
    assert.equal(commandResultAuditWarning(value), null)
  }
})

test('malformed conflict details retain the warning without invented identifiers or exit codes', () => {
  const bad = [{ ...conflict, artifact_id: '61b78660' }, { ...conflict, claimed_exit_code: '0' },
    { ...conflict, actual_exit_code: 0 }, { ...conflict, actual_exit_code: Infinity }, null]
  assert.deepEqual(commandResultAuditWarning({ status: 'conflict', conflicts: bad }), { kind: 'conflict', conflicts: [], truncated: false })
})

test('the truncation flag covers both bounded display and limited audit scope', () => {
  assert.equal(commandResultAuditWarning({ status: 'conflict', conflicts: [conflict], truncated: true })?.kind, 'conflict')
  assert.equal(commandResultAuditWarning({ status: 'conflict', conflicts: Array(11).fill(conflict) })?.conflicts.length, 10)
  assert.equal(commandResultAuditWarning({ status: 'conflict', conflicts: Array(11).fill(conflict) })?.truncated, true)
  assert.equal(commandResultAuditWarning({ status: 'conflict', conflicts: [conflict], truncated: true })?.truncated, true)
  assert.deepEqual(commandResultAuditWarning({ status: 'conflict', conflicts: [], truncated: true }), { kind: 'conflict', conflicts: [], truncated: true })
})

test('copying a conflicting answer preserves the warning, full artifact id and real exit code', () => {
  const answer = '第一行\n原始模型回答。'
  const copied = commandResultAuditCopyText(answer, { status: 'conflict', conflicts: [conflict], truncated: true })
  assert.match(copied, /命令退出码核对提醒/)
  assert.ok(copied.includes(`工件 ${id}：回答写退出码 0，工具记录为 1。`))
  assert.ok(copied.includes('核对范围或展示数量有限，请查看完整工具记录。'))
  assert.ok(copied.endsWith('\n\n' + answer))
})

test('copying without conflict preserves exact answer; malformed details still copy a generic warning', () => {
  const answer = '  保留原样\n'
  for (const value of [undefined, null, { status: 'unknown' }, { status: 'no_conflict_detected' }]) {
    assert.equal(commandResultAuditCopyText(answer, value), answer)
  }
  const copied = commandResultAuditCopyText(answer, { status: 'conflict', conflicts: [{ artifact_id: 'partial' }] })
  assert.match(copied, /命令退出码核对提醒/)
  assert.ok(copied.endsWith('\n\n' + answer))
  assert.ok(!copied.includes('partial'))
})
