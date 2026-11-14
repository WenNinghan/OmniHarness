import type { AgentStrategy } from '../lib/types.ts'
import { strategyLabels } from '../lib/collaboration-state.ts'
import './CollaborationMode.css'

export function CollaborationMode({ value, onChange, disabled }: {
  value: AgentStrategy; onChange: (value: AgentStrategy) => void; disabled: boolean
}) {
  return <div className="collaboration-mode"><div className="collaboration-options" role="group" aria-label="协作方式">
    {(['single', 'compact', 'staged'] as const).map(item => <button key={item} type="button" aria-pressed={value === item}
      disabled={disabled} onClick={() => onChange(item)}>{strategyLabels[item]}</button>)}
  </div><span>{value === 'single' ? '使用下方模型，自主完成读、改、测。' : value === 'compact'
    ? '规划 → 执行 → 审查；任务分配由程序校验。' : '思考 → 规划 → 分配 → 执行 → 审查。'}</span></div>
}
