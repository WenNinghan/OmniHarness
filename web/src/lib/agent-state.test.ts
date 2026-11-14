import test from 'node:test'
import assert from 'node:assert/strict'
import { refreshesAgentNodes, workflowAvailable } from './agent-state.ts'

test('agent mode requires an explicit ready response, with no fallback to chat', () => {
  assert.equal(workflowAvailable('chat', undefined), true)
  assert.equal(workflowAvailable('agent', undefined), false)
  assert.equal(workflowAvailable('agent', false), false)
  assert.equal(workflowAvailable('agent', 'true'), false)
  assert.equal(workflowAvailable('agent', true), true)
})

test('actual agent and lifecycle events refresh the persisted node snapshot', () => {
  for (const event of ['agent.model_started', 'agent.delegated', 'agent.model_completed', 'agent.stage_result', 'run.pending_review', 'run.cancelled']) {
    assert.equal(refreshesAgentNodes(event), true)
  }
  assert.equal(refreshesAgentNodes('message.delta'), false)
})
