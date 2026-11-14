import { useState } from 'react'
import type { FormEvent } from 'react'
import { Gauge, ListOrdered, Pause, Play, Plus } from 'lucide-react'
import type { BudgetSnapshot, CommandType, Run, RunCommand, RunEvent } from '../lib/types.ts'
import { availableCommands, commandLabels, commandOutcome, outcomeLabel } from '../lib/control-state.ts'
import { Badge, Button } from './ui.tsx'

export function RunControls({ run, commands, budget, events, busy, onSubmit }: {
  run: Run
  commands: RunCommand[]
  budget: BudgetSnapshot | null
  events: RunEvent[]
  busy: boolean
  onSubmit: (type: CommandType, content?: string) => Promise<boolean>
}) {
  const agent = run.workflow_type === 'agent'
  const [requirement, setRequirement] = useState('')
  const available = availableCommands(run.status)
  async function submitRequirement(event: FormEvent) {
    event.preventDefault()
    const content = requirement.trim()
    if (!content) return
    if (await onSubmit('append_requirement', content)) setRequirement('')
  }
  const budgetRows: ReadonlyArray<readonly [string, number, number]> = budget ? [
    ['模型调用', budget.model_calls, budget.max_model_calls],
    ['工具调用', budget.tool_calls, budget.max_tool_calls],
    ['修订', budget.revisions, budget.max_revisions],
    ['活跃秒数', budget.active_seconds, budget.max_active_seconds],
  ] : []
  return <>
    <section className="detail-section">
      <h3><Gauge size={14} />运行控制</h3>
      <div className="control-actions">
        <Button size="small" variant="secondary" disabled={!available.pause || busy} onClick={() => void onSubmit('pause')}><Pause size={13} />暂停</Button>
        <Button size="small" variant="secondary" disabled={!available.resume || busy} onClick={() => void onSubmit('resume')}><Play size={13} />继续</Button>
      </div>
      <form className="control-form" onSubmit={event => void submitRequirement(event)}>
        <input aria-label="补充要求" placeholder={agent ? '补充要求（安全边界处理）' : '补充要求（仅记录）'} value={requirement} onChange={event => setRequirement(event.target.value)} disabled={!available.append || busy} />
        <Button size="small" variant="ghost" type="submit" disabled={!available.append || busy || !requirement.trim()}><Plus size={13} />添加</Button>
      </form>
      <p className="detail-hint">{agent ? '补充要求先入库，待安全边界准备纳入模型输入；提交不代表已经发送或采纳。暂停后可以继续或停止，结果不明的操作需要核对。' : '补充要求目前仅记录，不会传给模型或影响本次生成。命令先入库，由 Worker 处理；已执行过的任务没有检查点，继续会被拒绝。暂停后可以停止任务；尚未开始的任务可以继续。'}</p>
    </section>
    <section className="detail-section">
      <h3><Gauge size={14} />预算（只读）</h3>
      {budget ? <div className="budget-grid">{budgetRows.map(([label, used, cap]) => <div key={label}><span>{label}</span><strong>{used} / {cap}</strong></div>)}</div> : <p className="detail-hint">正在读取预算…</p>}
      <p className="detail-hint">{budget?.initialized ? '预算上限已冻结。' : '尚未开始计数，上限取自当前配置。'}{agent ? '仅展示运行期间实际记录的计数。' : '当前聊天执行只有模型调用接入计数，工具/修订/活跃时间尚无真实业务调用点。'}</p>
    </section>
    <section className="detail-section">
      <h3><ListOrdered size={14} />控制命令 <span className="event-count">{commands.length}</span></h3>
      {commands.length ? <div className="control-list">{commands.map(command => {
        const outcome = commandOutcome(events, command.command_id)
        const rejected = command.status === 'rejected'
        const tone: 'red' | 'green' | 'neutral' = rejected ? 'red' : command.status === 'applied' ? 'green' : 'neutral'
        const label = command.status === 'pending' ? '待执行' : outcomeLabel(command.status, outcome.reason, command.command_type, outcome.delivery)
        return <div className="control-row" key={command.command_id}><span>#{command.sequence_number}</span><strong>{commandLabels[command.command_type] ?? command.command_type}</strong><Badge tone={tone}>{label}</Badge></div>
      })}</div> : <p className="detail-hint">本次运行还没有控制命令。</p>}
    </section>
  </>
}
