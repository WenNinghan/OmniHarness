import { CollaborationMode } from './components/CollaborationMode.tsx'
import { strategyFromStorage, strategyLabels } from './lib/collaboration-state.ts'
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ArrowRight, ArrowUp, Check, ChevronRight, CircleAlert, Code2, FileText, FlaskConical, KeyRound, LoaderCircle, LockKeyhole, LogOut, Menu, MessageSquare, PanelRightOpen, Plus, Search, Settings2, ShieldCheck, Sparkles, Square, X, GitBranch } from 'lucide-react'
import { api } from './lib/api.ts'
import type { AgentRun, Bootstrap, BudgetSnapshot, Clarification, CommandType, Conversation, Message, ModelConfig, PendingRoutingPlan, Run, RunCommand, RunEvent, RunStatus, WorkflowType } from './lib/types.ts'
import { statusLabels, terminalStatuses } from './lib/types.ts'
import { acceptsSnapshot, applyEvent, emptyStream, mergeMessages, mergeRunSnapshot } from './lib/run-state.ts'
import { canStopRun } from './lib/control-state.ts'
import { refreshesAgentNodes, workflowAvailable } from './lib/agent-state.ts'
import { Badge, Brand, Button } from './components/ui.tsx'
import { MessageBubble } from './components/MessageBubble.tsx'
import { ModelPicker } from './components/ModelPicker.tsx'
import { WorkspacePicker } from './components/WorkspacePicker.tsx'
import { HistoryScopePicker } from './components/HistoryScopePicker.tsx'
import { modelDisplayName } from './lib/model-picker.ts'
import { ModelSettings } from './components/ModelSettings.tsx'
import { Orchestration } from './components/Orchestration.tsx'
import { RunControls } from './components/RunControls.tsx'
import { RunDetails } from './components/RunDetails.tsx'
import { ModelRoutingPlanner } from './components/ModelRoutingPlanner.tsx'
import { ClarificationPanel } from './components/ClarificationPanel.tsx'
import { HistoricalClarifications } from './components/HistoricalClarifications.tsx'
import { ActionRequirements } from './components/ActionRequirements.tsx'
import { parseActionDrafts } from './lib/action-requirements.ts'
import type { ActionDraft } from './lib/action-requirements.ts'
import { CLARIFICATION_EVENTS, canAnswerClarification, historicalClarificationRun, isClarificationRefreshEvent, supportsClarifications } from './lib/clarification-state.ts'
import { latestProgress } from './lib/progress-state.ts'
import { displayedWorkflowType, previewActiveNotice, previewBadge, previewNotice } from './lib/preview-state.ts'
import { displayedRunModel } from './lib/model-run-state.ts'
import { restoreRoutingPlans, serializeRoutingPlans } from './lib/routing-plan-cache.ts'

function Pairing({ onPaired }: { onPaired: () => void }) {
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    try { await api.pair(code.trim()); onPaired() }
    catch (reason) { setError(reason instanceof Error ? reason.message : '配对失败，请重试。') }
    finally { setBusy(false) }
  }
  return <div className="pair-page"><div className="pair-top"><Brand /><span>LOCAL-FIRST AI WORKSPACE</span></div><main className="pair-card"><span className="pair-icon"><LockKeyhole size={28} /></span><span className="eyebrow">欢迎来到你的 AI 工作台</span><h1>连接本地 OmniHarness</h1><p>输入启动终端显示的配对码，<br />将当前浏览器与本地服务安全连接。</p><form onSubmit={submit}><label htmlFor="pair-code">本地配对码</label><input id="pair-code" value={code} onChange={event => setCode(event.target.value)} placeholder="输入配对码" autoComplete="one-time-code" autoFocus required spellCheck={false} /><Button variant="primary" type="submit" busy={busy} disabled={!code.trim()}>连接工作台<ArrowRight size={16} /></Button>{error && <div className="notice notice-error" role="alert">{error}</div>}</form><div className="pair-footnote"><ShieldCheck size={15} /><span>登录凭据仅用于当前本地服务</span></div></main><div className="pair-bottom">OmniHarness <span>·</span> 由你掌控的模型工作空间</div></div>
}

export default function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null)
  const [bootError, setBootError] = useState('')
  const [models, setModels] = useState<ModelConfig[]>([])
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [selected, setSelected] = useState<string | null>(() => localStorage.getItem('omni.conversation'))
  const [page, setPage] = useState<'chat' | 'models' | 'orchestration'>('chat')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [detailsOpen, setDetailsOpen] = useState(() => {
    const saved = localStorage.getItem('omni.details')
    return saved === null ? window.matchMedia('(min-width: 1100px)').matches : saved !== 'false'
  })
  const [history, setHistory] = useState<Message[]>([])
  const [run, setRun] = useState<Run | null>(null)
  const [conversationRuns, setConversationRuns] = useState<Run[]>([])
  const [stream, setStream] = useState(emptyStream)
  const [connected, setConnected] = useState(false)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [modelId, setModelId] = useState(() => localStorage.getItem('omni.model') ?? '')
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [toast, setToast] = useState<{ message: string; error: boolean } | null>(null)
  const [commands, setCommands] = useState<RunCommand[]>([])
  const [budget, setBudget] = useState<BudgetSnapshot | null>(null)
  const [agentRuns, setAgentRuns] = useState<AgentRun[] | null>(null)
  const [agentRunsError, setAgentRunsError] = useState(false)
  const [clarifications, setClarifications] = useState<Clarification[]>([])
  const [clarificationError, setClarificationError] = useState('')
  const [workflowType, setWorkflowType] = useState<WorkflowType>('chat')
  const [agentStrategy, setAgentStrategy] = useState(() => strategyFromStorage(localStorage.getItem('omni.agent.strategy')))
  const [workspaceId, setWorkspaceId] = useState<string | null>(null)
  const [actionDrafts, setActionDrafts] = useState<ActionDraft[]>([])
  const [autoCorrection, setAutoCorrection] = useState(true)
  const [chatHistoryWorkspaceId, setChatHistoryWorkspaceId] = useState<string | null>(null)
  const [additionalHistoryRunIds, setAdditionalHistoryRunIds] = useState<string[]>([])
  const [routingOpen, setRoutingOpen] = useState(false)
  const [routingPlans, setRoutingPlans] = useState<Record<string, PendingRoutingPlan>>(() => {
    try { return restoreRoutingPlans(sessionStorage.getItem('omni.routing.plans.v1')) } catch { return {} }
  })
  const [controlBusy, setControlBusy] = useState(false)
  const [search, setSearch] = useState('')
  const textRef = useRef<HTMLTextAreaElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const selectedRef = useRef(selected)
  const runRef = useRef(run)
  const snapshotVersion = useRef(0)
  const extrasVersion = useRef(0)
  const agentRunsVersion = useRef(0)
  const clarificationVersion = useRef(0)
  const sendingRef = useRef(false)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const nearBottom = useRef(true)
  selectedRef.current = selected
  runRef.current = run
  const notify = useCallback((message: string, error = false) => {
    setToast({ message, error }); if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), error ? 9000 : 4500)
  }, [])
  const refreshModels = useCallback(async () => { setModels(await api.models()) }, [])
  const refreshConversations = useCallback(async () => { setConversations(await api.conversations()) }, [])
  const refreshExtras = useCallback(async (id: string, conversationId: string) => {
    if (selectedRef.current !== conversationId || runRef.current?.id !== id) return
    const version = ++extrasVersion.current
    try {
      const [list, snapshot] = await Promise.all([api.commands(id), api.budget(id)])
      if (!acceptsSnapshot(version, extrasVersion.current, selectedRef.current, conversationId, runRef.current?.id, id)) return
      setCommands(list); setBudget(snapshot)
    } catch (reason) {
      if (acceptsSnapshot(version, extrasVersion.current, selectedRef.current, conversationId, runRef.current?.id, id)) notify(reason instanceof Error ? reason.message : '控制状态加载失败', true)
    }
  }, [notify])
  const refreshAgentRuns = useCallback(async (id: string, conversationId: string) => {
    if (selectedRef.current !== conversationId || runRef.current?.id !== id || runRef.current.workflow_type !== 'agent') return
    const version = ++agentRunsVersion.current
    try {
      const nodes = await api.agentRuns(id)
      if (acceptsSnapshot(version, agentRunsVersion.current, selectedRef.current, conversationId, runRef.current?.id, id)) { setAgentRuns(nodes); setAgentRunsError(false) }
    } catch (reason) {
      if (acceptsSnapshot(version, agentRunsVersion.current, selectedRef.current, conversationId, runRef.current?.id, id)) { setAgentRunsError(true); notify(reason instanceof Error ? reason.message : '模型调用节点加载失败', true) }
    }
  }, [notify])
  const refreshClarifications = useCallback(async (id: string, conversationId: string) => {
    if (selectedRef.current !== conversationId || runRef.current?.id !== id) return
    const version = ++clarificationVersion.current
    try {
      const items = await api.clarifications(id)
      if (acceptsSnapshot(version, clarificationVersion.current, selectedRef.current, conversationId, runRef.current?.id, id)) {
        setClarifications(items.filter(item => item.run_id === id)); setClarificationError('')
      }
    } catch (reason) {
      if (acceptsSnapshot(version, clarificationVersion.current, selectedRef.current, conversationId, runRef.current?.id, id))
        setClarificationError(reason instanceof Error ? reason.message : '问题加载失败，请重试。')
    }
  }, [])
  const loadBootstrap = useCallback(async () => {
    setBootError('')
    try { setBootstrap(await api.bootstrap()) }
    catch (reason) { setBootError(reason instanceof Error ? reason.message : '无法连接本地服务。') }
  }, [])
  useEffect(() => { void loadBootstrap() }, [loadBootstrap])
  useEffect(() => {
    if (!bootstrap?.authenticated) return
    void Promise.all([refreshModels(), refreshConversations()]).catch(reason => notify(reason instanceof Error ? reason.message : '加载失败', true))
  }, [bootstrap?.authenticated, refreshModels, refreshConversations, notify])
  const enabledModels = models.filter(model => model.enabled)
  const selectedModel = models.find(model => model.id === modelId)
  const routingKey = selected ?? '__new__'
  const routingPlan = routingPlans[routingKey] ?? null
  const routingAccepted = Boolean(routingPlan && routingPlan.acceptedTask === draft.trim() && draft.trim())
  function setCurrentRoutingPlan(next: PendingRoutingPlan | null) {
    setRoutingPlans(old => {
      const updated = { ...old }
      if (next) updated[routingKey] = next
      else delete updated[routingKey]
      return updated
    })
  }
  useEffect(() => { if (modelId) localStorage.setItem('omni.model', modelId); else localStorage.removeItem('omni.model') }, [modelId])
  useEffect(() => { try { sessionStorage.setItem('omni.routing.plans.v1', serializeRoutingPlans(routingPlans)) } catch { /* Storage may be unavailable. */ } }, [routingPlans])
  useEffect(() => { localStorage.setItem('omni.details', String(detailsOpen)) }, [detailsOpen])
  useEffect(() => { setAdditionalHistoryRunIds([]) }, [selected, workflowType, workspaceId, chatHistoryWorkspaceId])
  useEffect(() => {
    if (selected) localStorage.setItem('omni.conversation', selected); else localStorage.removeItem('omni.conversation')
    if (!bootstrap?.authenticated) return
    setHistory([]); setRun(null); setConversationRuns([]); setStream(emptyStream()); setConnected(false); setCommands([]); setBudget(null); setAgentRuns(null); setAgentRunsError(false); setClarifications([]); setClarificationError(''); clarificationVersion.current++; agentRunsVersion.current++; nearBottom.current = true
    if (!selected) return
    if (sendingRef.current) { setLoadingHistory(false); return }
    let cancelled = false
    setLoadingHistory(true)
    void Promise.all([api.messages(selected), api.runs(selected)]).then(([messages, runs]) => {
      if (cancelled) return
      setHistory(messages)
      setConversationRuns(runs)
      const latest = [...runs].sort((a, b) => b.created_at.localeCompare(a.created_at))[0]
      setRun(latest ?? null)
    }).catch(reason => { if (!cancelled) notify(reason instanceof Error ? reason.message : '会话加载失败', true) }).finally(() => { if (!cancelled) setLoadingHistory(false) })
    return () => { cancelled = true }
  }, [selected, bootstrap?.authenticated, notify])

  useEffect(() => {
    if (!run || run.conversation_id !== selected) return
    setConversationRuns(old => [run, ...old.filter(item => item.id !== run.id)])
  }, [run, selected])

  useEffect(() => {
    if (!run?.id || !bootstrap?.authenticated) return
    const id = run.id, conversationId = run.conversation_id
    let disposed = false, finished = false
    setConnected(false)
    setClarifications([]); setClarificationError(''); clarificationVersion.current++
    setAgentRuns(null); setAgentRunsError(false); agentRunsVersion.current++
    void refreshExtras(id, conversationId)
    void refreshAgentRuns(id, conversationId)
    if (supportsClarifications(run.workflow_version)) void refreshClarifications(id, conversationId)
    const source = new EventSource(`/api/v1/runs/${encodeURIComponent(id)}/events?after=0`, { withCredentials: true })
    async function reconcile() {
      const version = ++snapshotVersion.current
      try {
        const [latestRun, messages] = await Promise.all([api.run(id), api.messages(conversationId)])
        if (disposed || !acceptsSnapshot(version, snapshotVersion.current, selectedRef.current, conversationId, runRef.current?.id, id)) return
        setRun(old => mergeRunSnapshot(old, latestRun)); setHistory(messages)
        if (terminalStatuses.has(latestRun.status)) { source.close(); setConnected(false) }
        if (supportsClarifications(latestRun.workflow_version)) await refreshClarifications(id, conversationId)
        await refreshConversations()
      } catch (reason) {
        if (!disposed && acceptsSnapshot(version, snapshotVersion.current, selectedRef.current, conversationId, runRef.current?.id, id)) notify(reason instanceof Error ? reason.message : '运行状态同步失败', true)
      }
    }
    source.onopen = () => { if (!disposed) { setConnected(true); void reconcile() } }
    const receive = (raw: Event) => {
      if (disposed || finished) return
      try {
        const event = JSON.parse((raw as MessageEvent).data) as RunEvent
        if (event.run_id !== id || typeof event.sequence_number !== 'number') return
        setStream(old => applyEvent(old, event))
        if (event.event_type === 'run.started') void reconcile()
        if (isClarificationRefreshEvent(event.event_type)) {
          void reconcile(); void refreshClarifications(id, conversationId)
        }
        if (event.event_type === 'run.command' || event.event_type === 'run.paused' || event.event_type === 'run.pending_review' || event.event_type === 'run.awaiting_approval' || event.event_type === 'approval.decided') {
          void reconcile()
          void refreshExtras(id, conversationId)
        }
        if (runRef.current?.workflow_version === 'stage-agent-v10' && event.event_type === 'agent.stage_result' && event.payload.stage === 'plan') void reconcile()
        if (refreshesAgentNodes(event.event_type)) void refreshAgentRuns(id, conversationId)
        const terminal: Record<string, RunStatus> = { 'run.completed': 'completed', 'run.cancelled': 'cancelled', 'run.failed': 'failed', 'run.interrupted': 'interrupted' }
        const status = terminal[event.event_type]
        if (status) {
          finished = true; source.close(); setConnected(false)
          setRun(old => old?.id === id ? { ...old, status, error_code: typeof event.payload.code === 'string' ? event.payload.code : old.error_code, error_message: typeof event.payload.message === 'string' ? event.payload.message : old.error_message } : old)
          void reconcile()
          void refreshExtras(id, conversationId)
          if (supportsClarifications(runRef.current?.workflow_version)) void refreshClarifications(id, conversationId)
        }
      } catch { notify('收到无法解析的运行事件，正在重新同步状态。', true); void reconcile() }
    }
    source.onmessage = receive
    const names = ['run.created', 'run.started', 'message.delta', 'message.reasoning_delta', 'message.output_state', 'run.completed', 'run.cancelled', 'run.failed', 'run.interrupted', 'run.command', 'run.paused', 'run.pending_review', 'agent.delegated', 'agent.model_started', 'agent.model_completed', 'agent.model_uncertain', 'agent.stage_result', 'tool.requested', 'tool.completed', 'tool.failed', ...CLARIFICATION_EVENTS]
    names.forEach(name => source.addEventListener(name, receive))
    source.onerror = () => { if (!disposed && !finished) { setConnected(false); void reconcile() } }
    return () => { disposed = true; source.close(); setConnected(false) }
  }, [run?.id, bootstrap?.authenticated, notify, refreshConversations, refreshExtras, refreshAgentRuns, refreshClarifications])

  const visibleMessages = useMemo(() => mergeMessages(history, stream, selected ?? '', run?.id ?? ''), [history, stream, selected, run?.id])
  const active = Boolean(run && !terminalStatuses.has(run.status))
  const shownRunModel = displayedRunModel(active, run, modelId)
  useEffect(() => {
    if (nearBottom.current && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [visibleMessages, active])
  useEffect(() => {
    if (textRef.current) { textRef.current.style.height = 'auto'; textRef.current.style.height = `${Math.min(textRef.current.scrollHeight, 160)}px` }
  }, [draft])
  const hasMessages = visibleMessages.length > 0
  const conversation = conversations.find(item => item.id === selected)
  const visibleConversations = conversations.filter(item => item.title.toLowerCase().includes(search.toLowerCase())).sort((a, b) => b.updated_at.localeCompare(a.updated_at))
  const missingCredential = Boolean(selectedModel && !['mock', 'openai-compatible'].includes(selectedModel.adapter_type) && !selectedModel.key_configured)
  function newChat() { if (sending) return; setActionDrafts([]); setWorkspaceId(null); setChatHistoryWorkspaceId(null); setAdditionalHistoryRunIds([]); selectedRef.current = null; snapshotVersion.current++; extrasVersion.current++; agentRunsVersion.current++; setSelected(null); setPage('chat'); setDraft(''); setSidebarOpen(false); setRoutingOpen(false); setRoutingPlans(old => { const updated = { ...old }; delete updated.__new__; return updated }) }
  function chooseChat(id: string) { if (sending) return; setActionDrafts([]); setWorkspaceId(null); setChatHistoryWorkspaceId(null); setAdditionalHistoryRunIds([]); selectedRef.current = id; snapshotVersion.current++; extrasVersion.current++; agentRunsVersion.current++; setSelected(id); setPage('chat'); setDraft(''); setSidebarOpen(false); setRoutingOpen(false) }
  async function send(event: FormEvent) {
    event.preventDefault()
    if (!draft.trim() || sendingRef.current || active || !selectedModel || !selectedModel.enabled) return
    if (!workflowAvailable(workflowType, bootstrap?.agent_available)) { notify('Agent 任务当前不可用，请检查服务设置。', true); return }
    if (missingCredential) { notify('此模型尚未配置 API Key，请在模型连接中补充密钥。', true); setPage('models'); return }
    if (workflowType === 'agent' && routingPlan?.acceptedTask && !routingAccepted) { notify('问题已变化，请在模型规划中确认沿用或重新生成。', true); setRoutingOpen(true); setDetailsOpen(false); return }
    let actionOptions: Parameters<typeof api.send>[9]
    if (workflowType === 'agent' && workspaceId && bootstrap?.workspace_workflow_version === 'stage-agent-v10') {
      try { actionOptions = { auto_correction_enabled: autoCorrection, ...(actionDrafts.length ? { action_requirements: parseActionDrafts(actionDrafts) } : {}) } }
      catch (reason) { notify(reason instanceof Error ? reason.message : '动作要求不正确。', true); return }
    }
    const stageOverrides = workflowType === 'agent' && !(workspaceId && agentStrategy === 'single') && routingAccepted && routingPlan ? routingPlan.stages.map(item => ({
      stage: item.stage, model_config_id: item.model_config_id, model_version: item.model_version, provider_version: item.provider_version,
    })) : undefined
    sendingRef.current = true; setSending(true)
    const content = draft.trim(), requestId = crypto.randomUUID()
    try {
      let id = selected
      if (!id) {
        const created = await api.createConversation(content.slice(0, 40))
        id = created.id; setConversations(old => [created, ...old]); setSelected(id); selectedRef.current = id
        if (routingPlan) setRoutingPlans(old => { const updated = { ...old, [created.id]: routingPlan }; delete updated.__new__; return updated })
      }
      const historyWorkspaceId = workflowType === 'agent' ? workspaceId : chatHistoryWorkspaceId
      const response = await api.send(id, content, modelId, requestId, workflowType, stageOverrides, workflowType === 'agent' ? workspaceId : null, workflowType === 'agent' && workspaceId ? agentStrategy : 'staged', {
        schema_version: 1, kind: historyWorkspaceId ? 'workspace' : 'unscoped', workspace_id: historyWorkspaceId,
        additional_run_ids: additionalHistoryRunIds,
      }, actionOptions)
      if (stageOverrides && routingPlan) setRoutingPlans(old => ({ ...old, [id!]: { ...routingPlan, acceptedTask: null } }))
      setDraft(''); setActionDrafts([]); setStream(emptyStream()); nearBottom.current = true
      const [messages, currentRun] = await Promise.all([api.messages(id), api.run(response.run_id)])
      if (selectedRef.current === id) { setHistory(messages); setRun(currentRun) }
      void refreshConversations().catch(() => {})
    } catch (reason) { notify(reason instanceof Error ? reason.message : '发送失败，请重试。', true) }
    finally { setSending(false); sendingRef.current = false; textRef.current?.focus() }
  }
  async function submitControl(type: CommandType, content?: string): Promise<boolean> {
    if (!run || controlBusy) return false
    setControlBusy(true)
    try {
      const result = await api.submitCommand(run.id, type, content)
      notify(`命令 #${result.sequence_number} 已提交，等待 Worker 执行。`)
      await refreshExtras(run.id, run.conversation_id)
      if (selectedRef.current !== run.conversation_id || runRef.current?.id !== run.id) return true
      const version = ++snapshotVersion.current
      const latest = await api.run(run.id)
      if (acceptsSnapshot(version, snapshotVersion.current, selectedRef.current, run.conversation_id, runRef.current?.id, run.id)) setRun(old => mergeRunSnapshot(old, latest))
      return true
    } catch (reason) {
      notify(reason instanceof Error ? reason.message : '命令提交失败', true)
      return false
    } finally { setControlBusy(false) }
  }
  async function stop() {
    if (!run || stopping || !canStopRun(run.status)) return
    setStopping(true)
    const version = ++snapshotVersion.current
    try {
      const cancelled = await api.cancel(run.id)
      if (acceptsSnapshot(version, snapshotVersion.current, selectedRef.current, run.conversation_id, runRef.current?.id, run.id)) setRun(old => mergeRunSnapshot(old, cancelled))
    }
    catch (reason) { notify(reason instanceof Error ? reason.message : '停止失败', true) }
    finally { setStopping(false) }
  }
  async function answerClarification(item: Clarification, answer: string, requestId: string) {
    const current = runRef.current
    if (!current || !canAnswerClarification(current, item)) throw new Error('任务状态已变化，请刷新后重试。')
    await api.answerClarification(current.id, item.id, answer, requestId)
    if (selectedRef.current !== current.conversation_id || runRef.current?.id !== current.id) return
    await refreshClarifications(current.id, current.conversation_id)
    const version = ++snapshotVersion.current
    const latest = await api.run(current.id)
    if (acceptsSnapshot(version, snapshotVersion.current, selectedRef.current, current.conversation_id, runRef.current?.id, current.id)) setRun(old => mergeRunSnapshot(old, latest))
  }
  async function logout() {
    try { await api.logout(); setBootstrap(null); setHistory([]); setRun(null); setConversations([]); setModels([]); await loadBootstrap() }
    catch (reason) { notify(reason instanceof Error ? reason.message : '退出失败', true) }
  }
  if (bootError) return <div className="boot-screen"><Brand /><CircleAlert size={34} /><h1>暂时无法连接本地服务</h1><p>{bootError}</p><p className="muted">请确认 OmniHarness 后端正在运行。</p><Button variant="primary" onClick={() => void loadBootstrap()}>重新连接</Button></div>
  if (!bootstrap) return <div className="boot-screen"><Brand /><LoaderCircle className="spin" size={26} /><p>正在连接工作台…</p></div>
  if (!bootstrap.authenticated) return <Pairing onPaired={() => void loadBootstrap()} />
  return <div className="app-shell">
    {sidebarOpen && <button className="sidebar-shade" aria-label="关闭侧边栏" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? 'sidebar-open' : ''}`}>
      <div className="sidebar-brand"><Brand /><Button className="mobile-close" variant="ghost" size="icon" aria-label="关闭导航" onClick={() => setSidebarOpen(false)}><X size={18} /></Button></div>
      <Button className="new-chat" onClick={newChat} disabled={sending}><Plus size={17} />新建对话<span>+</span></Button>
      <div className="sidebar-section-label">工作空间</div>
      <button className={`nav-link ${page === 'chat' ? 'nav-active' : ''}`} onClick={() => { setPage('chat'); setSidebarOpen(false) }}><MessageSquare size={17} />对话<ChevronRight size={14} /></button>
      <button className={`nav-link ${page === 'models' ? 'nav-active' : ''}`} onClick={() => { setPage('models'); setSidebarOpen(false) }}><Settings2 size={17} />模型连接<span className="nav-count">{models.length}</span></button>
      <button className={`nav-link ${page === 'orchestration' ? 'nav-active' : ''}`} onClick={() => { setPage('orchestration'); setSidebarOpen(false) }}><GitBranch size={17} />编排<ChevronRight size={14} /></button>
      <div className="recents-label"><span>最近对话</span><span>{conversations.length || ''}</span></div>
      {conversations.length > 5 && <div className="conversation-search"><Search size={13} /><input aria-label="搜索会话" placeholder="搜索对话" value={search} onChange={event => setSearch(event.target.value)} /></div>}
      <nav className="conversation-list" aria-label="最近对话">{visibleConversations.map(item => <button key={item.id} className={`conversation-link ${selected === item.id && page === 'chat' ? 'conversation-selected' : ''}`} onClick={() => chooseChat(item.id)} disabled={sending}><MessageSquare size={14} /><span>{item.title || '新对话'}</span></button>)}{!visibleConversations.length && <p className="no-conversations">{search ? '没有匹配的对话' : '你的对话会保存在这里'}</p>}</nav>
      <div className="sidebar-bottom"><div className="local-profile"><span className="local-avatar">O</span><div><strong>本地工作空间</strong><span><i />服务已连接</span></div><Button variant="ghost" size="icon" aria-label="退出当前浏览器配对" title="退出当前浏览器配对" onClick={() => void logout()}><LogOut size={15} /></Button></div><div className="version-label">OmniHarness <span>{bootstrap.version} · {bootstrap.stage}</span></div></div>
    </aside>
    <div className="workspace"><header className="workspace-header"><div className="header-leading"><Button className="mobile-menu" variant="ghost" size="icon" aria-label="打开导航" onClick={() => setSidebarOpen(true)}><Menu size={19} /></Button><span className="breadcrumb">工作空间</span><ChevronRight size={13} /><strong>{page === 'models' ? '模型连接' : page === 'orchestration' ? '编排配置' : conversation?.title || '新对话'}</strong></div><div className="header-actions">{bootstrap.v6_preview === true && <Badge tone="amber">{previewBadge(bootstrap.workspace_workflow_version)}</Badge>}<span className="local-chip"><LockKeyhole size={12} />本地服务</span>{page === 'chat' && <Button variant={detailsOpen ? 'secondary' : 'ghost'} size="small" onClick={() => { if (routingOpen) { setRoutingOpen(false); setDetailsOpen(true) } else setDetailsOpen(!detailsOpen) }} aria-expanded={detailsOpen && !routingOpen}><PanelRightOpen size={15} /><span>运行详情</span></Button>}</div></header>
      {page === 'models' ? <ModelSettings models={models} refresh={refreshModels} notify={notify} /> : page === 'orchestration' ? <Orchestration models={models} defaultModelId={modelId} notify={notify} /> : <div className="chat-layout"><main className="chat-main">
        <div className={`chat-scroll ${!hasMessages ? 'chat-scroll-empty' : ''}`} ref={scrollRef} onScroll={() => { const element = scrollRef.current; if (element) nearBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100 }}>
          {loadingHistory ? <div className="history-loading"><LoaderCircle size={21} className="spin" />正在读取对话…</div> : !hasMessages ? <div className="welcome"><span className="welcome-symbol"><Sparkles size={30} strokeWidth={1.6} /></span><span className="eyebrow">ONE WORKSPACE. YOUR MODELS.</span><h1>把想法，变成下一步。</h1><p>连接你信任的模型，从一段对话开始。<br />过程清晰可见，数据由你掌控。</p><div className="welcome-prompts">{[{ icon: <FileText size={19} />, title: '理清一个想法', text: '帮我梳理一个项目想法，从目标、约束和下一步三个方面开始。', detail: '把零散思路整理成行动' }, { icon: <Code2 size={19} />, title: '探讨技术问题', text: '我想讨论一个技术实现问题，请先帮我明确需要提供哪些信息。', detail: '一起理解、分析与推演' }, { icon: <MessageSquare size={19} />, title: '自由聊一聊', text: '你好，请介绍你擅长帮助我完成哪些工作。', detail: '从你的第一个问题开始' }].map(item => <button key={item.title} onClick={() => { setDraft(item.text); textRef.current?.focus() }}><span>{item.icon}</span><strong>{item.title}</strong><small>{item.detail}</small><ArrowRight size={14} /></button>)}</div>{!enabledModels.length && <div className="welcome-setup"><KeyRound size={16} /><span>先添加一个模型连接，开始你的第一次对话。</span><button onClick={() => setPage('models')}>配置模型<ArrowRight size={13} /></button></div>}</div> : <div className="messages">{visibleMessages.map(message => { const historical = message.role === 'assistant' ? historicalClarificationRun(conversationRuns, message.run_id, run?.id) : undefined; return <Fragment key={message.id}><MessageBubble message={message} streaming={active && run?.status !== 'paused' && run?.status !== 'awaiting_approval' && run?.status !== 'awaiting_user' && run?.status !== 'pending_review' && message.role === 'assistant' && message.run_id === run?.id} notify={notify} onModelSettings={() => setPage('models')} />{historical && <HistoricalClarifications key={historical.id} runId={historical.id} status={historical.status} />}</Fragment> })}{conversationRuns.length >= 100 && <p className="detail-hint">会话运行列表最多加载最近 100 条，更早任务的问答入口可能未显示。</p>}{active && run?.workflow_type === 'agent' && run.status !== 'awaiting_user' && <div className="agent-progress" role="status"><span>{latestProgress(stream.events)}</span><button type="button" onClick={() => { setRoutingOpen(false); setDetailsOpen(true) }}>查看运行详情</button></div>}{run && <ClarificationPanel items={clarifications} status={run.status} onAnswer={answerClarification} />}{clarificationError && run && <div className="chat-run-notice is-error" role="alert"><span>{clarificationError}</span><button onClick={() => void refreshClarifications(run.id, run.conversation_id)}>重新加载问题</button></div>}{run?.status === 'awaiting_user' && !clarifications.some(item => item.status === 'pending') && !clarificationError && <div className="chat-run-notice" role="status">正在加载等待回答的问题…</div>}{active && run?.status !== 'pending_review' && run?.status !== 'awaiting_user' && !visibleMessages.some(message => message.run_id === run?.id && message.role === 'assistant') && <div className="waiting-response"><span className="assistant-avatar"><Sparkles size={16} /></span><LoaderCircle size={16} className="spin" /><span>{run ? statusLabels[run.status] : '等待响应'}</span></div>}{run && ['failed', 'interrupted', 'cancelled', 'pending_review'].includes(run.status) && !visibleMessages.some(message => message.run_id === run.id && message.role === 'assistant' && ['failed', 'interrupted', 'cancelled'].includes(message.status)) && <div className={`chat-run-notice ${run.status === 'cancelled' ? '' : 'is-error'}`} role="status"><CircleAlert size={16} /><span>{run.status === 'cancelled' ? '本次生成已停止。你可以继续发送消息。' : run.error_message || '本次运行未完成，请查看运行详情或重新发送。'}</span></div>}</div>}
        </div>
        <div className="composer-area">{bootstrap.v6_preview === true && <div className="composer-warning" role="status"><ShieldCheck size={13} /><span>{active && run ? previewActiveNotice(run) : previewNotice(bootstrap.stage_workflow_version, bootstrap.workspace_workflow_version, workflowType, Boolean(workspaceId), agentStrategy, strategyLabels[agentStrategy])}</span></div>}{workspaceId && workflowType === 'agent' && bootstrap.workspace_workflow_version === 'stage-agent-v10' && !active && <ActionRequirements enabled={autoCorrection} onEnabled={setAutoCorrection} drafts={actionDrafts} onDrafts={setActionDrafts} disabled={sending} />}<WorkspacePicker value={workspaceId} onChange={value => { if (value !== workspaceId) setActionDrafts([]); setWorkspaceId(value); if (value) setWorkflowType('agent') }} disabled={active || sending} /><HistoryScopePicker messages={history.filter(message => message.conversation_id === selected)} workflowType={workflowType} workspaceId={workspaceId} chatWorkspaceId={chatHistoryWorkspaceId} onChatWorkspaceChange={setChatHistoryWorkspaceId} additionalRunIds={additionalHistoryRunIds} onAdditionalChange={setAdditionalHistoryRunIds} modelLabel={active ? shownRunModel.frozenLabel ?? '本次任务模型未记录' : selectedModel ? modelDisplayName(selectedModel) : '所选模型'} disabled={active || sending || loadingHistory} />{workspaceId && workflowType === 'agent' && <CollaborationMode value={agentStrategy} disabled={active || sending} onChange={value => { setAgentStrategy(value); if (value === 'single') setRoutingOpen(false); localStorage.setItem('omni.agent.strategy', value); if (routingPlan) setCurrentRoutingPlan({ ...routingPlan, acceptedTask: null }) }} />}<div className={`routing-default-line ${!active && (!selectedModel || !selectedModel.enabled) ? 'is-missing' : ''}`}><span>{active ? '本次任务默认模型' : '下次任务默认模型'}</span><strong title={active ? shownRunModel.frozenLabel : selectedModel ? modelDisplayName(selectedModel) : undefined}>{active ? shownRunModel.frozenLabel : selectedModel?.enabled ? modelDisplayName(selectedModel) : modelId ? '所选模型不可用，请重新选择' : '请在下方选择模型'}</strong>{shownRunModel.stageNote && <small>各阶段可能使用其他模型</small>}</div>{workflowType === 'agent' && !active && <div className="composer-warning"><GitBranch size={13} />{routingAccepted ? '本次任务将使用已确认的模型方案。' : routingPlan ? '当前方案尚未应用，将使用已保存的阶段配置。' : agentStrategy === 'single' && workspaceId ? '单 Agent 使用下方所选模型，不调用其他角色。' : `任务将使用${strategyLabels[workspaceId ? agentStrategy : 'staged']}及已保存的模型配置。`}<button type="button" onClick={() => setPage('orchestration')}>查看推荐并调整</button></div>}{!active && selectedModel?.adapter_type === 'mock' && <div className="composer-warning"><FlaskConical size={13} />正在使用 Mock 演示模型，不会调用真实 API。</div>}{!active && missingCredential && <div className="composer-warning"><KeyRound size={13} />此连接尚未配置密钥。<button onClick={() => setPage('models')}>前往配置</button></div>}<form className={`composer ${active ? 'composer-active' : ''}`} onSubmit={send}><textarea ref={textRef} value={draft} onChange={event => setDraft(event.target.value)} placeholder={run?.status === 'awaiting_user' ? '请在上方问题卡片中回答…' : run?.status === 'pending_review' ? '任务等待核对，可停止当前任务…' : run?.status === 'paused' ? run.workflow_type === 'agent' ? '任务已暂停，可在运行详情中继续或停止…' : '任务已暂停，可停止；尚未开始的任务可在运行详情中继续…' : active ? '等待当前回答完成后，继续对话…' : '输入问题，或描述你想完成的事情…'} aria-label="消息内容" rows={2} disabled={sending || loadingHistory || run?.status === 'awaiting_user'} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} /><div className="composer-toolbar"><div className="model-selector workflow-selector"><select aria-label="运行模式" value={displayedWorkflowType(active, run, workflowType)} onChange={event => { const next = event.target.value as WorkflowType; setWorkflowType(next); if (next === 'chat' && routingPlan) setCurrentRoutingPlan({ ...routingPlan, acceptedTask: null }) }} disabled={active || sending}><option value="unknown" disabled>当前任务模式未记录</option><option value="chat">普通聊天</option><option value="agent" disabled={bootstrap.agent_available !== true}>Agent 任务{bootstrap.agent_available === true ? '' : ' · 暂不可用'}</option></select></div><ModelPicker models={enabledModels} value={shownRunModel.id} onChange={setModelId} label="选择模型" disabled={active || sending} className="composer-model-picker" frozenLabel={shownRunModel.frozenLabel} frozenSubtitle={shownRunModel.frozenSubtitle} /><div className="composer-actions"><Button type="button" className={`routing-launch ${routingOpen || routingAccepted ? 'is-active' : ''}`} onClick={() => { setRoutingOpen(true); setDetailsOpen(false) }} disabled={Boolean(workspaceId && agentStrategy === 'single')} aria-label="AI 规划模型"><Sparkles size={15} />{routingAccepted ? '已选模型方案' : 'AI 规划模型'}</Button><span>Shift + Enter 换行</span>{active ? <Button type="button" className="send-button stop-button" variant="secondary" size="icon" busy={stopping} disabled={!canStopRun(run?.status)} aria-label="停止生成" onClick={() => void stop()}><Square size={15} fill="currentColor" /></Button> : <Button type="submit" className="send-button" variant="primary" size="icon" busy={sending} aria-label="发送消息" disabled={!draft.trim() || !selectedModel || !selectedModel.enabled || loadingHistory || missingCredential || (workflowType === 'agent' && bootstrap.agent_available !== true)}><ArrowUp size={20} /></Button>}</div></div></form><p className="composer-note"><ShieldCheck size={11} />对话保存在本地。模型回答可能有误，请核实重要信息。</p></div>
      </main>{routingOpen ? <ModelRoutingPlanner strategy={workspaceId ? agentStrategy : 'staged'} task={draft} conversationKey={routingKey} models={models} defaultModelId={modelId} plan={routingPlan} active={active} agentAvailable={bootstrap.agent_available === true} onChange={setCurrentRoutingPlan} onApply={() => { if (!routingPlan || !draft.trim()) return; setCurrentRoutingPlan({ ...routingPlan, acceptedTask: draft.trim() }); setWorkflowType('agent'); notify(active ? '方案已应用于下一次任务。' : '方案已应用于本次任务。') }} onClose={() => setRoutingOpen(false)} /> : detailsOpen && <RunDetails run={run} events={stream.events} connected={connected} agentRuns={agentRuns} agentRunsError={agentRunsError} onReloadAgentRuns={() => { if (run) void refreshAgentRuns(run.id, run.conversation_id) }} onClose={() => setDetailsOpen(false)} controls={run ? <RunControls run={run} commands={commands} budget={budget} events={stream.events} busy={controlBusy} onSubmit={submitControl} /> : null} />}</div>}
    </div>
    {toast && <div className={`toast ${toast.error ? 'toast-error' : ''}`} role={toast.error ? 'alert' : 'status'}>{toast.error ? <CircleAlert size={17} /> : <Check size={17} />}<span>{toast.message}</span><button aria-label="关闭通知" onClick={() => setToast(null)}><X size={15} /></button></div>}
  </div>
}
