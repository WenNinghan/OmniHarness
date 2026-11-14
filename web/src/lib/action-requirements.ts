import type { ActionRequirement } from './types.ts'

export type ActionDraft = { kind: 'workspace_file_write' | 'sandbox_command'; path: string; argv: string; cwd: string; step_id?: string; client_id?: string; depends_on?: string[] }

export function parseActionDrafts(drafts: ActionDraft[]): ActionRequirement[] {
  if (drafts.length > 1) {
    const ids = drafts.map(item => item.client_id)
    if (ids.some(id => !id) || new Set(ids).size !== ids.length || drafts.some(item => !Array.isArray(item.depends_on))) throw new Error('请为每个动作明确设置依赖关系。')
    if (drafts.length === 2 && (drafts.reduce((count, item) => count + (item.depends_on?.length ?? 0), 0) !== 1 || drafts.some(item => item.depends_on?.some(id => !ids.includes(id) || id === item.client_id)))) throw new Error('请明确两个动作的先后顺序。')
  }
  return drafts.map(item => {
    const dependency = drafts.length > 1 ? { client_id: item.client_id!, depends_on: item.depends_on! } : {}
    if (item.kind === 'workspace_file_write') {
      if (!item.path.trim()) throw new Error('请填写文件的项目相对路径。')
      return { kind: item.kind, path: item.path.trim(), ...(item.step_id ? { step_id: item.step_id } : {}), ...dependency }
    }
    let argv: unknown
    try { argv = JSON.parse(item.argv) } catch { throw new Error('命令参数请填写 JSON 字符串数组，例如 ["python", "-m", "pytest"]。') }
    if (!Array.isArray(argv) || !argv.length || argv.some(value => typeof value !== 'string' || !value)) throw new Error('命令参数必须是非空字符串数组。')
    return { kind: item.kind, argv: argv as string[], cwd: item.cwd.trim() || '.', ...(item.step_id ? { step_id: item.step_id } : {}), ...dependency }
  })
}
