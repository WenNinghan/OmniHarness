import { useEffect, useState } from 'react'
import { ClipboardCheck } from 'lucide-react'
import { api } from '../lib/api.ts'
import type { ActionContract, Run } from '../lib/types.ts'
import { ActionRequirements } from './ActionRequirements.tsx'
import { parseActionDrafts } from '../lib/action-requirements.ts'
import type { ActionDraft } from '../lib/action-requirements.ts'

const statusLabels: Record<string, string> = { NOT_ASSESSED: '尚未评估', UNVERIFIED: '证据不足，待核对', BLOCKED: '受阻', ACTIONS_RECORDED: '已记录所需动作' }
const reasonLabels: Record<string, string> = { no_actions: '没有可核对的动作要求', invalid_actions: '动作要求格式无法核对', invalid_action_scope: '动作范围无法核对', no_matching_record: '尚无匹配的工具记录', task_scope_unavailable: '缺少可靠的子任务关联', host_record_matched: '已匹配宿主工具记录', pending_or_unknown_call: '存在未结束或结果未知的调用', evidence_incomplete: '现有证据不足', host_records_only: '已记录动作，目标是否达成仍需核对' }
const reasonText = (reason: string) => reasonLabels[reason] ?? '详情暂不可用，请查看运行记录'

export function ActionOutcomePanel({ run }: { run: Run }) {
  const [contract, setContract] = useState<ActionContract | null>(run.action_contract ?? null)
  const [editing, setEditing] = useState(false)
  const [drafts, setDrafts] = useState<ActionDraft[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { setContract(run.action_contract ?? null) }, [run.action_contract])
  if (run.workflow_version !== 'stage-agent-v10') return null
  const outcome = run.action_outcome
  const startEdit = () => {
    setDrafts((contract?.explicit.actions ?? []).map(item => item.kind === 'workspace_file_write'
      ? { kind: item.kind, path: item.path, argv: '', cwd: '.', step_id: item.step_id, client_id: item.client_id, depends_on: item.depends_on }
      : { kind: item.kind, path: '', argv: JSON.stringify(item.argv), cwd: item.cwd, step_id: item.step_id, client_id: item.client_id, depends_on: item.depends_on }))
    setError(''); setEditing(true)
  }
  const save = async () => {
    if (!contract || busy) return
    try {
      setBusy(true); setError('')
      const requirements = parseActionDrafts(drafts)
      setContract(await api.updateActionRequirements(run.id, contract.version, requirements))
      setEditing(false)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败，请刷新运行状态。') }
    finally { setBusy(false) }
  }
  return <section className="detail-section"><h3><ClipboardCheck size={14} />动作与证据</h3>
    <p className="detail-hint">{outcome ? statusLabels[outcome.status] ?? '状态未知' : '运行结束后显示动作记录。'} 动作记录仅表示工具事实，不代表目标已验证。</p>
    {outcome?.reason && <p className="detail-hint">{reasonText(outcome.reason)}</p>}
    {outcome?.actions.map(item => { const candidate = run.action_candidates?.find(value => value.id === item.id); const description = candidate?.kind === 'workspace_file_write' ? candidate.path : candidate?.kind === 'sandbox_command' ? candidate.argv.join(' ') : '参数未记录'; return <div className="action-entry" key={item.id}><strong>{item.kind === 'workspace_file_write' ? '项目文件' : '快照命令'} · {statusLabels[item.status] ?? '状态未知'}</strong><span>{description}</span><small>{item.source === 'user' ? '用户明确要求' : '规划草案，语义未确认'} · 工具记录 {item.operation_ids?.length ?? 0} 条</small>{item.reason && <small>{reasonText(item.reason)}</small>}</div> })}
    {!outcome && run.action_candidates?.map(item => <div className="action-entry" key={item.id}><strong>{item.kind === 'workspace_file_write' ? '项目文件' : '快照命令'} · 待核对</strong><span>{item.kind === 'workspace_file_write' ? item.path : item.argv.join(' ')}</span><small>{item.source === 'user' ? '用户明确要求' : '规划草案，语义未确认'}</small></div>)}
    {contract && <p className="detail-hint">自主纠错：{contract.auto_correction_enabled ? '允许（执行功能尚未启用）' : '关闭'} · 要求版本 {contract.version}</p>}
    {run.status === 'queued' && !editing && <button type="button" className="action-add" onClick={startEdit}>修改明确的动作要求</button>}
    {editing && contract && <><ActionRequirements enabled={contract.auto_correction_enabled} onEnabled={() => {}} drafts={drafts} onDrafts={setDrafts} disabled={busy} showToggle={false} initiallyOpen /><p className="detail-hint">纠错开关在任务创建时冻结；这里只修改明确动作。</p><button type="button" className="action-add" disabled={busy} onClick={() => void save()}>保存要求</button> <button type="button" className="action-add" disabled={busy} onClick={() => setEditing(false)}>取消</button></>}
    {error && <p className="detail-hint" role="alert">{error}</p>}
  </section>
}
