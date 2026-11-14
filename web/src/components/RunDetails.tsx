import { Activity, ArrowUpRight, CheckCircle2, Circle, Clock3, Coins, FlaskConical, GitBranch, Hash, PanelRightClose, Radio, ShieldCheck, XCircle } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ActionOutcomePanel } from './ActionOutcome.tsx'
import type { ReactNode } from 'react'
import type { AgentRun, EvidenceViewItem, EvidenceViewPage, Run, RunEvent } from '../lib/types.ts'
import { api } from '../lib/api.ts'
import { statusLabels, terminalStatuses, stageLabels } from '../lib/types.ts'
import { Badge, Button } from './ui.tsx'
import { ToolActivity } from './ToolActivity.tsx'
import { RunObservation } from './RunObservation.tsx'
import { strategyLabels } from '../lib/collaboration-state.ts'

const eventLabels: Record<string, string> = { 'run.created': '任务已创建', 'run.started': '开始运行', 'message.delta': '接收流式内容', 'run.completed': '生成完成', 'run.cancelled': '运行已停止', 'run.failed': '运行失败', 'run.interrupted': '运行中断', 'run.paused': '运行已暂停', 'run.pending_review': '等待核对', 'run.command': '控制命令已处理', 'agent.delegated': '已委派', 'agent.model_started': '模型调用开始', 'agent.model_completed': '模型调用完成', 'agent.model_uncertain': '模型调用待核对', 'agent.stage_result': '阶段产物已保存' }
const time = (value: string) => new Date(value).toLocaleTimeString('zh-CN', { hour12: false })
const nodeLabels: Record<string, string> = { running: '运行中', completed: '已完成', pending_review: '待核对', failed: '失败', cancelled: '已停止' }
const evidenceReasons: Record<string, string> = {
  HOST_RECORD_AND_INPUT_MATCH: '引用与宿主记录匹配',
  NOT_IN_BOUND_INPUT_OR_UNAVAILABLE: '来源不在已绑定输入中或不可用',
  RANGE_NOT_MAPPABLE_TO_BOUND_INPUT: '无法确认所引用的字节范围',
  INTEGRITY_CHECK_BUDGET_EXCEEDED: '工件超出完整性检查额度',
}

function EvidenceReport({ text, onOpenArtifact, disabled }: { text: string; onOpenArtifact: (id: string, sha: string) => void; disabled: boolean }) {
  try {
    const value: unknown = JSON.parse(text)
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const report = value as Record<string, unknown>
      if (report.schema_version === 1 && Array.isArray(report.findings)) {
        return <div className="review-result"><p>以下为模型判断及宿主引用检查；引用可定位不代表结论已证实。</p>
          {typeof report.summary === 'string' && <p>{report.summary}</p>}
          {report.findings.map((entry: unknown, index: number) => {
            if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return null
            const item = entry as Record<string, unknown>
            const references = Array.isArray(item.references) ? item.references : []
            return <div key={index}>
              <strong>模型判断：{typeof item.model_status === 'string' ? item.model_status : '未知'}</strong>
              {typeof item.severity === 'string' && ` · ${item.severity}`}{typeof item.location === 'string' && ` · ${item.location}`}
              {typeof item.description === 'string' && <p>{item.description}</p>}
              {typeof item.recommendation === 'string' && <p>建议：{item.recommendation}</p>}
              <p>结论支持：未验证</p>
              {references.length === 0 && <p>没有可核对的引用。</p>}
              {references.map((reference: unknown, refIndex: number) => {
                const ref = reference && typeof reference === 'object' && !Array.isArray(reference) ? reference as Record<string, unknown> : {}
                const candidate = ref.candidate && typeof ref.candidate === 'object' && !Array.isArray(ref.candidate) ? ref.candidate as Record<string, unknown> : {}
                return <p key={refIndex}>引用关系：{ref.reference_status === 'VALIDATED' ? '已核对' : '未验证'}
                  {typeof candidate.operation_id === 'string' && <> · 操作 <code title={candidate.operation_id}>{candidate.operation_id.slice(0, 12)}</code></>}
                  {typeof candidate.artifact_id === 'string' && <> · 工件 <code title={candidate.artifact_id}>{candidate.artifact_id.slice(0, 12)}</code></>}
                  {typeof candidate.expected_sha256 === 'string' ? <> · 版本 <code title={candidate.expected_sha256}>{candidate.expected_sha256.slice(0, 12)}</code></> : ' · 版本未知'}
                  {typeof candidate.start === 'number' && typeof candidate.end === 'number' ? ` · 字节 ${candidate.start}–${candidate.end}（右端不含）` : ' · 定位未知'}
                  {typeof ref.reason === 'string' && ` · ${evidenceReasons[ref.reason] ?? '来源状态未知'}`}
                  {ref.reference_status === 'VALIDATED' && typeof candidate.artifact_id === 'string' && typeof candidate.expected_sha256 === 'string'
                    && <Button size="small" disabled={disabled} onClick={() => onOpenArtifact(candidate.artifact_id as string, candidate.expected_sha256 as string)}>查看来源工件</Button>}</p>
              })}
            </div>
          })}
          {Array.isArray(report.unverified) && report.unverified.length > 0 && <div><strong>未验证项</strong>{report.unverified.map((item: unknown, index: number) => typeof item === 'string' ? <p key={index}>{item}</p> : null)}</div>}
        </div>
      }
    }
  } catch { /* Show unrecognized content as plain text, not HTML. */ }
  return <pre>{text}</pre>
}

function EvidenceTrace({ run }: { run: Run }) {
  const [page, setPage] = useState<EvidenceViewPage | null>(null)
  const [items, setItems] = useState<EvidenceViewItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState<{ id: string; kind: string; content: string; offset: number; next: number; size: number; sha256: string } | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [previewBusy, setPreviewBusy] = useState(false)
  const generation = useRef(0), previewGeneration = useRef(0)

  async function load(reset: boolean) {
    if (!reset && (loading || !page?.next_cursor)) return
    const serial = reset ? ++generation.current : generation.current
    const cursor = reset ? null : page!.next_cursor
    const expected = reset ? null : page
    setLoading(true); setError('')
    if (reset) { setPage(null); setItems([]); setPreview(null); setPreviewBusy(false); setPreviewError(''); previewGeneration.current++ }
    try {
      const next = await api.evidenceView(run.id, cursor)
      if (serial !== generation.current) return
      if (next.schema_version !== 1 || next.run_id !== run.id || !Array.isArray(next.items)
          || !Number.isSafeInteger(next.watermark) || next.watermark < 0
          || !Number.isSafeInteger(next.snapshot_total) || next.snapshot_total < 0
          || !next.recording_capabilities || !['supported', 'not_recorded', 'unknown'].includes(next.recording_capabilities.handoff)
          || !['supported', 'not_recorded', 'unknown'].includes(next.recording_capabilities.review)
          || !['loading', 'complete', 'incomplete', 'legacy_unrecorded'].includes(next.snapshot_status)
          || next.items.some(item => !item || typeof item.event_id !== 'string' || !Number.isSafeInteger(item.sequence_number))
          || (next.next_cursor !== null && typeof next.next_cursor !== 'string')
          || (expected && (next.watermark !== expected.watermark || next.snapshot_total !== expected.snapshot_total))
          || (cursor && next.next_cursor === cursor)) throw new Error('证据快照不一致，请重新加载。')
      setItems(previous => {
        const byId = new Map((reset ? [] : previous).map(item => [item.event_id, item]))
        next.items.forEach(item => byId.set(item.event_id, item))
        return [...byId.values()].sort((a, b) => a.sequence_number - b.sequence_number)
      })
      setPage(next)
    } catch (reason) {
      if (serial === generation.current) setError(reason instanceof Error ? reason.message : '证据记录读取失败。')
    } finally { if (serial === generation.current) setLoading(false) }
  }

  useEffect(() => {
    void load(true)
    return () => { generation.current++; previewGeneration.current++ }
  }, [run.id])

  async function showArtifact(id: string, kind: string, offset = 0, expectedSha?: string) {
    const serial = ++previewGeneration.current
    const previous = preview
    setPreviewBusy(true); setPreviewError('')
    if (offset === 0) setPreview(null)
    try {
      const result = await api.evidenceArtifact(run.id, id, offset)
      if (serial !== previewGeneration.current) return
      if (offset === 0 && (!expectedSha || result.sha256 !== expectedSha)) throw new Error('工件版本与证据记录不一致。')
      if (kind === 'review' && result.size > 1024 * 1024) throw new Error('审查报告超过读取上限。')
      if (offset > 0) {
        if (!previous || previous.id !== id || previous.kind !== kind || previous.next !== offset
            || previous.sha256 !== result.sha256 || previous.size !== result.size) throw new Error('工件版本或分页发生变化，请重新读取。')
        if (kind === 'review') setPreview({ id, kind, offset: 0, ...result, content: previous.content + result.content })
        else setPreview({ id, kind, offset, ...result })
      } else setPreview({ id, kind, offset, ...result })
    } catch (reason) {
      if (serial === previewGeneration.current) setPreviewError(reason instanceof Error ? reason.message : '工件读取失败。')
    } finally { if (serial === previewGeneration.current) setPreviewBusy(false) }
  }

  const fullyLoaded = Boolean(page?.snapshot_complete && page.snapshot_status === 'complete' && items.length === page.snapshot_total)
  return <section className="detail-section">
    <h3><ShieldCheck size={14} />交接与审查证据</h3>
    <p className="detail-hint">来源记录和模型判断分开显示；记录完整不代表结论真实，也不能证明供应商已接收。</p>
    <div className="control-row"><span>{loading ? '正在读取快照…' : page ? `截至事件 ${page.watermark} · 已载入 ${items.length} / ${page.snapshot_total}` : '尚未读取快照'}</span><Button size="small" onClick={() => void load(true)} disabled={loading}>刷新</Button></div>
    {error && <p className="detail-hint" role="alert">{error} 当前记录完整性未知。</p>}
    {page && <>
      {page.snapshot_status === 'legacy_unrecorded' && <p className="detail-hint">当前工作流未记录交接或审查来源。</p>}
      {page.snapshot_status === 'incomplete' && <p className="detail-hint">快照存在缺失或不支持的记录，完整性未知。</p>}
      {page.recording_capabilities.handoff === 'not_recorded' && <p className="detail-hint">此工作流未记录交接来源。</p>}
      {page.recording_capabilities.review === 'not_recorded' && <p className="detail-hint">此工作流未记录逐项审查引用。</p>}
      {(page.recording_capabilities.handoff === 'unknown' || page.recording_capabilities.review === 'unknown') && <p className="detail-hint">部分记录能力未知。</p>}
      {fullyLoaded && <p className="detail-hint">已加载截至当前水位的记录；之后仍可能追加，可手动刷新。</p>}
      {items.map(item => {
        const payload = item.payload ?? {}
        if (item.unsupported_schema) return <p className="detail-hint" key={item.event_id}>记录 #{item.sequence_number} 的版本不受支持。</p>
        if (item.event_type === 'handoff.prepared') {
          const id = typeof payload.artifact_id === 'string' ? payload.artifact_id : null
          const sha = typeof payload.artifact_sha256 === 'string' ? payload.artifact_sha256 : undefined
          const operations = Array.isArray(payload.host_operations) ? payload.host_operations : []
          return <details className="stage-result" key={item.event_id}><summary>交接记录 #{item.sequence_number} · 模型报告未验证</summary>
            <p className="detail-hint">来源任务 {typeof payload.source_task_ref === 'string' ? payload.source_task_ref.slice(0, 12) : '未知'} → 目标任务 {typeof payload.target_task_ref === 'string' ? payload.target_task_ref.slice(0, 12) : '未知'}。</p>
            <p className="detail-hint">已关联操作 {typeof payload.operations_included === 'number' ? payload.operations_included : '未知'} 条；操作覆盖未验证{payload.operations_truncated === true ? '，列表已截断' : ''}。{payload.report_truncated === true ? '交接报告已截断。' : ''}</p>
            {operations.map((entry: unknown, index: number) => { const operation = entry && typeof entry === 'object' && !Array.isArray(entry) ? entry as Record<string, unknown> : {}; return typeof operation.operation_id === 'string' ? <p className="detail-hint" key={index}>操作 <code title={operation.operation_id}>{operation.operation_id.slice(0, 12)}</code> · {operation.status === 'RECORDED' ? '已有记录' : '未验证'}</p> : null })}
            {id && sha && <Button size="small" disabled={previewBusy} onClick={() => void showArtifact(id, 'handoff', 0, sha)}>查看原始模型报告</Button>}
          </details>
        }
        if (item.event_type === 'review.evidence_recorded') {
          const id = typeof payload.report_artifact_id === 'string' ? payload.report_artifact_id : null
          const sha = typeof payload.report_sha256 === 'string' ? payload.report_sha256 : undefined
          return <details className="stage-result" key={item.event_id}><summary>审查引用 #{item.sequence_number} · 结论未验证</summary>
            <p className="detail-hint">审查项 {typeof payload.finding_count === 'number' ? payload.finding_count : '未知'} · 已核对引用 {typeof payload.validated_count === 'number' ? payload.validated_count : '未知'} · 未验证引用 {typeof payload.unverified_count === 'number' ? payload.unverified_count : '未知'}。</p>
            {payload.truncated === true && <p className="detail-hint">报告元数据存在截断。</p>}
            {id && sha && <Button size="small" disabled={previewBusy} onClick={() => void showArtifact(id, 'review', 0, sha)}>查看审查与引用</Button>}
          </details>
        }
        return <p className="detail-hint" key={item.event_id}>记录 #{item.sequence_number} 的类型不受支持。</p>
      })}
      {page.next_cursor && <Button size="small" busy={loading} onClick={() => void load(false)}>加载更多</Button>}
      {!page.next_cursor && !fullyLoaded && page.snapshot_status !== 'legacy_unrecorded' && <p className="detail-hint">无法确认当前水位记录完整。</p>}
    </>}
    {previewError && <p className="detail-hint" role="alert">{previewError}</p>}
    {preview && <div className="artifact-preview"><strong>{preview.kind === 'review' ? '审查与引用' : preview.kind === 'source' ? '引用来源工件' : '模型报告'}</strong>
      {preview.kind === 'review' && preview.offset === 0 && preview.next === preview.size ? <EvidenceReport text={preview.content} disabled={previewBusy} onOpenArtifact={(id, sha) => void showArtifact(id, 'source', 0, sha)} /> : <pre>{preview.content}</pre>}
      <p className="detail-hint">{preview.next < preview.size ? preview.kind === 'review' ? '报告尚未完整加载，继续加载后可查看结构化审查。' : '内容分页显示，当前并非完整工件。' : '当前工件页已显示至结尾。'}</p>
      <Button size="small" onClick={() => { previewGeneration.current++; setPreview(null); setPreviewBusy(false); setPreviewError('') }}>收起</Button>
      {preview.next < preview.size && <Button size="small" disabled={previewBusy} onClick={() => void showArtifact(preview.id, preview.kind, preview.next)}>下一页</Button>}
    </div>}
  </section>
}
function StageResultContent({ stage, content }: { stage: string; content: string }) {
  if (stage === 'review') {
    try {
      const report: unknown = JSON.parse(content)
      if (report && typeof report === 'object' && !Array.isArray(report)) {
        const data = report as Record<string, unknown>
        if (typeof data.summary === 'string' && Array.isArray(data.findings) && Array.isArray(data.unverified)) {
          return <div className="review-result"><p>模型审查意见；其中的状态不代表已运行测试或完成验证。</p><p>{data.summary}</p>
            {data.findings.map((item: unknown, index: number) => {
              if (!item || typeof item !== 'object' || Array.isArray(item)) return null
              const finding = item as Record<string, unknown>
              return <div key={index}><strong>{typeof finding.status === 'string' ? finding.status : '状态未知'}</strong>{typeof finding.severity === 'string' && ` · ${finding.severity}`}{typeof finding.location === 'string' && ` · ${finding.location}`}<p>{typeof finding.description === 'string' ? finding.description : ''}</p>{typeof finding.recommendation === 'string' && <p>建议：{finding.recommendation}</p>}</div>
            })}
            {data.unverified.length > 0 && <div><strong>未验证</strong>{data.unverified.map((item: unknown, index: number) => typeof item === 'string' ? <p key={index}>{item}</p> : null)}</div>}
          </div>
        }
      }
    } catch { /* Legacy review content is plain text. */ }
  }
  return <>{stage === 'review' && <p className="detail-hint">模型审查意见，不代表已运行测试或完成验证。</p>}<pre>{content}</pre></>
}
export function RunDetails({ run, events, connected, onClose, controls, agentRuns = null, agentRunsError = false, onReloadAgentRuns }: { run: Run | null; events: RunEvent[]; connected: boolean; onClose: () => void; controls?: ReactNode; agentRuns?: AgentRun[] | null; agentRunsError?: boolean; onReloadAgentRuns?: () => void }) {
  const panelRef = useRef<HTMLElement>(null)
  const onCloseRef = useRef(onClose)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [closing, setClosing] = useState(false)
  const [overlay, setOverlay] = useState(() => window.matchMedia('(max-width: 1100px)').matches)
  onCloseRef.current = onClose
  const close = useCallback(() => {
    if (closeTimer.current !== null) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      onCloseRef.current()
      return
    }
    setClosing(true)
    closeTimer.current = setTimeout(() => onCloseRef.current(), 180)
  }, [])
  useEffect(() => {
    const media = window.matchMedia('(max-width: 1100px)')
    const update = () => setOverlay(media.matches)
    media.addEventListener('change', update)
    return () => {
      media.removeEventListener('change', update)
      if (closeTimer.current !== null) clearTimeout(closeTimer.current)
    }
  }, [])
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const panel = panelRef.current
    // On small screens the detail drawer owns focus until it closes.
    const background = overlay ? Array.from(document.querySelectorAll<HTMLElement>('.sidebar, .workspace-header, .chat-main')) : []
    const previousInert = background.map(element => element.inert)
    background.forEach(element => { element.inert = true })
    if (overlay) panel?.querySelector<HTMLButtonElement>('button')?.focus({ preventScroll: true })
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.isComposing) return
      if (event.key === 'Escape') { event.preventDefault(); close(); return }
      if (!overlay || event.key !== 'Tab' || !panel) return
      const targets = Array.from(panel.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]'))
        .filter(element => element.getClientRects().length > 0)
      const first = targets[0], last = targets[targets.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      background.forEach((element, index) => { element.inert = previousInert[index] })
      if (opener?.isConnected && !opener.closest('.run-panel') && (overlay || panel?.contains(document.activeElement))) opener.focus({ preventScroll: true })
    }
  }, [overlay, close])
  const displayEvents = events.filter(event => event.event_type !== 'message.delta')
  const deltas = events.filter(event => event.event_type === 'message.delta').length
  const stageResults = events.filter(event => event.event_type === 'agent.stage_result' && typeof event.payload.stage === 'string' && typeof event.payload.content === 'string')
  const selectionEvents = events.filter(event => event.event_type === 'context.history_selection')
  const inputEvents = events.filter(event => event.event_type === 'context.model_input')
  const reasonLabels: Record<string, string> = { current_input: '当前输入', same_scope: '当前范围', explicitly_selected: '手动选入', scope_mismatch: '其他项目', legacy_unassigned: '旧未归属', incomplete_answer: '回答未完成' }
  const usage = run?.usage ?? {}
  const input = usage.input_tokens ?? usage.prompt_tokens
  const output = usage.output_tokens ?? usage.completion_tokens
  const cost = usage.cost_usd
  const isError = run?.status === 'failed' || run?.status === 'interrupted' || run?.status === 'pending_review'
  return <>
    {overlay && <div className={`run-panel-shade ${closing ? 'is-closing' : ''}`} onClick={close} aria-hidden="true" />}
    <aside ref={panelRef} className={`run-panel ${closing ? 'is-closing' : ''}`} role={overlay ? 'dialog' : 'complementary'} aria-modal={overlay || undefined} aria-labelledby="run-panel-heading">
    <div className="run-panel-title"><div><Activity size={18} /><h2 id="run-panel-heading">运行详情</h2></div><Button variant="ghost" size="icon" aria-label="收起运行详情" title="收起运行详情 · Esc" onClick={close}><PanelRightClose size={19} /></Button></div>
    {!run ? <div className="run-placeholder"><span><Activity size={27} /></span><h3>每一步，都有迹可循</h3><p>发送消息后，这里会显示真实的运行状态、模型用量和事件记录。</p><div className="placeholder-steps"><span><Circle size={10} />创建任务</span><i /><span><Circle size={10} />调用模型</span><i /><span><Circle size={10} />交付回答</span></div></div> : <>
      <div className="run-summary"><div className="detail-label">当前运行</div><div className="run-status"><span className={`status-orb ${isError ? 'orb-red' : run.status === 'completed' ? 'orb-green' : terminalStatuses.has(run.status) ? '' : 'orb-blue'}`} /><h3>{run.workflow_type === 'agent' && run.workspace_id && run.status === 'completed' ? '流程已结束' : statusLabels[run.status]}</h3>{run.mode === 'mock' && <Badge tone="amber">MOCK</Badge>}</div>{run.workflow_type === 'agent' && run.workspace_id && run.status === 'completed' && <p className="detail-hint">流程结束表示答复已生成；目标是否达成请结合工具记录和工件核对。</p>}<p className="run-model"><Radio size={14} />{run.model_label}</p><p className="run-model"><GitBranch size={14} />运行版本：{typeof run.workflow_version === 'string' && run.workflow_version.trim() ? run.workflow_version : '未记录'}</p>{run.workflow_type === 'agent' && run.agent_strategy === 'single' && <p className="detail-hint">单 Agent 运行，不产生跨 Agent 交接或独立审查。</p>}<code className="run-id" title={run.id}><Hash size={11} />{run.id.slice(0, 16)}</code></div>
      <ToolActivity key={run.id} run={run} />
      <RunObservation key={run.id} run={run} />
      <ActionOutcomePanel key={run.id} run={run} />
      {controls}
      <EvidenceTrace key={run.id} run={run} />
      <section className="detail-section"><h3><Hash size={14} />上下文来源</h3>
        <p className="detail-hint">这里只展示框架记录的来源和派发意图；不能证明供应商已接收或模型已理解。未关联记录的调用不会显示来源，缺失表示未记录。</p>
        {!selectionEvents.length && !inputEvents.length && <p className="detail-hint">当前事件流尚未显示上下文来源，历史记录可能尚未收全。</p>}
        {selectionEvents.length > 0 && <details className="stage-result"><summary>首次历史范围选择 · {selectionEvents.reduce((total, event) => total + (Array.isArray(event.payload.items) ? event.payload.items.length : 0), 0)} 条</summary>
          <p className="detail-hint">这是首次准备时、压缩前的历史筛选结果，不代表所有选入内容都以原文发送；恢复后以调用记录为准。</p>
          {selectionEvents.flatMap(event => Array.isArray(event.payload.items) ? event.payload.items : []).map((item: unknown, index) => {
            if (!item || typeof item !== 'object' || Array.isArray(item)) return null
            const row = item as Record<string, unknown>
            if (typeof row.message_id !== 'string' || typeof row.source_run_id !== 'string') return null
            return <p className="detail-hint" key={`${row.message_id}-${index}`}><code title={row.message_id}>{row.message_id.slice(0, 12)}</code> · 任务 <code title={row.source_run_id}>{row.source_run_id.slice(0, 8)}</code> · {row.disposition === 'included' ? '选入' : '未带入'} · {typeof row.reason === 'string' ? (reasonLabels[row.reason] ?? '原因未知') : '原因未知'}</p>
          })}
          {selectionEvents.some(event => event.payload.truncated === true) && <p className="detail-hint">历史来源较多，展示已截断。</p>}
        </details>}
        {inputEvents.map(event => {
          const payload = event.payload
          const node = agentRuns?.find(item => item.id === payload.agent_run_id)
          const direct = Array.isArray(payload.direct) ? payload.direct : []
          const coverage = Array.isArray(payload.summary_coverage) ? payload.summary_coverage : []
          const summaries = Array.isArray(payload.summary_ids) ? payload.summary_ids : []
          const manifest = payload.manifest && typeof payload.manifest === 'object' && !Array.isArray(payload.manifest) ? payload.manifest as Record<string, unknown> : null
          return <details className="stage-result" key={event.event_id}><summary>{typeof payload.role === 'string' ? (stageLabels[payload.role as keyof typeof stageLabels] ?? payload.role) : '阶段未知'} · {node ? (nodeLabels[node.status] ?? '状态未知') : '已预留 / 派发意图'}</summary>
            <p className="detail-hint">模型 ID：{typeof payload.model_config_id === 'string' ? payload.model_config_id : '未知'} · 版本：{typeof payload.model_version === 'number' ? payload.model_version : '未知'}</p>
            {manifest?.status === 'recorded' ? <>
              <p className="detail-hint">上下文策略 {typeof manifest.policy_version === 'string' ? manifest.policy_version : '未知'} · 本地估算输入 {typeof manifest.estimated_input_tokens === 'number' ? manifest.estimated_input_tokens.toLocaleString() : '未知'} tokens · 输入预算 {typeof manifest.input_limit === 'number' ? manifest.input_limit.toLocaleString() : '未知'}。</p>
              <p className="detail-hint">准备内容指纹 <code title={String(manifest.request_fingerprint ?? '')}>{String(manifest.request_fingerprint ?? '').slice(0, 12)}</code> · 静态前缀指纹 <code title={String(manifest.prefix_fingerprint ?? '')}>{String(manifest.prefix_fingerprint ?? '').slice(0, 12)}</code>。仅用于同一任务内比较，不能证明缓存命中或供应商实际接收。</p>
            </> : <p className="detail-hint">该次调用未记录可用的请求清单。</p>}
            <p className="detail-hint">派发意图中可识别的原文来源 {direct.length} 条 · 已记录的摘要覆盖来源 {coverage.length} 条 · 摘要工件 {summaries.length} 个；系统指令{payload.system_present ? '存在' : '未记录'}，工具定义 {typeof payload.tool_count === 'number' ? payload.tool_count : '未知'} 个。完整协议内容和供应商接收状态未知。</p>
            {direct.map((item: unknown, index) => { const row = item && typeof item === 'object' ? item as Record<string, unknown> : {}; return typeof row.message_id === 'string' ? <p className="detail-hint" key={`direct-${index}`}>原文来源 <code title={row.message_id}>{row.message_id.slice(0, 12)}</code></p> : null })}
            {coverage.map((item: unknown, index) => { const row = item && typeof item === 'object' ? item as Record<string, unknown> : {}; return typeof row.message_id === 'string' && typeof row.summary_id === 'string' ? <p className="detail-hint" key={`summary-${index}`}>摘要覆盖 <code title={row.message_id}>{row.message_id.slice(0, 12)}</code> · 工件 <code title={row.summary_id}>{row.summary_id.slice(0, 12)}</code></p> : null })}
            {payload.truncated === true && <p className="detail-hint">来源较多，展示已截断。</p>}
          </details>
        })}
      </section>
      {run.workflow_type === 'agent' && Boolean(run.stage_plan?.length) && <section className="detail-section"><h3><GitBranch size={14} />计划分配</h3><p className="detail-hint">{strategyLabels[run.agent_strategy ?? 'staged']} · 任务创建时冻结的模型安排。</p><div className="control-list">{run.stage_plan?.filter(item => !run.active_stages || run.active_stages.includes(item.stage)).map(item => <div className="control-row" key={item.stage}><strong>{stageLabels[item.stage]}</strong><span>{item.model_name}</span><Badge>{item.source === 'task_override' ? '本次方案' : item.source === 'override' ? '阶段设置' : '默认'}</Badge></div>)}</div></section>}
      {run.workflow_type === 'agent' && <section className="detail-section"><h3><Activity size={14} />实际模型调用 <span className="event-count">{agentRuns?.length ?? '—'}</span></h3><p className="detail-hint">仅展示实际记录的节点及其模型；未发生的阶段不会显示。</p>{agentRunsError ? <p className="detail-hint">模型调用记录读取失败。 <Button variant="ghost" size="small" onClick={onReloadAgentRuns}>重试</Button></p> : agentRuns === null ? <p className="detail-hint">正在读取模型调用记录。</p> : agentRuns.length ? <div className="control-list">{agentRuns.map(node => <div className="control-row" key={node.id}><strong>{stageLabels[node.role as keyof typeof stageLabels] ?? node.role}</strong><span title={node.selection_source ?? undefined}>{node.model_name || '模型未知'}</span><Badge tone={node.status === 'pending_review' || node.status === 'failed' ? 'red' : node.status === 'completed' ? 'green' : 'neutral'}>{nodeLabels[node.status] ?? node.status}</Badge></div>)}</div> : <p className="detail-hint">暂无已记录的模型调用。</p>}</section>}
      {run.workflow_type === 'agent' && stageResults.length > 0 && <section className="detail-section"><h3><CheckCircle2 size={14} />已保存的阶段产物 <span className="event-count">{stageResults.length}</span></h3><p className="detail-hint">只显示已保存的真实产物；可展开查看。</p>{stageResults.map(event => <details className="stage-result" key={event.event_id}>
        <summary>{stageLabels[event.payload.stage as keyof typeof stageLabels] ?? String(event.payload.stage)}{typeof event.payload.task_id === 'string' ? ` · 子任务 ${event.payload.task_id}` : ''}</summary>
        <StageResultContent stage={String(event.payload.stage)} content={String(event.payload.content)} />{event.payload.truncated === true && <p className="detail-hint">内容过长，显示已截断。</p>}
      </details>)}</section>}
      {isError && <div className="run-error" role="alert"><XCircle size={17} /><div><strong>{run.error_code || '运行未完成'}</strong><p>{run.error_message || '本次运行已中断，请重新发送消息。'}</p></div></div>}
      {run.mode === 'mock' && <div className="mock-detail"><FlaskConical size={15} /><p>当前为模拟运行。内容与用量不代表真实模型表现。</p></div>}
      <section className="detail-section"><h3><Coins size={14} />用量</h3><div className="usage-grid"><div><span>输入 Token</span><strong>{input == null ? '—' : input.toLocaleString()}</strong></div><div><span>输出 Token</span><strong>{output == null ? '—' : output.toLocaleString()}</strong></div></div><div className="cost-row"><span>预估费用</span><strong>{cost == null ? '未知' : `$${cost.toFixed(6)}`}</strong></div><p className="detail-hint">仅展示服务端返回的用量。缺失数据不作估算。</p></section>
      <section className="detail-section event-section"><h3><Clock3 size={14} />运行事件 <span className="event-count">{displayEvents.length}</span></h3><div className="event-timeline">{displayEvents.map(event => <div className="event-row" key={event.event_id}><span className={`event-dot ${event.event_type === 'run.failed' ? 'error' : ''}`}>{event.event_type === 'run.completed' ? <CheckCircle2 size={13} /> : event.event_type === 'run.failed' ? <XCircle size={13} /> : <span />}</span><div><strong>{eventLabels[event.event_type] ?? event.event_type}</strong><time>{time(event.created_at)}</time>{typeof event.payload.message === 'string' && <p>{event.payload.message}</p>}</div></div>)}{!displayEvents.length && <p className="detail-hint">正在读取已保存的事件…</p>}</div>{deltas > 0 && <div className="stream-count"><ArrowUpRight size={13} />已接收 {deltas} 个内容片段</div>}</section>
      <div className="run-connection"><span className={`connection-dot ${connected ? 'live' : ''}`} />{connected ? '事件流已连接' : terminalStatuses.has(run.status) ? '运行已结束 · 记录已保存' : '事件流连接中'}</div>
    </>}
  </aside></>
}
