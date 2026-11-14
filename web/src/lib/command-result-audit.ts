/** Present explicit conflicts and incomplete checks from the host audit.
 * Missing, unknown, or unrecognised metadata is not a passed verification.
 */
export interface CommandResultConflict {
  artifactId: string
  claimedExitCode: number
  actualExitCode: number
}

export type CommandResultAuditWarning =
  | { kind: 'conflict'; conflicts: CommandResultConflict[]; truncated: boolean }
  | { kind: 'incomplete'; conflicts: []; truncated: boolean }

const artifactId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)

export function commandResultAuditWarning(value: unknown): CommandResultAuditWarning | null {
  if (!object(value)) return null
  if (value.status === 'incomplete') return { kind: 'incomplete', conflicts: [], truncated: value.truncated === true }
  if (value.status !== 'conflict') return null
  const entries = Array.isArray(value.conflicts) ? value.conflicts : []
  const conflicts = entries.flatMap(entry => {
    if (!object(entry) || typeof entry.artifact_id !== 'string' || !artifactId.test(entry.artifact_id) ||
        !Number.isSafeInteger(entry.claimed_exit_code) || !Number.isSafeInteger(entry.actual_exit_code) ||
        entry.claimed_exit_code === entry.actual_exit_code) return []
    return [{ artifactId: entry.artifact_id, claimedExitCode: entry.claimed_exit_code as number,
      actualExitCode: entry.actual_exit_code as number }]
  })
  return { kind: 'conflict', conflicts: conflicts.slice(0, 10), truncated: value.truncated === true || conflicts.length > 10 }
}

/** Preserve the host notice when a reader copies an answer. */
export function commandResultAuditCopyText(answer: string, value: unknown): string {
  const warning = commandResultAuditWarning(value)
  if (!warning) return answer
  if (warning.kind === 'incomplete') {
    return `【命令退出码核对未完成】部分内容或证据超出本次核对范围，请查看工具记录。\n\n${answer}`
  }
  const lines = ['【命令退出码核对提醒】回答中的命令退出码与实际工具记录不一致，请查看完整工具记录。',
    ...warning.conflicts.map(item => `工件 ${item.artifactId}：回答写退出码 ${item.claimedExitCode}，工具记录为 ${item.actualExitCode}。`)]
  if (warning.truncated) lines.push('核对范围或展示数量有限，请查看完整工具记录。')
  return `${lines.join('\n')}\n\n${answer}`
}
