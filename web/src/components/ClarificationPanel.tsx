import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { CheckCircle2, MessageCircleQuestion } from 'lucide-react'
import type { Clarification, RunStatus } from '../lib/types.ts'
import { CLARIFICATION_ANSWER_MAX_LENGTH, answerRequest, canAnswerInStatus, clarificationPendingNotice } from '../lib/clarification-state.ts'
import { Button } from './ui.tsx'
import './ClarificationPanel.css'

function QuestionCard({ item, status, readOnly, onAnswer }: {
  item: Clarification
  status: RunStatus
  readOnly: boolean
  onAnswer: (item: Clarification, answer: string, requestId: string) => Promise<void>
}) {
  const enabled = !readOnly && canAnswerInStatus(status)
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // Keep the same key when retrying an uncertain request with identical input.
  const pending = useRef<{ value: string; id: string } | null>(null)
  async function submit(event: FormEvent) {
    event.preventDefault()
    const value = answer.trim()
    if (!value || busy || !enabled) return
    pending.current = answerRequest(value, pending.current, () => crypto.randomUUID())
    setBusy(true); setError('')
    try { await onAnswer(item, value, pending.current.id) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '提交失败，请重试。') }
    finally { setBusy(false) }
  }
  return <section className={`clarification-card ${item.status !== 'pending' ? 'clarification-settled' : ''}`} aria-label="任务澄清">
    <div className="clarification-title"><span className="clarification-icon">{item.status === 'answered' ? <CheckCircle2 size={17} /> : <MessageCircleQuestion size={17} />}</span><div><strong>{item.status === 'pending' ? '需要你的回复' : item.status === 'answered' ? '已回复' : '问题已取消'}</strong><small>{item.status === 'pending' ? '任务会等待你的回答，再继续相关步骤。' : '这条记录属于当前任务。'}</small></div></div>
    <p className="clarification-question">{item.question}</p>
    {item.reason && <p className="clarification-reason">{item.reason}</p>}
    {item.status === 'answered' && <div className="clarification-answer"><span>你的回复</span><p>{item.answer}</p></div>}
    {item.status === 'pending' && <form onSubmit={event => void submit(event)}>
      {item.options.length > 0 && <div className="clarification-options" aria-label="建议选项">{item.options.map((option, index) => <button key={`${index}-${option}`} type="button" disabled={busy || !enabled} className={answer === option ? 'selected' : ''} onClick={() => setAnswer(option)}>{option}</button>)}</div>}
      <label htmlFor={`clarification-${item.id}`}>你的回答</label>
      <textarea id={`clarification-${item.id}`} value={answer} onChange={event => setAnswer(event.target.value)} disabled={busy || !enabled} placeholder="选择上面的建议，或输入你的回答…" rows={3} maxLength={CLARIFICATION_ANSWER_MAX_LENGTH} />
      {error && <p className="clarification-error" role="alert">{error}</p>}
      {!enabled && <p className="clarification-reason">{readOnly ? '历史记录仅供查看，请在当前任务中回复。' : clarificationPendingNotice(status)}</p>}
      <div className="clarification-actions"><Button type="submit" variant="primary" size="small" busy={busy} disabled={!enabled || busy || !answer.trim()}>提交回复</Button></div>
    </form>}
  </section>
}

export function ClarificationPanel({ items, status, readOnly = false, onAnswer }: {
  items: Clarification[]; status: RunStatus; readOnly?: boolean
  onAnswer: (item: Clarification, answer: string, requestId: string) => Promise<void>
}) {
  if (!items.length) return null
  return <div className="clarification-list">{items.map(item => <QuestionCard key={item.id} item={item} status={status} readOnly={readOnly} onAnswer={onAnswer} />)}</div>
}
