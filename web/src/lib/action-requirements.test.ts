import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { parseActionDrafts } from './action-requirements.ts'

test('optional explicit actions preserve separate command arguments', () => {
  assert.deepEqual(parseActionDrafts([
    { kind: 'workspace_file_write', path: ' src/main.py ', argv: '', cwd: '', client_id: 'write', depends_on: [] },
    { kind: 'sandbox_command', path: '', argv: '["python", "-m", "pytest"]', cwd: '', client_id: 'test', depends_on: ['write'] },
  ]), [
    { kind: 'workspace_file_write', path: 'src/main.py', client_id: 'write', depends_on: [] },
    { kind: 'sandbox_command', argv: ['python', '-m', 'pytest'], cwd: '.', client_id: 'test', depends_on: ['write'] },
  ])
})

test('two actions need an explicit order', () => {
  const drafts = [
    { kind: 'workspace_file_write' as const, path: 'a.py', argv: '', cwd: '', client_id: 'a', depends_on: [] },
    { kind: 'sandbox_command' as const, path: '', argv: '["pytest"]', cwd: '.', client_id: 'b', depends_on: [] },
  ]
  assert.throws(() => parseActionDrafts(drafts))
})

test('an empty file or malformed command cannot be submitted as a requirement', () => {
  assert.throws(() => parseActionDrafts([{ kind: 'workspace_file_write', path: ' ', argv: '', cwd: '' }]))
  assert.throws(() => parseActionDrafts([{ kind: 'sandbox_command', path: '', argv: 'pytest', cwd: '.' }]))
  assert.throws(() => parseActionDrafts([{ kind: 'sandbox_command', path: '', argv: '["pytest", 1]', cwd: '.' }]))
})

test('editing an existing explicit requirement preserves its task binding', () => {
  assert.deepEqual(parseActionDrafts([{ kind: 'workspace_file_write', path: 'main.py', argv: '', cwd: '.', step_id: 's1' }]),
    [{ kind: 'workspace_file_write', path: 'main.py', step_id: 's1' }])
})
