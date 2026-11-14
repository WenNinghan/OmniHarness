import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown, CornerDownLeft, Search, SlidersHorizontal, X } from 'lucide-react'
import type { ModelConfig, Stage } from '../lib/types.ts'
import { comparisonMix, evidencePrice, filterModels, formatOutputLimit, formatPriceRatio, groupModels, modelDisplayName, modelFamily, modelHost, parsePricePreferences, pricePreferencesKey, relativePrice, validMix } from '../lib/model-picker.ts'
import type { PricePreferences } from '../lib/model-picker.ts'
import { referencePrice } from '../lib/reference-prices.ts'
import { readRoutingEvidence } from './RoutingEvidence.tsx'
import './ModelPicker.css'

export interface ModelPickerProps {
  models: ModelConfig[]
  value: string
  onChange: (id: string) => void
  label: string
  id?: string
  disabled?: boolean
  className?: string
  placeholder?: string
  frozenLabel?: string
  frozenSubtitle?: string
  inherit?: { modelId: string; label?: string }
  stage?: Stage
}

function readPreferences(): PricePreferences {
  try { return parsePricePreferences(localStorage.getItem(pricePreferencesKey)) }
  catch { return parsePricePreferences(null) }
}

export function ModelPicker({ models, value, onChange, label, id, disabled, className = '', placeholder = '选择模型', inherit, stage, frozenLabel, frozenSubtitle }: ModelPickerProps) {
  const generated = useId()
  const listId = `model-list-${generated}`
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const search = useRef<HTMLInputElement>(null)
  const priceDetails = useRef<HTMLDetailsElement>(null)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const [position, setPosition] = useState<CSSProperties>({ visibility: 'hidden' })
  const [preferences, setPreferences] = useState(readPreferences)
  const [evidence, setEvidence] = useState(readRoutingEvidence)
  const [at, setAt] = useState(() => new Date())
  const [storageError, setStorageError] = useState(false)
  const [inspectedId, setInspectedId] = useState<string | null>(null)
  const enabled = useMemo(() => models.filter(model => model.enabled), [models])
  const selected = enabled.find(model => model.id === value)
  const inherited = inherit ? enabled.find(model => model.id === inherit.modelId) : undefined
  // A running task's catalog entry may have changed since submission. Do not
  // display today's price, limits or family as if they were frozen facts.
  const actual = frozenLabel !== undefined ? undefined : value ? selected : inherited
  const groups = useMemo(() => groupModels(filterModels(enabled, query)), [enabled, query])
  const showInherited = Boolean(inherit && (!query.trim() || `继承默认 ${inherited ? modelDisplayName(inherited) : ''}`.toLowerCase().includes(query.toLowerCase())))
  const items = [...(showInherited ? [''] : []), ...groups.flatMap(group => group.models.map(model => model.id))]
  const activeId = items[Math.min(activeIndex, Math.max(0, items.length - 1))]
  // A removed explicit baseline stays unknown until the user selects another one.
  const baseline = preferences.baselineId ? enabled.find(model => model.id === preferences.baselineId)
    : enabled.find(model => model.model_name.toLowerCase() === 'deepseek-flash')
  const mix = comparisonMix(preferences, evidence, stage)
  const quote = (model: ModelConfig | undefined) => {
    if (!model) return null
    const own = evidence.models[model.id]
    // An incomplete user quote is unknown, not silently replaced by another source.
    if (own && [own.input, own.output, own.currency, own.source].some(value => value.trim())) return evidencePrice(model, evidence)
    return referencePrice(model, at)
  }
  const baselineQuote = quote(baseline)
  const ratio = (model: ModelConfig | undefined) => relativePrice(quote(model), baselineQuote, mix.input, mix.output)
  const activeModel = activeId === '' ? inherited : enabled.find(model => model.id === activeId)
  const inspectedModel = inspectedId ? enabled.find(model => model.id === inspectedId) : activeModel
  const activeQuote = quote(inspectedModel)
  const activeRatio = ratio(inspectedModel)
  const subtitle = frozenLabel !== undefined ? frozenSubtitle ?? '本次任务模型 · 已冻结' : value ? selected ? modelHost(selected) : '已选连接不可用' : inherit ? '沿用任务默认模型' : '按系列浏览或搜索'
  const title = frozenLabel !== undefined ? frozenLabel : value ? selected ? modelDisplayName(selected) : '请重新选择模型'
    : inherit ? `${inherit.label ?? '继承默认'}${inherited ? ` · ${modelDisplayName(inherited)}` : ' · 尚未设置'}` : placeholder

  function close() {
    setOpen(false)
    trigger.current?.focus()
  }
  function choose(next: string) {
    onChange(next)
    close()
  }
  function updatePreferences(patch: Partial<PricePreferences>) {
    const next = { ...preferences, ...patch }
    if (!validMix(next.input, next.output)) return
    setPreferences(next)
    try {
      localStorage.setItem(pricePreferencesKey, JSON.stringify(next))
      window.dispatchEvent(new Event('omni-price-preferences'))
      setStorageError(false)
    } catch { setStorageError(true) }
  }
  function start() {
    if (disabled) return
    setQuery('')
    setInspectedId(null)
    setEvidence(readRoutingEvidence())
    setPreferences(readPreferences())
    setAt(new Date())
    const ordered = [...(inherit ? [''] : []), ...groupModels(enabled).flatMap(group => group.models.map(model => model.id))]
    setActiveIndex(Math.max(0, ordered.indexOf(value)))
    setOpen(true)
  }

  useEffect(() => {
    const refresh = () => setPreferences(readPreferences())
    window.addEventListener('storage', refresh)
    window.addEventListener('omni-price-preferences', refresh)
    return () => { window.removeEventListener('storage', refresh); window.removeEventListener('omni-price-preferences', refresh) }
  }, [])
  useEffect(() => { if (disabled && open) setOpen(false) }, [disabled, open])
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const rect = trigger.current?.getBoundingClientRect()
      if (!rect) return
      const width = Math.min(420, window.innerWidth - 24)
      const below = window.innerHeight - rect.bottom - 20
      const above = rect.top - 20
      const useAbove = below < 440 && above > below
      setPosition({ left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)), width,
        ...(useAbove ? { bottom: window.innerHeight - rect.top + 9 } : { top: rect.bottom + 9 }),
        maxHeight: Math.min(650, Math.max(260, useAbove ? above : below)) })
    }
    place()
    search.current?.focus({ preventScroll: true })
    const onScroll = (event: Event) => { if (!panel.current?.contains(event.target as Node)) place() }
    window.addEventListener('resize', place)
    window.addEventListener('scroll', onScroll, true)
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', onScroll, true) }
  }, [open])
  useEffect(() => {
    if (open) panel.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' })
  }, [open, activeId])

  function navigate(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (!items.length) return
      setActiveIndex(index => (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length)
    } else if (event.key === 'Enter') {
      event.preventDefault()
      if (activeId !== undefined) choose(activeId)
    } else if (event.key === 'Home' && event.ctrlKey) { event.preventDefault(); setActiveIndex(0) }
    else if (event.key === 'End' && event.ctrlKey) { event.preventDefault(); setActiveIndex(items.length - 1) }
  }
  function trap(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return }
    if (event.key !== 'Tab') return
    const focusable = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not([disabled]):not([tabindex="-1"]),input:not([disabled]),select:not([disabled]),summary') ?? [])
      .filter(element => element.offsetParent !== null)
    const first = focusable[0], last = focusable.at(-1)
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
  }

  return <>
    <button ref={trigger} id={id} type="button" className={`model-picker-trigger ${className}`} aria-label={label}
      aria-haspopup="dialog" aria-expanded={open} disabled={disabled} title={title} onClick={() => open ? close() : start()}
      onKeyDown={event => { if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); start() } }}>
      <span className={`model-picker-mark family-${actual ? modelFamily(actual).toLowerCase() : 'default'}`} aria-hidden="true">{actual ? modelFamily(actual).slice(0, 1) : 'M'}</span>
      <span className="model-picker-trigger-copy"><strong>{title}</strong><small>{actual && `输出上限 ${formatOutputLimit(actual.max_output_tokens)} · `}{subtitle}</small></span>
      {actual && ratio(actual) !== null && <span className="model-picker-trigger-price">{formatPriceRatio(ratio(actual))}</span>}
      <ChevronDown size={16} className="model-picker-chevron" aria-hidden="true" />
    </button>
    {open && createPortal(<div className="model-picker-layer">
      <div className="model-picker-scrim" onMouseDown={event => { event.preventDefault(); close() }} aria-hidden="true" />
      <div ref={panel} className="model-picker-panel" role="dialog" aria-modal="true" aria-label={`${label}选项`} style={position} onKeyDown={trap}>
        <div className="model-picker-mobile-handle" />
        <header className="model-picker-heading"><div><strong>{label}</strong><span>{enabled.length} 个已配置模型</span></div>
          <button type="button" className="model-picker-close" aria-label="关闭模型选择" onClick={close}><X size={18} /></button></header>
        <div className="model-picker-search"><Search size={18} aria-hidden="true" /><input ref={search} value={query}
          placeholder="搜索名称、系列或连接…" aria-label="搜索模型" role="combobox" aria-autocomplete="list" aria-expanded="true"
          aria-controls={listId} aria-activedescendant={activeId !== undefined ? `${listId}-${activeId || 'inherit'}` : undefined}
          onChange={event => { setQuery(event.target.value); setActiveIndex(0) }} onKeyDown={navigate} />
          {query && <button type="button" aria-label="清空模型搜索" onClick={() => { setQuery(''); setActiveIndex(0); search.current?.focus() }}><X size={14} /></button>}</div>
        <div className="model-picker-list" role="listbox" aria-label="可用模型" id={listId}>
          {showInherited && <button type="button" role="option" tabIndex={-1} id={`${listId}-inherit`} aria-selected={!value}
            className="model-picker-option model-picker-inherit" data-active={activeId === ''} onMouseMove={() => setActiveIndex(0)} onClick={() => choose('')}>
            <span className="model-picker-mark" aria-hidden="true">↳</span><span className="model-picker-option-copy"><strong>{inherit?.label ?? '继承默认'}</strong><small>{inherited ? `输出上限 ${formatOutputLimit(inherited.max_output_tokens)} · ${modelDisplayName(inherited)}` : '尚未设置默认模型'}</small></span>
            {!value && <Check className="model-picker-check" size={17} aria-hidden="true" />}</button>}
          {groups.map(group => <div className="model-picker-group" key={group.family} role="group" aria-label={group.family}>
            <div className="model-picker-group-label" aria-hidden="true"><span>{group.family}</span><span>{group.models.length}</span></div>
            {group.models.map(model => {
              const displayName = modelDisplayName(model)
              const modelRatio = ratio(model)
              const details = quote(model)
              const hasAlias = model.name.trim().toLowerCase() !== model.model_name.toLowerCase()
              return <div className="model-picker-row" key={model.id}><button type="button" role="option" tabIndex={-1} id={`${listId}-${model.id}`} aria-selected={value === model.id}
                className="model-picker-option" data-active={activeId === model.id} onMouseMove={() => setActiveIndex(items.indexOf(model.id))} onClick={() => choose(model.id)}
                title={`${model.model_name} · ${modelHost(model)}\n输出上限 ${model.max_output_tokens.toLocaleString('en-US')} tokens${details ? `\n${details.source}${details.observedAt ? ` · ${details.observedAt}` : ''}\n${details.note ?? ''}` : '\n尚无可比较的价格数据'}`}>
                <span className={`model-picker-mark family-${group.family.toLowerCase()}`} aria-hidden="true">{group.family.slice(0, 1)}</span>
                <span className="model-picker-option-copy"><strong>{displayName}</strong><small>输出上限 {formatOutputLimit(model.max_output_tokens)} · {hasAlias ? `${model.model_name} · ` : ''}{modelHost(model)}{model.adapter_type === 'mock' ? ' · MOCK' : ''}</small></span>
                <span className="model-picker-price-space" aria-hidden="true" />
                <span className="model-picker-check">{value === model.id && <Check size={17} aria-hidden="true" />}</span>
              </button><button type="button" tabIndex={-1} className={`model-picker-price ${modelRatio === null ? 'is-unknown' : ''}`}
                aria-label={`查看 ${displayName} 的价格依据`} onClick={() => {
                  setInspectedId(model.id)
                  if (priceDetails.current) priceDetails.current.open = true
                }}>{formatPriceRatio(modelRatio)}</button></div>
            })}</div>)}
          {!items.length && <div className="model-picker-empty"><Search size={23} /><strong>没有匹配的模型</strong><p>试试系列名称，或更短的关键词。</p></div>}
        </div>
        <footer className="model-picker-footer">
          <details ref={priceDetails} className="model-picker-price-settings"><summary><SlidersHorizontal size={13} /><span>价格说明</span><span className="model-picker-baseline">{baseline ? modelDisplayName(baseline) : '未选基准'} = 1×</span><ChevronDown size={13} /></summary>
            <div className="model-picker-price-fields"><label>比较基准<select aria-label="价格比较基准" value={baseline?.id ?? ''} onChange={event => updatePreferences({ baselineId: event.target.value })}>
              <option value="" disabled>请选择基准模型</option>{enabled.map(model => <option key={model.id} value={model.id}>{modelDisplayName(model)}</option>)}</select></label>
              <div className="model-picker-mix"><span>输入 : 输出</span><label><span className="model-picker-sr">输入权重</span><input aria-label="价格比较输入权重" type="number" min={0} max={10000000} step={1} value={preferences.input} disabled={mix.stageBased} onChange={event => { if (event.target.value !== '') updatePreferences({ input: Number(event.target.value) }) }} /></label><span>:</span><label><span className="model-picker-sr">输出权重</span><input aria-label="价格比较输出权重" type="number" min={0} max={10000000} step={1} value={preferences.output} disabled={mix.stageBased} onChange={event => { if (event.target.value !== '') updatePreferences({ output: Number(event.target.value) }) }} /></label></div>
              <p>{mix.stageBased ? `当前阶段按已填写的 ${mix.input} 输入 / ${mix.output} 输出 Token 比较。` : `按 ${mix.input}:${mix.output} 用量、无缓存命中比较。`}偏好仅保存到本浏览器。</p>
              {inspectedModel && <p><strong>{modelDisplayName(inspectedModel)} · {formatPriceRatio(activeRatio)}</strong></p>}
              <p>{activeQuote && activeRatio !== null ? `${activeQuote.basis === 'user_estimate' ? '用户估计' : '最高档参考'} · ${activeQuote.source}${activeQuote.observedAt ? ` · ${activeQuote.observedAt}` : ''}。${activeQuote.note ?? ''}` : '缺少同口径报价时保持未知；公开价与用户估计分开比较。'}倍率仅作参考，不代表账户实际扣费。</p>
              {storageError && <p role="status">浏览器未能保存偏好，本次选择仍然有效。</p>}
            </div>
          </details>
          <div className="model-picker-shortcuts"><span>↑ ↓ 浏览</span><span><CornerDownLeft size={11} /> 选择</span><span>Esc 关闭</span><span>{mix.input}:{mix.output} · 估计</span></div>
        </footer>
      </div>
    </div>, document.body)}
  </>
}
