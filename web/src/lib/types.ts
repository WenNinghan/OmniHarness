export type AdapterType = 'openai' | 'anthropic' | 'gemini' | 'openai-compatible' | 'mock'
export type Probe = 'chat' | 'streaming' | 'tools' | 'structured' | 'summary_non_thinking'
export type ProbeResult = { status: 'supported' | 'unsupported' | 'error' | 'unknown'; message: string; parameter_accepted?: boolean; non_thinking_observed?: boolean }
export interface ContextConfiguration {
  schema_version?: number
  policy?: { version?: string; input_target_tokens?: number; summarize_at?: number; summary_output_tokens?: number; keep_recent_tokens?: number; window_margin_tokens?: number; max_messages?: number; max_characters?: number; counter_profile?: string; summary_target_tokens?: number; summary_recovery_output_tokens?: number; summary_recovery_attempts?: number; summary_min_new_tokens?: number; summary_min_gain?: number; post_summary_target?: number; summary_thinking?: 'disable_if_verified' | 'inherit'; replayed_reasoning?: 'count' | 'ignore'; mask_tool_results?: boolean; mask_keep_recent_exchanges?: number; summary_failure?: 'degrade' | 'fail' }
  model_limits?: { context_window_tokens?: number | null; max_input_tokens?: number | null; source?: string | null; verified_at?: string | null }
}
export interface ModelConfig {
  id: string; name: string; adapter_type: AdapterType; model_name: string
  base_url: string | null; enabled: boolean; key_configured: boolean; version: number; provider_version?: number
  parameters: Record<string, unknown>; timeout_seconds: number; max_output_tokens: number
  capabilities: Partial<Record<Probe, ProbeResult>>; tested_at: string | null; created_at: string
  context_policy?: ContextConfiguration
}
export interface ModelInput {
  name: string; adapter_type: AdapterType; model_name: string; base_url?: string
  api_key?: string; enabled: boolean; parameters: Record<string, unknown>
  timeout_seconds: number; max_output_tokens: number
  context_policy?: ContextConfiguration
}
export interface Conversation { id: string; title: string; created_at: string; updated_at: string }
export interface HistoryScope {
  schema_version: 1; kind: 'unscoped' | 'workspace'; workspace_id: string | null; additional_run_ids: string[]
}
export interface Message {
  id: string; conversation_id: string; run_id: string | null; role: 'user' | 'assistant'
  content: string; status: string; created_at: string
  reasoning_content?: string
  output_metadata?: { finish_reason?: string | null; output_status?: string | null; stage?: string; command_result_audit?: unknown }
  error_code?: string | null; error_message?: string | null
  history_scope?: HistoryScope | null; workspace_id?: string | null; run_status?: RunStatus | null
}
export type RunStatus = 'queued' | 'running' | 'cancel_requested' | 'pause_requested' | 'paused' | 'awaiting_approval' | 'awaiting_user' | 'pending_review' | 'completed' | 'cancelled' | 'failed' | 'interrupted'

export type ObservedPhase = 'accepted' | 'executing' | 'verifying' | 'needs_input' | 'needs_reconciliation' | 'done' | 'incomplete' | 'stopping' | 'paused'
export type CacheObservation = {
  value: number | null; observed: number | null; reported_calls: number; total_finished_calls: number
  source: 'provider_reported' | 'partial' | 'unknown'
}
export type RunObservation = {
  schema_version: 1; run_id: string; run_status: RunStatus; phase: ObservedPhase
  observed_at: string; event_watermark: number; last_event_at: string | null; last_event_type: string | null
  execution: { active_model_calls: number; active_tools: number; uncertain_tools: number; uncertain_ledger_entries: number }
  resources: { tool_slots_held: number; workspace_lease_held: boolean }
  inputs: { pending: number; applied_requirements: number; model_read: 'unknown' }
  evidence: {
    command_results: { operation_id: string; status: string; command_id: string | null; sandbox_id: string | null
      exit_code: number | null; outcome: 'exit_recorded' | 'unknown'; resource_reclaimed: boolean
      log_artifact_ids: string[]; source: 'host_tool_record' }[]
    command_total: number; truncated: boolean; independent_acceptance: 'not_recorded_in_this_view'; completion_claim: 'unverified'
  }
  context: { input_records: number; summary_artifacts: number; mask_events: number; degradation_events: number }
  cache: { cache_read_input_tokens: CacheObservation; cache_creation_input_tokens: CacheObservation }
}
export interface Clarification {
  id: string; run_id: string; request_key: string; question: string; reason: string | null
  options: string[]; status: 'pending' | 'answered' | 'cancelled'; answer: string | null
  task_id: string | null; stage: string; created_at: string; answered_at: string | null
}
export type WorkflowType = 'chat' | 'agent'
export type AgentStrategy = 'single' | 'staged' | 'compact'
export type ActionRequirement =
  | { kind: 'workspace_file_write'; path: string; step_id?: string; client_id?: string; depends_on?: string[] }
  | { kind: 'sandbox_command'; argv: string[]; cwd: string; step_id?: string; client_id?: string; depends_on?: string[] }
export interface ActionContract {
  version: number; auto_correction_enabled: boolean
  explicit: { assessed: boolean; reason: string | null; actions: Array<ActionRequirement & { id: string }> }
}
export interface ActionOutcome {
  version: number; status: 'NOT_ASSESSED' | 'BLOCKED' | 'UNVERIFIED' | 'ACTIONS_RECORDED'; reason: string | null
  actions: Array<{ id: string; kind: string; source: string; status: string; reason?: string | null; path?: string; argv?: string[]; cwd?: string; operation_ids: string[] }>
}
export interface Run {
  id: string; conversation_id: string; model_config_id: string; model_label: string
  status: RunStatus; mode: 'live' | 'mock'; workflow_type?: WorkflowType; error_code: string | null; error_message: string | null
  workflow_version?: string | null
  usage: Record<string, number | null> | null; created_at: string; started_at: string | null; finished_at: string | null
  stage_plan?: { stage: Stage; model_config_id: string; model_name: string; source: string }[] | null
  workspace_id?: string | null
  agent_strategy?: AgentStrategy
  active_stages?: Stage[]
  workspace?: { id: string; name: string; policy_version: number; tool_policy: Record<string, unknown> } | null
  action_contract?: ActionContract | null
  action_outcome?: ActionOutcome | null
  action_candidates?: Array<ActionRequirement & { id: string; source: string }> | null
}
export interface RunEvent {
  schema_version: 1; event_id: string; run_id: string; sequence_number: number
  event_type: string; created_at: string; payload: Record<string, unknown>
}
export type EvidenceCapability = 'supported' | 'not_recorded' | 'unknown'
export interface EvidenceViewItem {
  sequence_number: number; event_id: string; event_type: string
  payload?: Record<string, unknown>; unsupported_schema?: boolean
}
export interface EvidenceViewPage {
  schema_version: 1; run_id: string; watermark: number; snapshot_total: number
  items: EvidenceViewItem[]; next_cursor: string | null; snapshot_complete: boolean
  snapshot_status: 'loading' | 'complete' | 'incomplete' | 'legacy_unrecorded'
  recording_capabilities: { handoff: EvidenceCapability; review: EvidenceCapability }
}
export interface AgentRun {
  id: string; run_id: string; parent_agent_run_id: string | null; role: string
  status: string; depth: number; model_config_id: string | null; model_name?: string | null; selection_source?: string | null; error_code: string | null
  created_at: string; started_at: string | null; finished_at: string | null
}
export type CommandType = 'pause' | 'resume' | 'append_requirement'
export interface RunCommand {
  run_id: string; command_id: string; sequence_number: number
  command_type: CommandType; status: string; run_status: string
}
export interface BudgetSnapshot {
  run_id: string; initialized: boolean
  max_model_calls: number; model_calls: number
  max_tool_calls: number; tool_calls: number
  max_revisions: number; revisions: number
  max_active_seconds: number; active_seconds: number
  budget_remaining: Record<string, number>; updated_at: string | null
}
export type AgentRole = 'think' | 'plan' | 'execute' | 'review' | 'verifier'
export interface RoleBinding {
  role: AgentRole; model_config_id: string | null
  model_label: string | null; enabled: boolean
}
export interface RoleBindings {
  version: number; bindings: RoleBinding[]; updated_at: string | null
}
export const agentRoles: AgentRole[] = ['think', 'plan', 'execute', 'review', 'verifier']
export const roleLabels: Record<AgentRole, string> = {
  think: '思考链', plan: '任务规划', execute: '执行', review: '审查', verifier: '验证',
}
export const roleDescriptions: Record<AgentRole, string> = {
  think: '拆解问题、形成推理链', plan: '生成可执行的步骤与分工',
  execute: '实际产出结果与调用工具', review: '复核输出、发现遗漏',
  verifier: '独立验收并给出结论',
}
export interface Bootstrap { version: string; authenticated: boolean; pairing_required: boolean; demo_enabled: boolean; stage: string; agent_available?: boolean; v6_preview?: boolean; stage_workflow_version?: string; workspace_workflow_version?: string }
export const terminalStatuses = new Set<RunStatus>(['completed', 'cancelled', 'failed', 'interrupted'])
export const statusLabels: Record<RunStatus, string> = {
  queued: '等待运行', running: '正在生成', cancel_requested: '正在停止',
  pause_requested: '正在暂停', paused: '已暂停', pending_review: '待核对', completed: '已完成',
  cancelled: '已停止', failed: '运行失败', interrupted: '运行中断',
  awaiting_approval: '等待你的审批', awaiting_user: '等待你的回复',
}

export interface WorkspaceConfig {
  id: string; name: string; host_path: string; policy_version: number; enabled: boolean; ready: boolean
  read_allow: string[]; write_allow: string[]; excludes: string[]; tool_policy: Record<string, unknown>
}
export interface ToolOperation {
  id: string; run_id: string; task_id: string; tool_name: string; tool_version: string; parameters: Record<string, unknown>
  status: string; result: Record<string, unknown> | null; error_code: string | null
  sandbox_id: string | null; command_id: string | null; snapshot_id: string | null; binding_fingerprint: string
  created_at: string; finished_at: string | null
}
export interface ToolApproval {
  id: string; operation_id: string; run_id: string; fingerprint: string; status: string; operation: ToolOperation
}
export interface ArtifactReference {
  id: string; run_id: string; operation_id: string | null; kind: string; name: string
  sha256: string; size_bytes: number; metadata_json: Record<string, unknown>; created_at: string
}

export interface CatalogModel { id: string; owned_by: string | null; capabilities: null; price: null }
export interface ModelCatalog { model_config_id: string; fetched_at: string; models: CatalogModel[] }
export interface CatalogImport { results: { model_id: string; status: 'created' | 'existing'; model_config_id: string }[] }
export type Stage = 'think' | 'plan' | 'dispatch' | 'execute' | 'review'
export interface StageBinding { stage: Stage; model_config_id: string | null; model_label: string | null }
export interface StageBindings { version: number; bindings: StageBinding[]; updated_at: string | null }
export interface StageRecommendation { stage: Stage; model_config_id: string; reason: string; capabilities: { stage_score?: number; minimum_score?: number } | null; price: { estimated_cost: string; currency: string; input_price_per_million: string; output_price_per_million: string; expected_input_tokens: number; expected_output_tokens: number; source: string; basis: 'user_estimate' } | null; evidence?: { qualified_model_config_ids: string[]; unassessed_model_config_ids: string[]; comparison_currency_conflict: boolean } }
export interface RoutingRecommendation { recommendations: StageRecommendation[]; limitations: string[] }
export interface RoutingModelSelection { stage: Stage; model_config_id: string; model_version: number; provider_version: number }
export interface AIRoutingStage extends RoutingModelSelection { reason: string }
export interface AIRoutingPreview { planner: { model_config_id: string; model_name: string; endpoint: string; model_version: number; provider_version: number } }
export interface AIRoutingPlan {
  planner: { model_config_id: string; model_name: string; endpoint: string }
  stages: AIRoutingStage[]; warnings: string[]
  usage: { input_tokens: number | null; output_tokens: number | null; total_tokens: number | null; cost: null }
}
export interface PendingRoutingPlan { task: string; stages: AIRoutingStage[]; warnings: string[]; planner: AIRoutingPlan['planner']; usage: AIRoutingPlan['usage']; acceptedTask: string | null }
export const stages: Stage[] = ['think', 'plan', 'dispatch', 'execute', 'review']
export const stageLabels: Record<Stage, string> = { think: '思考', plan: '任务规划', dispatch: '任务分配', execute: '执行', review: '审查' }
export const stageDescriptions: Record<Stage, string> = {
  think: '理解目标与约束', plan: '制定步骤与验收条件', dispatch: '分配子任务与依赖',
  execute: '根据已分配任务产出结果', review: '根据目标与证据审查结果',
}
