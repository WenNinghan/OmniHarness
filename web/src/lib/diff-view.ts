import type { ToolOperation } from './types.ts'

export type DiffLineKind = 'meta' | 'hunk' | 'add' | 'del' | 'context'

export interface DiffLine {
  kind: DiffLineKind
  text: string
}

export interface ParsedDiff {
  lines: DiffLine[]
  additions: number
  deletions: number
  truncated: boolean
  totalLines: number
}

export interface PatchDiffInfo {
  path: string
  diff: string | null
  beforeSha: string | null
  afterSha: string | null
  recoveryId: string | null
  truncatedInResult: boolean
  rollback: { action: string; restoredSha: string | null } | null
}

export function parseUnifiedDiff(diffText: string, maxLines = 400): ParsedDiff {
  if (!diffText) {
    return { lines: [], additions: 0, deletions: 0, truncated: false, totalLines: 0 }
  }
  const rawLines = diffText.split(/\r?\n/)
  if (rawLines.length > 0 && rawLines[rawLines.length - 1] === '') {
    rawLines.pop()
  }
  const limit = Math.max(0, maxLines)
  const lines: DiffLine[] = []
  let additions = 0
  let deletions = 0
  for (const text of rawLines) {
    let kind: DiffLineKind = 'context'
    if (text.startsWith('--- ') || text.startsWith('+++ ')) {
      kind = 'meta'
    } else if (text.startsWith('@@')) {
      kind = 'hunk'
    } else if (text.startsWith('+')) {
      kind = 'add'
      additions += 1
    } else if (text.startsWith('-')) {
      kind = 'del'
      deletions += 1
    }
    if (lines.length < limit) {
      lines.push({ kind, text })
    }
  }
  return {
    lines,
    additions,
    deletions,
    truncated: rawLines.length > limit,
    totalLines: rawLines.length,
  }
}

export function extractPatchDiff(operation: ToolOperation): PatchDiffInfo | null {
  if (
    operation.tool_name !== 'workspace_patch' ||
    !operation.result ||
    typeof operation.result !== 'object' ||
    Array.isArray(operation.result)
  ) {
    return null
  }
  const result = operation.result
  if (result.error) {
    return null
  }
  const path =
    typeof result.path === 'string'
      ? result.path
      : typeof operation.parameters?.path === 'string'
        ? operation.parameters.path
        : ''
  const diff = typeof result.diff === 'string' ? result.diff : null
  const beforeSha = typeof result.before_sha256 === 'string' ? result.before_sha256 : null
  const afterSha = typeof result.sha256 === 'string' ? result.sha256 : null
  const recoveryId = typeof result.recovery_id === 'string' ? result.recovery_id : null
  const truncatedInResult = result.truncated === true
  const rawRollback =
    result.rollback && typeof result.rollback === 'object' && !Array.isArray(result.rollback)
      ? (result.rollback as Record<string, unknown>)
      : null
  const rollback =
    rawRollback && typeof rawRollback.action === 'string'
      ? {
          action: rawRollback.action,
          restoredSha: typeof rawRollback.restored_sha256 === 'string' ? rawRollback.restored_sha256 : null,
        }
      : null
  return { path, diff, beforeSha, afterSha, recoveryId, truncatedInResult, rollback }
}

export function compoundMaskingWarning(operation: ToolOperation): string | null {
  if (
    operation.tool_name !== 'run_command' ||
    !operation.result ||
    typeof operation.result !== 'object' ||
    Array.isArray(operation.result)
  ) {
    return null
  }
  const result = operation.result
  if (result.exit_code_scope !== 'terminal_segment_only' || !Array.isArray(result.compound_exit_masking)) {
    return null
  }
  const operators = result.compound_exit_masking.filter(
    (item): item is string => typeof item === 'string' && item.length > 0,
  )
  if (!operators.length) {
    return null
  }
  return `复合命令包含 ${operators.join('、')} ，退出码仅反映末段命令，不代表前序子命令全部成功。`
}
