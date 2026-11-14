import type { EvidenceRequest } from './routing-evidence.ts'
import type { AgentStrategy, ArtifactReference, Clarification, EvidenceViewPage, HistoryScope, ToolApproval, ToolOperation, WorkspaceConfig } from './types.ts'
import type { AgentRole, AgentRun, Bootstrap, BudgetSnapshot, CommandType, Conversation, Message, ModelConfig, ModelInput, Probe, ProbeResult, RoleBindings, Run, RunCommand, WorkflowType, ModelCatalog, CatalogImport, RoutingRecommendation, StageBindings, Stage, AIRoutingPlan, AIRoutingPreview, RoutingModelSelection } from './types.ts'

export class ApiError extends Error {
  code: string
  status: number
  constructor(message: string, code: string, status: number) { super(message); this.code = code; this.status = status }
}
export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const response = await fetch(`/api/v1${path}`, {
    credentials: 'same-origin', ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(['POST', 'PUT', 'PATCH', 'DELETE'].includes(method) ? { 'X-Omni-Request': '1' } : {}), ...options.headers },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const detail = body?.detail
    const message = typeof detail === 'string' ? detail : detail?.message ?? `请求失败（${response.status}）`
    throw new ApiError(message, detail?.code ?? 'request_failed', response.status)
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>
}
const json = (method: string, body?: unknown): RequestInit => ({ method, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) })
export const api = {
  runObservation: (id: string, signal?: AbortSignal) => request<import('./types.ts').RunObservation>(
    `/runs/${encodeURIComponent(id)}/observation`, { signal }),
  clarifications: (runId: string) => request<Clarification[]>(`/runs/${encodeURIComponent(runId)}/clarifications`),
  answerClarification: (runId: string, id: string, answer: string, client_request_id: string) => request<Clarification>(`/runs/${encodeURIComponent(runId)}/clarifications/${encodeURIComponent(id)}/answer`, json('POST', { answer, client_request_id })),
  workspaces: () => request<WorkspaceConfig[]>('/workspaces'),
  updateActionRequirements: (id: string, expected_version: number, requirements: import('./types.ts').ActionRequirement[]) => request<import('./types.ts').ActionContract>(`/runs/${id}/action-requirements`, json('PUT', { expected_version, requirements })),
  toolStatus: () => request<{ enabled: boolean; broker_ready: boolean }>('/tools/status'),
  createWorkspace: (input: { name: string; host_path: string; write_allow: string[]; excludes: string[]; command_approval: boolean }) => request<WorkspaceConfig>('/workspaces', json('POST', input)),
  workspaceEnabled: (id: string, enabled: boolean) => request<WorkspaceConfig>(`/workspaces/${id}`, json('PATCH', { enabled })),
  toolOperations: (id: string) => request<ToolOperation[]>(`/runs/${id}/tools`),
  reconcileTool: (id: string, fingerprint: string, requestId: string) => request<{ operation: ToolOperation; workspace_released: boolean }>(`/tool-operations/${id}/reconcile`, json('POST', { fingerprint, acknowledge_unverified: true, client_request_id: requestId })),
  approvals: (id: string) => request<ToolApproval[]>(`/runs/${id}/approvals`),
  decideApproval: (id: string, fingerprint: string, decision: 'approve' | 'reject') => request<ToolApproval>(`/approvals/${id}/decision`, json('POST', { fingerprint, decision, client_request_id: crypto.randomUUID() })),
  artifacts: (id: string) => request<ArtifactReference[]>(`/runs/${id}/artifacts`),
  evidenceView: (id: string, cursor?: string | null) => request<EvidenceViewPage>(
    `/runs/${encodeURIComponent(id)}/evidence-view?limit=25${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`),
  evidenceArtifact: async (runId: string, artifactId: string, offset = 0) => {
    const response = await fetch(`/api/v1/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}/content?offset=${offset}&limit=32768`, { credentials: 'same-origin' })
    if (!response.ok) throw new Error('工件不可用或不属于当前任务。')
    const nextRaw = response.headers.get('X-Artifact-Next-Offset'), sizeRaw = response.headers.get('X-Artifact-Size'), sha256 = response.headers.get('X-Artifact-SHA256')
    if (nextRaw === null || sizeRaw === null || !/^\d+$/.test(nextRaw) || !/^\d+$/.test(sizeRaw) || !sha256 || !/^[0-9a-f]{64}$/.test(sha256)) throw new Error('工件分页信息不可用。')
    const next = Number(nextRaw), size = Number(sizeRaw)
    if (!Number.isSafeInteger(next) || !Number.isSafeInteger(size) || next < offset || next > size || (next === offset && next < size)) throw new Error('工件分页信息无效。')
    return { content: await response.text(), next, size, sha256 }
  },
  bootstrap: () => request<Bootstrap>('/bootstrap'),
  pair: (code: string) => request<{ authenticated: boolean }>('/auth/pair', json('POST', { code })),
  logout: () => request<void>('/auth/logout', json('POST')),
  models: () => request<ModelConfig[]>('/model-configs'),
  saveModel: (input: ModelInput, id?: string) => request<ModelConfig>(id ? `/model-configs/${id}` : '/model-configs', json(id ? 'PUT' : 'POST', input)),
  disableModel: (id: string) => request<void>(`/model-configs/${id}`, json('DELETE')),
  testModel: (id: string, probes: Probe[]) => request<{ model_config_id: string; results: Partial<Record<Probe, ProbeResult>>; tested_at: string }>(`/model-configs/${id}/test`, json('POST', { probes })),
  modelCatalog: (id: string) => request<ModelCatalog>(`/model-configs/${encodeURIComponent(id)}/catalog`),
  importCatalog: (id: string, model_ids: string[]) => request<CatalogImport>(`/model-configs/${encodeURIComponent(id)}/catalog/import`, json('POST', { model_ids })),
  recommendModels: (default_model_config_id: string, candidate_model_config_ids?: string[], evidence?: EvidenceRequest) => request<RoutingRecommendation>('/model-routing/recommend', json('POST', { default_model_config_id, ...(candidate_model_config_ids ? { candidate_model_config_ids } : {}), ...(evidence ?? {}) })),
  aiPlanPreview: (id: string) => request<AIRoutingPreview>(`/model-routing/ai-plan/preview?planner_model_config_id=${encodeURIComponent(id)}`),
  aiPlan: (body: { task: string; planner_model_config_id: string; planner_model_version: number; planner_provider_version: number; candidate_model_config_ids: string[]; assessments?: EvidenceRequest['assessments'] }) => request<AIRoutingPlan>('/model-routing/ai-plan', json('POST', body)),
  stageBindings: () => request<StageBindings>('/stage-bindings'),
  saveStageBindings: (expected_version: number, bindings: { stage: Stage; model_config_id: string | null }[]) => request<StageBindings>('/stage-bindings', json('PUT', { expected_version, bindings })),
  exportConfig: () => request<{ schema_version: 1; model_configs: unknown[] }>('/configuration/export'),
  importConfig: (body: unknown) => request<{ imported: number }>('/configuration/import', json('POST', body)),
  conversations: () => request<Conversation[]>('/conversations'),
  createConversation: (title?: string) => request<Conversation>('/conversations', json('POST', { title })),
  messages: (id: string) => request<Message[]>(`/conversations/${id}/messages`),
  send: (id: string, content: string, model_config_id: string, client_request_id: string, workflow_type: WorkflowType = 'chat', stage_overrides?: RoutingModelSelection[], workspace_id?: string | null, agent_strategy: AgentStrategy = 'staged', history_scope?: HistoryScope, actionOptions?: { action_requirements?: import('./types.ts').ActionRequirement[]; auto_correction_enabled: boolean }) => request<{ conversation_id: string; run_id: string; message_id: string }>(`/conversations/${id}/messages`, json('POST', { content, model_config_id, client_request_id, workflow_type, ...(stage_overrides ? { stage_overrides } : {}), ...(workspace_id ? { workspace_id, agent_strategy } : {}), ...(history_scope ? { history_scope } : {}), ...(actionOptions ?? {}) })),
  runs: (id: string) => request<Run[]>(`/runs?conversation_id=${encodeURIComponent(id)}`),
  run: (id: string) => request<Run>(`/runs/${id}`),
  agentRuns: (id: string) => request<AgentRun[]>(`/runs/${encodeURIComponent(id)}/agent-runs`),
  cancel: (id: string) => request<Run>(`/runs/${id}/cancel`, json('POST')),
  submitCommand: (id: string, command_type: CommandType, content?: string) => request<RunCommand>(`/runs/${id}/commands`, json('POST', {
    command_type, client_request_id: crypto.randomUUID(), ...(content !== undefined ? { content } : {}),
  })),
  commands: (id: string) => request<RunCommand[]>(`/runs/${id}/commands`),
  budget: (id: string) => request<BudgetSnapshot>(`/runs/${id}/budget`),
  bindings: () => request<RoleBindings>('/orchestration/bindings'),
  saveBindings: (bindings: { role: AgentRole; model_config_id: string | null }[]) =>
    request<RoleBindings>('/orchestration/bindings', json('PUT', { bindings })),
}
