import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api.ts'
import type { Clarification, RunStatus } from '../lib/types.ts'
import { ClarificationPanel } from './ClarificationPanel.tsx'
import './ClarificationPanel.css'

/** Fetch a past run only after the user asks; stale requests cannot update a new view. */
export function HistoricalClarifications({ runId, status }: { runId: string; status: RunStatus }) {
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState<Clarification[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const generation = useRef(0)
  useEffect(() => () => { generation.current++ }, [runId])
  async function load() {
    const token = ++generation.current
    setLoading(true); setError('')
    try {
      const result = await api.clarifications(runId)
      if (token === generation.current) setItems(result.filter(item => item.run_id === runId))
    } catch (reason) {
      if (token === generation.current) setError(reason instanceof Error ? reason.message : '历史问题加载失败。')
    } finally { if (token === generation.current) setLoading(false) }
  }
  function toggle() {
    if (!open && items === null && !loading) void load()
    setOpen(!open)
  }
  return <div className="historical-clarifications">
    <button className="historical-clarifications-toggle" type="button" aria-expanded={open} onClick={toggle}>{open ? '收起历史问答' : '查看该任务的历史问答'}</button>
    {open && <div>{loading && <p role="status">正在读取历史问题…</p>}{error && <p role="alert">{error} <button type="button" onClick={() => void load()}>重试</button></p>}{items?.length === 0 && !loading && <p>该任务没有记录澄清问题。</p>}{items && <ClarificationPanel items={items} status={status} readOnly onAnswer={async () => { throw new Error('历史记录不可回复。') }} />}</div>}
  </div>
}
