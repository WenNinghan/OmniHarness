import { stageLabels } from './types.ts'
import type { AgentRun, RunEvent } from './types.ts'

export interface StageTimelineNode {
  id: string
  role: string
  label: string
  modelName: string
  selectionSource: string | null
  status: string
  durationMs: number | null
  durationLabel: string
  widthPct: number
}

export interface StageTimelineSummary {
  nodes: StageTimelineNode[]
  totalDurationMs: number | null
  totalDurationLabel: string
  revisionRounds: number
  revisionSkippedReasons: string[]
}

export function formatDuration(ms: number | null, status?: string): string {
  if (ms === null || !Number.isFinite(ms) || ms < 0) {
    return status === 'running' ? '进行中' : '未知'
  }
  if (ms < 1000) {
    return `${Math.round(ms)} ms`
  }
  if (ms < 60_000) {
    return `${(ms / 1000).toFixed(1)} s`
  }
  const totalSeconds = Math.round(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes} m ${seconds} s`
}

function nodeDurationMs(node: AgentRun): number | null {
  if (!node.started_at || !node.finished_at) {
    return null
  }
  const start = Date.parse(node.started_at)
  const end = Date.parse(node.finished_at)
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    return null
  }
  return end - start
}

export function buildStageTimeline(agentRuns: AgentRun[], events: RunEvent[]): StageTimelineSummary {
  const durations = agentRuns.map(nodeDurationMs)
  const knownDurations = durations.filter((value): value is number => value !== null)
  const maxDurationMs = knownDurations.length ? Math.max(...knownDurations) : 0
  const totalDurationMs = knownDurations.length ? knownDurations.reduce((sum, value) => sum + value, 0) : null

  const nodes: StageTimelineNode[] = agentRuns.map((node, index) => {
    const durationMs = durations[index]
    const widthPct =
      durationMs === null
        ? 0
        : maxDurationMs <= 0
          ? 12
          : Math.max(8, Math.round((durationMs / maxDurationMs) * 100))
    return {
      id: node.id,
      role: node.role,
      label: stageLabels[node.role as keyof typeof stageLabels] ?? node.role,
      modelName: node.model_name || '模型未知',
      selectionSource: node.selection_source ?? null,
      status: node.status,
      durationMs,
      durationLabel: formatDuration(durationMs, node.status),
      widthPct,
    }
  })

  const revisionRounds = events.filter(event => event.event_type === 'agent.revision_triggered').length
  const revisionSkippedReasons = events
    .filter(event => event.event_type === 'agent.revision_skipped' && typeof event.payload.reason === 'string')
    .map(event => String(event.payload.reason))

  return {
    nodes,
    totalDurationMs,
    totalDurationLabel: formatDuration(totalDurationMs),
    revisionRounds,
    revisionSkippedReasons,
  }
}
