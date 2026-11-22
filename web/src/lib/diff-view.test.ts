import assert from 'node:assert/strict'
import test from 'node:test'
import { compoundMaskingWarning, extractPatchDiff, parseUnifiedDiff } from './diff-view.ts'
import type { ToolOperation } from './types.ts'

function makeOperation(overrides: Partial<ToolOperation>): ToolOperation {
  return {
    id: 'op-1',
    run_id: 'run-1',
    task_id: 'task-1',
    tool_name: 'workspace_patch',
    tool_version: 'builtin-workspace-v1',
    parameters: {},
    status: 'completed',
    result: null,
    error_code: null,
    sandbox_id: null,
    command_id: null,
    snapshot_id: null,
    binding_fingerprint: 'a'.repeat(64),
    created_at: '2026-11-23T00:00:00Z',
    finished_at: '2026-11-23T00:00:01Z',
    ...overrides,
  }
}

test('parseUnifiedDiff handles standard unified diff and CRLF line endings', () => {
  const sample = [
    '--- a/foo.py',
    '+++ b/foo.py',
    '@@ -1,3 +1,3 @@',
    ' def add(a, b):',
    '-    return a - b',
    '+    return a + b',
    '+    # fixed',
    '',
  ].join('\r\n')
  const parsed = parseUnifiedDiff(sample)
  assert.equal(parsed.additions, 2)
  assert.equal(parsed.deletions, 1)
  assert.equal(parsed.truncated, false)
  assert.equal(parsed.totalLines, 7)
  assert.deepEqual(
    parsed.lines.map(item => item.kind),
    ['meta', 'meta', 'hunk', 'context', 'del', 'add', 'add'],
  )
})

test('parseUnifiedDiff handles empty diff and maxLines truncation with exact totals', () => {
  assert.deepEqual(parseUnifiedDiff(''), {
    lines: [],
    additions: 0,
    deletions: 0,
    truncated: false,
    totalLines: 0,
  })
  const diff = ['--- a/x', '+++ b/x', '@@ -1 +1,3 @@', '-old', '+new1', '+new2', '+new3\n'].join('\n')
  const parsed = parseUnifiedDiff(diff, 4)
  assert.equal(parsed.truncated, true)
  assert.equal(parsed.totalLines, 7)
  assert.equal(parsed.lines.length, 4)
  assert.equal(parsed.additions, 3)
  assert.equal(parsed.deletions, 1)
})

test('extractPatchDiff extracts completed patch, new file, truncated result, and ignores failures/other tools', () => {
  const edited = makeOperation({
    parameters: { path: 'src/app.py' },
    result: {
      path: 'src/app.py',
      before_sha256: 'b'.repeat(64),
      sha256: 'c'.repeat(64),
      recovery_id: 'rec-123',
      diff: '--- src/app.py\n+++ src/app.py\n@@ -1 +1 @@\n-x\n+y\n',
    },
  })
  assert.deepEqual(extractPatchDiff(edited), {
    path: 'src/app.py',
    diff: '--- src/app.py\n+++ src/app.py\n@@ -1 +1 @@\n-x\n+y\n',
    beforeSha: 'b'.repeat(64),
    afterSha: 'c'.repeat(64),
    recoveryId: 'rec-123',
    truncatedInResult: false,
    rollback: null,
  })

  const created = makeOperation({
    parameters: { path: 'new_file.py' },
    result: {
      path: 'new_file.py',
      before_sha256: null,
      sha256: 'd'.repeat(64),
      recovery_id: 'rec-new',
      diff: '--- new_file.py\n+++ new_file.py\n@@ -0,0 +1 @@\n+hello\n',
    },
  })
  assert.equal(extractPatchDiff(created)?.beforeSha, null)

  const truncated = makeOperation({
    parameters: { path: 'big.py' },
    result: {
      path: 'big.py',
      sha256: 'e'.repeat(64),
      truncated: true,
      preview: '...',
    },
  })
  assert.deepEqual(extractPatchDiff(truncated), {
    path: 'big.py',
    diff: null,
    beforeSha: null,
    afterSha: 'e'.repeat(64),
    recoveryId: null,
    truncatedInResult: true,
    rollback: null,
  })

  const rolledBack = makeOperation({
    parameters: { path: 'src/app.py' },
    result: {
      path: 'src/app.py',
      sha256: 'c'.repeat(64),
      rollback: { action: 'restored', restored_sha256: 'b'.repeat(64) },
    },
  })
  assert.deepEqual(extractPatchDiff(rolledBack)?.rollback, {
    action: 'restored',
    restoredSha: 'b'.repeat(64),
  })

  const failed = makeOperation({
    status: 'failed',
    error_code: 'version_conflict',
    result: { error: 'version_conflict', message: 'changed' },
  })
  assert.equal(extractPatchDiff(failed), null)
  assert.equal(extractPatchDiff(makeOperation({ tool_name: 'workspace_read', result: { content: 'hi' } })), null)
})

test('compoundMaskingWarning surfaces terminal_segment_only operators and ignores clean commands', () => {
  const compound = makeOperation({
    tool_name: 'run_command',
    result: {
      exit_code: 0,
      compound_exit_masking: [';', '|'],
      exit_code_scope: 'terminal_segment_only',
    },
  })
  const warning = compoundMaskingWarning(compound)
  assert.ok(warning)
  assert.match(warning, /；|;|、/)
  assert.match(warning, /末段命令/)

  const clean = makeOperation({
    tool_name: 'run_command',
    result: { exit_code: 0 },
  })
  assert.equal(compoundMaskingWarning(clean), null)
})
