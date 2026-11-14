import type { WorkflowType } from './types.ts'

// Backward-compatible but fail-closed: only an explicit server true enables Agent.
export function workflowAvailable(workflow: WorkflowType, agentAvailable: unknown): boolean {
  return workflow === 'chat' || agentAvailable === true
}

export function refreshesAgentNodes(eventType: string): boolean {
  return eventType.startsWith('agent.') || eventType === 'run.pending_review'
    || ['run.completed', 'run.cancelled', 'run.failed', 'run.interrupted'].includes(eventType)
}
