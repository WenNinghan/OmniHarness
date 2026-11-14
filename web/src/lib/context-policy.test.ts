import assert from 'node:assert/strict'
import test from 'node:test'
import { contextFromForm } from './context-policy.ts'

test('v2 keeps main output separate and preserves declared limits', () => {
  const data = new FormData()
  data.set('context_v2', 'on'); data.set('context_recovery', 'on')
  const result = contextFromForm(data, { policy: { max_characters: 123456 }, model_limits: { max_input_tokens: 32000 } })
  assert.equal(result.policy?.version, 'context-v2')
  assert.equal(result.policy?.summary_target_tokens, 1200)
  assert.equal(result.policy?.summary_output_tokens, 4000)
  assert.equal(result.policy?.summary_recovery_output_tokens, 8000)
  assert.equal(result.policy?.summary_recovery_attempts, 1)
  assert.equal(result.policy?.max_characters, 123456)
  assert.equal(result.model_limits?.max_input_tokens, 32000)
  assert.equal('max_output_tokens' in result, false)
})

test('returning to v1 removes v2-only fields, disabling recovery stays explicit', () => {
  const old = { policy: { version: 'context-v2', summary_target_tokens: 1200, counter_profile: 'unicode-weighted-v2' } }
  const result = contextFromForm(new FormData(), old)
  assert.equal(result.policy?.version, 'context-v1')
  assert.equal('summary_target_tokens' in result.policy!, false)
  assert.equal('counter_profile' in result.policy!, false)
  const enabled = new FormData(); enabled.set('context_v2', 'on')
  assert.equal(contextFromForm(enabled).policy?.summary_recovery_attempts, 0)
})

test('v3 selection adds masking and degradation fields with safe defaults', () => {
  const data = new FormData()
  data.set('context_version', 'context-v3'); data.set('context_mask_tools', 'on'); data.set('context_recovery', 'on')
  const result = contextFromForm(data)
  assert.equal(result.policy?.version, 'context-v3')
  assert.equal(result.policy?.input_target_tokens, 32000)
  assert.equal(result.policy?.keep_recent_tokens, 8000)
  assert.equal(result.policy?.mask_tool_results, true)
  assert.equal(result.policy?.mask_keep_recent_exchanges, 4)
  assert.equal(result.policy?.summary_failure, 'degrade')
  assert.equal(result.policy?.replayed_reasoning, 'count')
  assert.equal(result.policy?.summary_recovery_attempts, 1)
})

test('v3 explicit choices are kept and v2 never carries v3-only fields', () => {
  const data = new FormData()
  data.set('context_version', 'context-v3'); data.set('context_replayed_reasoning', 'ignore')
  data.set('context_summary_failure', 'fail'); data.set('context_mask_keep', '6')
  const v3 = contextFromForm(data)
  assert.equal(v3.policy?.replayed_reasoning, 'ignore')
  assert.equal(v3.policy?.summary_failure, 'fail')
  assert.equal(v3.policy?.mask_tool_results, false)
  assert.equal(v3.policy?.mask_keep_recent_exchanges, 6)
  const back = new FormData(); back.set('context_version', 'context-v2')
  const v2 = contextFromForm(back, v3)
  assert.equal(v2.policy?.version, 'context-v2')
  for (const key of ['replayed_reasoning', 'mask_tool_results', 'mask_keep_recent_exchanges', 'summary_failure'])
    assert.equal(key in v2.policy!, false)
})
