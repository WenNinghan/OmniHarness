import type { ActionDraft } from '../lib/action-requirements.ts'
import './ActionRequirements.css'

export function ActionRequirements({ enabled, onEnabled, drafts, onDrafts, disabled, showToggle = true, initiallyOpen = false }: { enabled: boolean; onEnabled: (value: boolean) => void; drafts: ActionDraft[]; onDrafts: (value: ActionDraft[]) => void; disabled: boolean; showToggle?: boolean; initiallyOpen?: boolean }) {
  const update = (index: number, value: Partial<ActionDraft>) => onDrafts(drafts.map((item, i) => i === index ? { ...item, ...value } : item))
  return <details className="action-requirements" open={initiallyOpen || undefined}>
    <summary>动作要求与自主纠错 <span>可选</span></summary>
    <p>可明确需要写入的文件或在快照中运行的命令。填写要求不会扩大项目权限。</p>
    {showToggle && <><label className="action-toggle"><input type="checkbox" checked={enabled} disabled={disabled} onChange={event => onEnabled(event.target.checked)} />允许最多一次有条件自主纠错</label>
    <p>纠错执行尚在开发，当前不会自动补做；结果不明时等待核对。</p></>}
    {drafts.map((item, index) => <div className="action-entry" key={item.client_id ?? index}>
      <div className="action-entry-head"><select aria-label={`动作 ${index + 1} 类型`} value={item.kind} disabled={disabled} onChange={event => update(index, { kind: event.target.value as ActionDraft['kind'] })}><option value="workspace_file_write">写入项目文件</option><option value="sandbox_command">运行快照命令</option></select><button type="button" disabled={disabled} onClick={() => onDrafts(drafts.filter((_, i) => i !== index))}>移除</button></div>
      {item.kind === 'workspace_file_write' ? <input aria-label={`动作 ${index + 1} 文件路径`} placeholder="项目相对路径，例如 src/app.py" value={item.path} disabled={disabled} onChange={event => update(index, { path: event.target.value })} /> : <><input aria-label={`动作 ${index + 1} 命令参数`} placeholder={'参数数组，例如 ["python", "-m", "pytest"]'} value={item.argv} disabled={disabled} onChange={event => update(index, { argv: event.target.value })} /><input aria-label={`动作 ${index + 1} 工作目录`} placeholder="快照内相对工作目录，默认 ." value={item.cwd} disabled={disabled} onChange={event => update(index, { cwd: event.target.value })} /></>}
      {drafts.length === 2 && <label className="action-toggle"><input type="checkbox" checked={(item.depends_on ?? []).includes(drafts[1 - index].client_id ?? '')} disabled={disabled} onChange={event => update(index, { depends_on: event.target.checked ? [drafts[1 - index].client_id!] : [] })} />在动作 {2 - index} 成功后执行</label>}
    </div>)}
    <button type="button" className="action-add" disabled={disabled || drafts.length >= 2} onClick={() => onDrafts([...drafts.map(item => ({ ...item, client_id: item.client_id ?? crypto.randomUUID(), depends_on: item.depends_on ?? [] })), { kind: 'workspace_file_write', path: '', argv: '', cwd: '.', client_id: crypto.randomUUID(), depends_on: [] }])}>＋ 添加动作要求</button>
  </details>
}
