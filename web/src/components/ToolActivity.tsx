import { useEffect, useRef, useState } from 'react'
import { FileText, FolderOpen, RefreshCw, ShieldCheck, Terminal } from 'lucide-react'
import { api } from '../lib/api.ts'
import type { ArtifactReference, Run, ToolApproval, ToolOperation } from '../lib/types.ts'
import { terminalStatuses } from '../lib/types.ts'
import { isUndispatchedSchemaFailure } from '../lib/tool-activity-state.ts'
import { Badge, Button } from './ui.tsx'
import './WorkspaceTools.css'

const names: Record<string, string> = { workspace_list: '查看目录', workspace_read: '读取文件', workspace_search: '搜索项目', workspace_patch: '修改文件', run_command: '运行快照命令', mcp_call: '调用 MCP 工具', artifact_read: '读取完整工件', ask_user: '向用户提问' }
const states: Record<string, string> = { requested: '已请求', authorized: '待执行', awaiting_approval: '待审批', dispatched: '已派发', running: '执行中', completed: '完成', failed: '失败', uncertain: '待核对', rejected: '已拒绝', cancelled: '已取消', approved: '已批准', pending: '待审批', expired: '已失效' }

export function ToolActivity({ run }: { run: Run }) {
  const [operations, setOperations] = useState<ToolOperation[]>([])
  const [loaded, setLoaded] = useState(false)
  const [approvals, setApprovals] = useState<ToolApproval[]>([])
  const [artifacts, setArtifacts] = useState<ArtifactReference[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const [acknowledged, setAcknowledged] = useState<Record<string, boolean>>({})
  const [notice, setNotice] = useState('')
  const reconciliationIds = useRef<Record<string, string>>({})
  const [preview, setPreview] = useState<{ id: string; name: string; content: string; next: number; size: number } | null>(null)
  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout>
    async function refresh() {
      try {
        const [tools, decisions, files] = await Promise.all([api.toolOperations(run.id), api.approvals(run.id), api.artifacts(run.id)])
        if (alive) { setOperations(tools); setApprovals(decisions); setArtifacts(files); setLoaded(true); setError('') }
      } catch (reason) { if (alive) setError(reason instanceof Error ? reason.message : '工具记录读取失败') }
      if (alive && !terminalStatuses.has(run.status)) timer = setTimeout(() => void refresh(), 2000)
    }
    void refresh()
    return () => { alive = false; clearTimeout(timer) }
  }, [run.id, run.status, revision])
  async function decide(item: ToolApproval, decision: 'approve' | 'reject') {
    setBusy(item.id); setError('')
    try { await api.decideApproval(item.id, item.fingerprint, decision); setRevision(value => value + 1) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '审批未保存') }
    finally { setBusy(null) }
  }
  async function showArtifact(file: ArtifactReference, offset = 0) {
    setBusy(file.id)
    try {
      const response = await fetch(`/api/v1/artifacts/${file.id}/content?offset=${offset}&limit=32768`, { credentials: 'same-origin' })
      if (!response.ok) throw new Error('工件读取失败，请重试。')
      const content = await response.text()
      setPreview({ id: file.id, name: file.name, content, next: Number(response.headers.get('X-Artifact-Next-Offset')), size: file.size_bytes })
    } catch (reason) { setError(reason instanceof Error ? reason.message : '工件读取失败') }
    finally { setBusy(null) }
  }
  async function reconcile(operation: ToolOperation) {
    setBusy(operation.id); setError(''); setNotice('')
    const requestId = reconciliationIds.current[operation.id] ??= crypto.randomUUID()
    try {
      const result = await api.reconcileTool(operation.id, operation.binding_fingerprint, requestId)
      setNotice(result.workspace_released ? '核对记录已保存，工作区已释放。该操作仍标记为未验证。' : '核对记录已保存；其他未核对操作仍占用工作区。')
      setRevision(value => value + 1)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '核对未完成，工作区仍保持占用') }
    finally { setBusy(null) }
  }
  const pending = approvals.filter(item => item.status === 'pending')
  if (!run.workspace && !artifacts.length && !error) return null
  return <section className="detail-section tool-activity">
    <h3><FolderOpen size={16} />{run.workspace?.name ?? '工具与工件'}</h3>
    {run.workspace && <p className="tool-caption">授权版本 {run.workspace.policy_version} · 原项目受控补丁 · 命令只在快照运行</p>}
    {error && <div className="tools-error" role="alert">{error}<Button size="small" variant="ghost" onClick={() => setRevision(n => n + 1)}><RefreshCw size={13} />重试</Button></div>}
    {notice && <p className="tool-caption" role="status">{notice}</p>}
    {pending.map(item => <article className="approval-card" key={item.id}>
      <div className="tool-card-heading"><ShieldCheck size={18} /><strong>需要你的决定</strong><Badge tone="amber">待审批</Badge></div>
      <p>{names[item.operation.tool_name] ?? item.operation.tool_name}</p>
      <pre>{JSON.stringify(item.operation.parameters, null, 2)}</pre>
      <small>操作 {item.operation_id.slice(0, 12)} · 已绑定参数与项目版本</small>
      {run.status === 'paused' && <p className="tool-caption">任务仍处于暂停状态；批准后需继续任务才会执行。</p>}
      <div className="approval-actions"><Button variant="primary" busy={busy === item.id} disabled={Boolean(busy)} onClick={() => void decide(item, 'approve')}>批准此操作</Button><Button disabled={Boolean(busy)} onClick={() => void decide(item, 'reject')}>拒绝</Button></div>
    </article>)}
    {run.workspace && <div className="tool-card-heading tool-list-heading"><Terminal size={16} /><strong>实际工具调用</strong><span>{loaded ? operations.length : '…'}</span></div>}
    {run.workspace && !operations.length && !error && <p className="tool-caption">{!loaded ? '正在读取工具记录…' : terminalStatuses.has(run.status) ? '本次任务未记录工具调用。' : '目前尚未记录工具调用。'}</p>}
    {operations.map(operation => <details className="tool-record" key={operation.id}>
      <summary><span>{names[operation.tool_name] ?? operation.tool_name}</span><Badge tone={operation.error_code || ['failed', 'uncertain'].includes(operation.status) ? 'red' : operation.status === 'completed' ? 'green' : 'neutral'}>{isUndispatchedSchemaFailure(operation) ? '参数无效 · 未执行' : operation.status === 'reconciled' ? '已核对 · 未验证' : operation.status === 'completed' && operation.error_code ? '已执行 · 返回错误' : states[operation.status] ?? operation.status}</Badge></summary>
      <pre>{JSON.stringify(operation.parameters, null, 2)}</pre>
      {operation.result && <><p className="tool-caption">实际结果</p><pre>{JSON.stringify(operation.result, null, 2)}</pre></>}
      {operation.snapshot_id && <small>测试快照：{operation.snapshot_id}</small>}
      {operation.error_code && <p className="tools-error">{operation.error_code}</p>}
      {['uncertain', 'running', 'dispatched'].includes(operation.status) && terminalStatuses.has(run.status) && ['workspace_patch', 'run_command'].includes(operation.tool_name) && <div className="reconciliation-box">
        <label className="workspace-check"><input type="checkbox" checked={acknowledged[operation.id] ?? false} onChange={event => setAcknowledged(value => ({ ...value, [operation.id]: event.target.checked }))} /><span>我理解核对不会重跑操作，也不代表测试或任务成功。</span></label>
        <p className="tool-caption">只有确认原沙箱已回收，或补丁回执与当前文件版本一致，才会释放该操作的占用。</p>
        <Button size="small" disabled={!acknowledged[operation.id] || Boolean(busy)} onClick={() => void reconcile(operation)}>核对并释放占用</Button>
      </div>}
    </details>)}
    {approvals.some(item => item.status !== 'pending') && <details className="tool-record"><summary><ShieldCheck size={15} />审批记录</summary>{approvals.filter(item => item.status !== 'pending').map(item => <p key={item.id} className="tool-caption">{names[item.operation.tool_name] ?? item.operation.tool_name} · {states[item.status] ?? item.status}</p>)}</details>}
    <div className="tool-card-heading tool-list-heading"><FileText size={16} /><strong>保存的工件</strong><span>{artifacts.length}</span></div>
    <div className="artifact-list">{artifacts.map(file => <button key={file.id} onClick={() => void showArtifact(file)} disabled={busy === file.id}><FileText size={15} /><span><strong>{file.name}</strong><small>{file.kind === 'context_summary' ? file.metadata_json.output_status === 'complete' ? '上下文摘要' : '摘要未采用 · 已保留原记录' : file.kind} · {(file.size_bytes / 1024).toFixed(1)} KB</small></span></button>)}</div>
    {preview && <div className="artifact-preview"><strong>{preview.name}</strong><pre>{preview.content}</pre><div><Button size="small" onClick={() => setPreview(null)}>收起</Button>{preview.next < preview.size && <Button size="small" onClick={() => { const file = artifacts.find(item => item.id === preview.id); if (file) void showArtifact(file, preview.next) }}>下一页</Button>}</div></div>}
  </section>
}
