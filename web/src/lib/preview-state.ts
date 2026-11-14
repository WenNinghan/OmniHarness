import type { Run, WorkflowType } from './types.ts'

/** Describe the version selected for new Runs; old Runs retain their own frozen version. */
export function previewBadge(workspaceVersion: string | undefined): string {
  if (workspaceVersion === 'stage-agent-v7' || workspaceVersion === 'stage-agent-v8' || workspaceVersion === 'stage-agent-v9' || workspaceVersion === 'stage-agent-v10') return '交互预览'
  if (workspaceVersion === 'stage-agent-v6') return 'V6 预览'
  return '预览版本未知'
}

export function previewNotice(stageVersion: string | undefined, workspaceVersion: string | undefined,
  workflowType: string, hasWorkspace: boolean, strategy: string, strategyLabel: string): string {
  const isolation = '预览请使用独立测试项目，勿与原服务同时修改同一目录。'
  if (workflowType === 'chat') return `下次任务将使用普通聊天，不使用 Agent 协作。 ${isolation}`
  if (!hasWorkspace) {
    const status = stageVersion === 'stage-agent-interactive-v1' ? '无项目 Agent 将使用交互候选。'
      : stageVersion === 'stage-agent-v2' ? '无项目 Agent 将使用原阶段流程。' : '无项目 Agent 版本未知，请检查服务。'
    return `${status} ${isolation}`
  }
  const version = (workspaceVersion === 'stage-agent-v7' || workspaceVersion === 'stage-agent-v8' || workspaceVersion === 'stage-agent-v9' || workspaceVersion === 'stage-agent-v10') ? '交互候选' : workspaceVersion === 'stage-agent-v6' ? 'V6' : null
  if (!version) return `预览工作区版本未知，请检查服务。 ${isolation}`
  const status = strategy === 'single' ? `下次工作区任务将使用 ${version} 单 Agent，不产生跨 Agent 交接或独立审查。`
    : `下次工作区任务将使用 ${version} · ${strategyLabel}；运行详情展示已记录的来源与审查。`
  return `${status} ${isolation}`
}

/** Activity is described only from the frozen Run, never from draft settings. */
export function previewActiveNotice(run: Run): string {
  const isolation = '预览请使用独立测试项目，勿与原服务同时修改同一目录。'
  if (run.workflow_type === 'chat') return `当前任务：普通聊天，不使用 Agent 协作。 ${isolation}`
  if (run.workflow_type !== 'agent') return `当前任务模式未记录，请查看运行详情。 ${isolation}`
  const version = run.workflow_version === 'stage-agent-v10' ? '交互候选（stage-agent-v10）'
    : run.workflow_version === 'stage-agent-v9' ? '交互候选（stage-agent-v9）'
    : run.workflow_version === 'stage-agent-v8' ? '交互候选（stage-agent-v8）'
    : run.workflow_version === 'stage-agent-v7' ? '交互候选（stage-agent-v7）'
    : run.workflow_version === 'stage-agent-v6' ? 'V6（stage-agent-v6）'
      : run.workflow_version === 'stage-agent-interactive-v1' ? '交互候选（无项目）'
        : run.workflow_version ? `工作流 ${run.workflow_version}` : '工作流版本未记录'
  const workspace = run.workspace_id === undefined ? '工作区信息未记录' : run.workspace_id ? '工作区 Agent' : '无项目 Agent'
  const strategy = run.agent_strategy === 'single' ? '单 Agent'
    : run.agent_strategy === 'compact' ? '精简协作' : run.agent_strategy === 'staged' ? '完整五阶段' : '协作模式未记录'
  return `当前任务：${workspace} · ${version} · ${strategy}。 ${isolation}`
}

export function displayedWorkflowType(active: boolean, run: Run | null, draft: WorkflowType): WorkflowType | 'unknown' {
  if (!active) return draft
  return run?.workflow_type === 'agent' || run?.workflow_type === 'chat' ? run.workflow_type : 'unknown'
}
