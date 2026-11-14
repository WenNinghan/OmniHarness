import assert from 'node:assert/strict'
import test from 'node:test'
import { isUndispatchedSchemaFailure } from './tool-activity-state.ts'

test('only a versioned schema audit proves a request was not dispatched', () => {
  assert.equal(isUndispatchedSchemaFailure({ tool_version: 'invalid-tool-audit-v1', error_code: 'INVALID_ARGUMENTS' }), true)
  assert.equal(isUndispatchedSchemaFailure({ tool_version: 'invalid-tool-audit-v9-v1', error_code: 'INVALID_ARGUMENTS' }), true)
  assert.equal(isUndispatchedSchemaFailure({ tool_version: 'builtin-workspace-v1', error_code: 'INVALID_ARGUMENTS' }), false)
  assert.equal(isUndispatchedSchemaFailure({ tool_version: 'invalid-tool-audit-v1', error_code: null }), false)
})
