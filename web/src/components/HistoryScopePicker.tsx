import { useEffect, useMemo, useState } from 'react'
import { Layers3, ShieldCheck } from 'lucide-react'
import { api } from '../lib/api.ts'
import type { Message, WorkspaceConfig, WorkflowType } from '../lib/types.ts'
import { Button, Modal } from './ui.tsx'
import './HistoryScopePicker.css'

const terminal = new Set(['completed', 'cancelled', 'failed', 'interrupted'])

export function HistoryScopePicker({ messages, workflowType, workspaceId, chatWorkspaceId, onChatWorkspaceChange,
  additionalRunIds, onAdditionalChange, modelLabel, disabled }: {
  messages: Message[]; workflowType: WorkflowType; workspaceId: string | null; chatWorkspaceId: string | null
  onChatWorkspaceChange: (id: string | null) => void; additionalRunIds: string[]
  onAdditionalChange: (ids: string[]) => void; modelLabel: string; disabled: boolean
}) {
  const [open, setOpen] = useState(false)
  const [workspaces, setWorkspaces] = useState<WorkspaceConfig[]>([])
  const [workspacesLoaded, setWorkspacesLoaded] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let alive = true
    void api.workspaces().then(rows => { if (alive) { setWorkspaces(rows); setWorkspacesLoaded(true); setError('') } })
      .catch(() => { if (alive) setError('项目列表暂不可用，可稍后重试。') })
    return () => { alive = false }
  }, [open])
  const scopeId = workflowType === 'agent' ? workspaceId : chatWorkspaceId
  const selectedWorkspace = workspaces.find(item => item.id === scopeId)
  const unavailableScope = Boolean(scopeId && workspacesLoaded && !selectedWorkspace?.enabled)
  const sources = useMemo(() => {
    const grouped = new Map<string, Message>()
    for (const message of messages) {
      if (!message.run_id || !message.run_status || !terminal.has(message.run_status)) continue
      if (!grouped.has(message.run_id) || message.role === 'user') grouped.set(message.run_id, message)
    }
    return [...grouped.values()].map(message => {
      const scope = message.history_scope
      const sourceWorkspace = scope?.kind === 'workspace' ? scope.workspace_id : !scope ? message.workspace_id : null
      const legacy = !scope && !sourceWorkspace
      const defaultIncluded = scopeId ? sourceWorkspace === scopeId : scope?.kind === 'unscoped'
      const label = legacy ? '未归属的旧历史' : sourceWorkspace
        ? workspaces.find(item => item.id === sourceWorkspace)?.name ?? '已登记项目'
        : '无项目历史'
      return { id: message.run_id!, preview: message.content, label, legacy, defaultIncluded }
    }).reverse()
  }, [messages, scopeId, workspaces])
  const extraSources = sources.filter(source => !source.defaultIncluded)
  const legacyIds = extraSources.filter(source => source.legacy).map(source => source.id)
  function toggle(id: string) {
    if (!additionalRunIds.includes(id) && additionalRunIds.length >= 100) {
      setError('最多选入 100 个历史来源，请先取消部分选择。'); return
    }
    setError('')
    onAdditionalChange(additionalRunIds.includes(id) ? additionalRunIds.filter(item => item !== id) : [...additionalRunIds, id])
  }
  function includeLegacy() {
    const combined = [...new Set([...additionalRunIds, ...legacyIds])]
    if (combined.length > 100) {
      setError('未归属历史超过 100 个来源，请手动选择需要的内容。'); return
    }
    setError(''); onAdditionalChange(combined)
  }
  return <>
    <button type="button" className="history-scope-trigger" disabled={disabled} onClick={() => setOpen(true)}>
      <Layers3 size={16} /><span>历史范围</span><strong>{unavailableScope ? '项目不可用' : scopeId ? selectedWorkspace?.name ?? '当前项目' : '无项目'}</strong>
      {additionalRunIds.length > 0 && <small>+{additionalRunIds.length} 条来源</small>}
    </button>
    {open && <Modal title="本次使用的历史" onClose={() => setOpen(false)}>
      <div className="history-scope-panel">
        <p>默认只允许带入当前范围的同会话历史。你可以明确选入其他来源；对话中显示的历史不会被删除。</p>
        {error && <p className="history-scope-error" role="alert">{error}</p>}
        {workflowType === 'chat' ? <label className="history-scope-field">历史范围
          <select value={chatWorkspaceId ?? ''} onChange={event => onChatWorkspaceChange(event.target.value || null)}>
            <option value="">无项目</option>
            {unavailableScope && <option value={chatWorkspaceId ?? ''} disabled>{selectedWorkspace?.name ?? '原项目'}（已停用或不存在）</option>}
            {workspaces.filter(item => item.enabled).map(item => <option value={item.id} key={item.id}>{item.name}</option>)}
          </select>
        </label> : <div className="history-scope-current"><span>当前项目</span><strong>{scopeId ? selectedWorkspace?.name ?? '已选项目' : '未选择项目'}</strong></div>}
        {unavailableScope && <p className="history-scope-error" role="alert">当前项目已停用或不存在。请{workflowType === 'chat' ? '在上方切换历史范围' : '重新选择工作项目'}后发送。</p>}
        <p className="history-scope-safety"><ShieldCheck size={16} />历史范围只决定可带入的消息，不授予文件、命令或工具权限。</p>
        <div className="history-scope-heading"><strong>手动选入其他历史</strong>{legacyIds.length > 0 && <button type="button" onClick={includeLegacy}>选入全部未归属历史</button>}</div>
        <p className="history-scope-note">只显示本会话中已结束、默认未选入的来源。选中后可供 {modelLabel || '所选模型'} 使用；实际带入内容可能受上下文预算和摘要影响。</p>
        <div className="history-scope-sources">
          {extraSources.map(source => <label key={source.id} className="history-scope-source">
            <input type="checkbox" checked={additionalRunIds.includes(source.id)} onChange={() => toggle(source.id)} />
            <span><strong>{source.label}</strong><small>{source.preview.trim().slice(0, 100) || '此来源没有可预览的文字'}</small></span>
          </label>)}
          {!extraSources.length && <p className="history-scope-empty">当前没有其他可选历史。</p>}
        </div>
        <div className="history-scope-actions"><Button variant="primary" onClick={() => setOpen(false)}>完成</Button></div>
      </div>
    </Modal>}
  </>
}
