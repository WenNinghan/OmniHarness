import { useEffect, useState } from 'react'
import { Check, FolderOpen, Plus, ShieldCheck } from 'lucide-react'
import { api } from '../lib/api.ts'
import type { WorkspaceConfig } from '../lib/types.ts'
import { Button, Modal } from './ui.tsx'
import './WorkspaceTools.css'

export function WorkspacePicker({ value, onChange, disabled }: {
  value: string | null; onChange: (value: string | null) => void; disabled: boolean
}) {
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState<WorkspaceConfig[]>([])
  const [available, setAvailable] = useState(false)
  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [write, setWrite] = useState(false)
  const [approval, setApproval] = useState(false)
  const [excludes, setExcludes] = useState('node_modules, .venv, __pycache__, dist')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let alive = true
    void Promise.all([api.workspaces(), api.toolStatus()]).then(([rows, status]) => {
      if (alive) { setItems(rows); setAvailable(status.enabled && status.broker_ready) }
    }).catch(reason => { if (alive) setError(reason instanceof Error ? reason.message : '工作区读取失败') })
    return () => { alive = false }
  }, [open])
  const selected = items.find(item => item.id === value)
  async function create(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true); setError('')
    try {
      const item = await api.createWorkspace({ name, host_path: path, write_allow: write ? ['.'] : [],
        excludes: excludes.split(',').map(value => value.trim()).filter(Boolean), command_approval: approval })
      setItems(old => [...old, item]); onChange(item.id); setAdding(false); setOpen(false)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '登记未完成') }
    finally { setBusy(false) }
  }
  function choose(id: string | null) { onChange(id); setOpen(false) }
  return <>
    <button type="button" className={`workspace-trigger ${value ? 'has-workspace' : ''}`} disabled={disabled} onClick={() => setOpen(true)}>
      <FolderOpen size={16} /><strong>{selected?.name ?? (value ? '已选工作区' : '选择项目')}</strong>
      <span>{selected ? selected.write_allow.length ? '受控修改 · 快照测试' : '只读 · 快照测试' : '接入真实文件与工具'}</span>
    </button>
    {open && <Modal title="选择工作项目" onClose={() => { if (!busy) setOpen(false) }}>
      <div className="workspace-manager">
        <p className="workspace-intro">明确工作范围，再开始执行。文件通过受控补丁修改，命令与测试运行在独立快照里。</p>
        {error && <p className="tools-error" role="alert">{error}</p>}
        {!available && <p className="workspace-hint">工具服务尚未就绪，请检查宿主文件服务和沙箱配置。</p>}
        <div className="workspace-options">
          <button onClick={() => choose(null)} className={!value ? 'chosen' : ''}><FolderOpen size={19} /><span><strong>不关联项目</strong><small>使用对话中提供的材料</small></span>{!value && <Check size={18} />}</button>
          {items.map(item => <button key={item.id} className={item.id === value ? 'chosen' : ''} disabled={!item.ready || !available} onClick={() => choose(item.id)}>
            <FolderOpen size={20} /><span><strong>{item.name}<i>{item.ready ? item.write_allow.length ? '可修改' : '只读' : '未就绪'}</i></strong><small title={item.host_path}>{item.host_path}</small></span>{item.id === value && <Check size={18} />}
          </button>)}
        </div>
        <Button onClick={() => setAdding(!adding)} disabled={!available || busy}><Plus size={16} />登记新项目</Button>
        {adding && <form className="workspace-form" onSubmit={event => void create(event)}>
          <label>项目名称<input required autoFocus value={name} onChange={e => setName(e.target.value)} placeholder="例如：我的 Python 项目" /></label>
          <label>本机绝对路径<input required value={path} onChange={e => setPath(e.target.value)} placeholder="G:\Projects\我的项目" /></label>
          <label>额外排除目录或文件<small>逗号分隔；凭据文件始终排除</small><input value={excludes} onChange={e => setExcludes(e.target.value)} /></label>
          <label className="workspace-check"><input type="checkbox" checked={write} onChange={e => setWrite(e.target.checked)} /><span><strong>允许修改原项目</strong><small>允许创建和更新授权文件；修改前核对文件版本，并保留恢复材料。</small></span></label>
          <label className="workspace-check"><input type="checkbox" checked={approval} onChange={e => setApproval(e.target.checked)} /><span><strong>每条命令由我审批</strong><small>默认可自主运行离线快照命令；开启后逐条确认。</small></span></label>
          <p className="workspace-hint"><ShieldCheck size={15} />默认断网，不挂载其他项目、模型密钥或 Docker 控制接口。</p>
          <Button variant="primary" type="submit" busy={busy}>检查目录并选用</Button>
        </form>}
      </div>
    </Modal>}
  </>
}
