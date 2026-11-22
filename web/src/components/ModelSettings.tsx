import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ArrowDownToLine, ArrowUpFromLine, Check, ChevronDown, CircleHelp, FlaskConical, KeyRound, Pencil, Plus, Power, Radio, Server, ShieldCheck, X } from 'lucide-react'
import { api } from '../lib/api.ts'
import { ContextSettings } from './ContextSettings.tsx'
import { contextFromForm } from '../lib/context-policy.ts'
import { formatOutputLimit } from '../lib/model-picker.ts'
import type { AdapterType, ModelConfig, ModelInput, ModelCatalog, Probe } from '../lib/types.ts'
import { Badge, Button, Modal } from './ui.tsx'

const adapters: { value: AdapterType; label: string }[] = [
  { value: 'openai', label: 'OpenAI' }, { value: 'anthropic', label: 'Anthropic' },
  { value: 'gemini', label: 'Google Gemini' }, { value: 'openai-compatible', label: 'OpenAI 兼容接口' }, { value: 'mock', label: 'Mock · 演示模型' },
]
const probeNames: Record<Probe, string> = { chat: '基本对话', streaming: '流式响应', tools: '工具调用', structured: '结构化输出', summary_non_thinking: '摘要推理模式', json_mode: 'JSON 模式' }
const probes: Probe[] = ['chat', 'streaming', 'tools', 'structured']
type Notify = (message: string, error?: boolean) => void

function ModelForm({ initial, onClose, onSaved }: { initial?: ModelConfig; onClose: () => void; onSaved: () => Promise<void> }) {
  const [adapter, setAdapter] = useState<AdapterType>(initial?.adapter_type ?? 'openai-compatible')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [advanced, setAdvanced] = useState(false)
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(''); setBusy(true)
    const data = new FormData(event.currentTarget)
    try {
      const parameters = JSON.parse(String(data.get('parameters') || '{}'))
      if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)) throw new Error('高级参数必须是 JSON 对象。')
      const key = String(data.get('api_key') || '')
      const input: ModelInput = {
        name: String(data.get('name')).trim(), adapter_type: adapter, model_name: String(data.get('model_name')).trim(),
        base_url: String(data.get('base_url') || '').trim(), enabled: data.get('enabled') === 'on',
        parameters, timeout_seconds: Number(data.get('timeout_seconds') || 120), max_output_tokens: Number(data.get('max_output_tokens') || 100000),
        context_policy: contextFromForm(data, initial?.context_policy),
        ...(key || !initial ? { api_key: key } : {}),
        ...(data.get('clear_key') === 'on' ? { api_key: '' } : {}),
      }
      await api.saveModel(input, initial?.id); await onSaved(); onClose()
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败。') }
    finally { setBusy(false) }
  }
  return <Modal title={initial ? '编辑模型连接' : '添加模型连接'} onClose={() => { if (!busy) onClose() }}>
    <form onSubmit={save} className="model-form">
      <p className="muted">连接你自己的模型 API。密钥仅保存在本地服务端。</p>
      <label>显示名称<input name="name" defaultValue={initial?.name} placeholder="例如：我的日常助手" required maxLength={100} autoFocus /></label>
      <div className="form-columns"><label>服务商<select value={adapter} onChange={event => setAdapter(event.target.value as AdapterType)}>{adapters.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label><label>模型名称<input name="model_name" defaultValue={initial?.model_name} placeholder={adapter === 'mock' ? 'mock' : '输入服务商提供的模型 ID'} required /></label></div>
      {adapter === 'mock' ? <div className="notice notice-amber"><FlaskConical size={18} /><span>这是模拟模型，用于验证界面与流式链路，不会请求真实模型，也不代表真实推理结果。</span></div> : <>
        <label>API 地址 <span className="optional">{adapter === 'openai-compatible' ? '必填' : '可选，留空使用官方地址'}</span><input type="url" name="base_url" defaultValue={initial?.base_url ?? ''} placeholder="https://api.example.com/v1" required={adapter === 'openai-compatible'} /></label>
        <label>API Key <span className="optional">{initial?.key_configured ? '已配置，留空保留' : '可稍后填写'}</span><input type="password" name="api_key" autoComplete="new-password" placeholder={initial?.key_configured ? '留空保留现有密钥' : '输入 API Key'} /></label>
        {initial?.key_configured && <label className="checkbox-label"><input type="checkbox" name="clear_key" />清除已保存的密钥</label>}
      </>}
      <button type="button" className="advanced-toggle" onClick={() => setAdvanced(!advanced)}>高级参数<ChevronDown size={15} className={advanced ? 'rotate' : ''} /></button>
      <div className={advanced ? 'advanced-fields' : 'advanced-fields hidden'}><div className="form-columns"><label>请求超时（秒）<input type="number" name="timeout_seconds" min={5} max={3600} defaultValue={initial?.timeout_seconds ?? 120} /></label><label>最大输出 Token<input type="number" name="max_output_tokens" min={16} max={131072} defaultValue={initial?.max_output_tokens ?? 100000} /><span className="field-hint">默认 100K（100,000 tokens）；这是请求上限，实际支持范围由模型服务决定。</span></label></div><ContextSettings initial={initial?.context_policy} /><label>模型参数（JSON）<textarea name="parameters" rows={3} defaultValue={JSON.stringify(initial?.parameters ?? {}, null, 2)} spellCheck={false} /><span className="field-hint">可配置 temperature、top_p、seed、reasoning_effort、top_k 及 omni_stage_json_mode（true/false）；支持情况由服务商决定。</span></label></div>
      <label className="checkbox-label"><input type="checkbox" name="enabled" defaultChecked={initial?.enabled ?? true} />启用此模型</label>
      {error && <div className="notice notice-error" role="alert">{error}</div>}
      <div className="modal-actions"><Button type="button" onClick={onClose} disabled={busy}>取消</Button><Button type="submit" variant="primary" busy={busy}>{initial ? '保存修改' : '添加连接'}</Button></div>
    </form>
  </Modal>
}

function CatalogModal({ source, onClose, onImported }: { source: ModelConfig; onClose: () => void; onImported: () => Promise<void> }) {
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState('')
  async function fetchCatalog() {
    setLoading(true); setError(''); setResult('')
    try { const data = await api.modelCatalog(source.id); setCatalog(data); setSelected([]) }
    catch (reason) { setCatalog(null); setError(reason instanceof Error ? reason.message : '目录读取失败') }
    finally { setLoading(false) }
  }
  async function addModels() {
    if (!selected.length) return
    setLoading(true); setError(''); setResult('')
    try {
      const response = await api.importCatalog(source.id, selected)
      await onImported()
      const created = response.results.filter(item => item.status === 'created').length
      const existing = response.results.filter(item => item.status === 'existing').length
      setResult(`已添加 ${created} 个模型，${existing} 个模型已存在。`); setSelected([])
    } catch (reason) { setError(reason instanceof Error ? reason.message : '导入失败') }
    finally { setLoading(false) }
  }
  return <Modal title={`读取模型目录 · ${source.name}`} onClose={() => { if (!loading) onClose() }}>
    <div className="model-form">
      <p className="muted">将通过已配置的地址 {source.base_url || '（默认地址）'} 查询模型目录；导入时会再次查询确认。目录仅表示服务商列出的模型，能力和价格均需另外核实。</p>
      <p className="muted">导入的每个模型使用独立的连接与凭据副本。之后修改此连接的密钥不会自动同步到其他模型。</p>
      <Button size="small" busy={loading} onClick={() => void fetchCatalog()}>读取目录</Button>
      {catalog && <>
        <p className="field-hint">获取时间：{new Date(catalog.fetched_at).toLocaleString('zh-CN')}；共 {catalog.models.length} 项，已选 {selected.length} 项（最多 100 项）。</p>
        <div className="catalog-list">{catalog.models.map(model => <label className="checkbox-label" key={model.id}>
          <input type="checkbox" disabled={loading || (selected.length >= 100 && !selected.includes(model.id))} checked={selected.includes(model.id)} onChange={event => setSelected(old => event.target.checked ? [...old, model.id] : old.filter(item => item !== model.id))} />
          <code>{model.id}</code><span>能力 / 价格：未知</span>
        </label>)}</div>
        {!catalog.models.length && <p className="muted">服务商未返回模型。</p>}
      </>}
      {error && <div className="notice notice-error" role="alert">{error}</div>}
      {result && <div className="notice" role="status">{result}</div>}
      <div className="modal-actions"><Button onClick={onClose} disabled={loading}>关闭</Button><Button variant="primary" busy={loading} disabled={!selected.length} onClick={() => void addModels()}>导入所选模型</Button></div>
    </div>
  </Modal>
}

export function ModelSettings({ models, refresh, notify }: { models: ModelConfig[]; refresh: () => Promise<void>; notify: Notify }) {
  const [catalogSource, setCatalogSource] = useState<ModelConfig | null>(null)
  const [editing, setEditing] = useState<ModelConfig | 'new' | null>(null)
  const [testing, setTesting] = useState<string | null>(null)
  const [changing, setChanging] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  async function test(model: ModelConfig, requested: Probe[] = probes) {
    setTesting(model.id); setExpanded(model.id)
    try { await api.testModel(model.id, requested); await refresh() }
    catch (reason) { notify(reason instanceof Error ? reason.message : '测试失败', true) }
    finally { setTesting(null) }
  }
  async function toggle(model: ModelConfig) {
    setChanging(model.id)
    try {
      if (model.enabled) await api.disableModel(model.id)
      else await api.saveModel({ name: model.name, adapter_type: model.adapter_type, model_name: model.model_name, base_url: model.base_url ?? '', enabled: true, parameters: model.parameters, timeout_seconds: model.timeout_seconds, max_output_tokens: model.max_output_tokens }, model.id)
      await refresh()
    } catch (reason) { notify(reason instanceof Error ? reason.message : '更新失败', true) }
    finally { setChanging(null) }
  }
  async function exportConfig() {
    try {
      const config = await api.exportConfig()
      const url = URL.createObjectURL(new Blob([JSON.stringify(config, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a'); link.href = url; link.download = 'omniharness-config.json'; link.click(); URL.revokeObjectURL(url)
      notify('配置已导出，不包含 API Key。')
    } catch (reason) { notify(reason instanceof Error ? reason.message : '导出失败', true) }
  }
  async function importFile(file?: File) {
    if (!file) return
    setImporting(true)
    try {
      if (file.size > 2 * 1024 * 1024) throw new Error('配置文件不能超过 2 MB。')
      const config = JSON.parse(await file.text())
      if (config.schema_version !== 1 || !Array.isArray(config.model_configs)) throw new Error('配置格式不正确，请选择导出的 OmniHarness JSON 文件。')
      const result = await api.importConfig(config); await refresh(); notify(`已导入 ${result.imported} 个连接，请为新连接配置密钥。`)
    } catch (reason) { notify(reason instanceof Error ? reason.message : '导入失败', true) }
    finally { setImporting(false); if (inputRef.current) inputRef.current.value = '' }
  }
  return <main className="settings-page">
    <div className="page-heading"><div><span className="eyebrow">WORKSPACE SETTINGS</span><h1>模型连接</h1><p>你的模型，你的选择。统一管理 API 与连接能力。</p></div><Button variant="primary" onClick={() => setEditing('new')}><Plus size={16} />添加模型</Button></div>
    <div className="settings-summary"><div><span className="summary-icon"><Server size={19} /></span><div><strong>{models.length} 个连接</strong><span>{models.filter(model => model.enabled).length} 个已启用</span></div></div><div className="summary-note"><ShieldCheck size={17} /><span>凭据由本地服务保存，配置导出不含密钥</span></div></div>
    <div className="section-toolbar"><h2>已配置的模型</h2><div><input ref={inputRef} type="file" accept="application/json,.json" className="hidden" onChange={event => void importFile(event.target.files?.[0])} /><Button size="small" busy={importing} onClick={() => inputRef.current?.click()}><ArrowUpFromLine size={14} />导入</Button><Button size="small" onClick={() => void exportConfig()}><ArrowDownToLine size={14} />导出</Button></div></div>
    {models.length === 0 ? <div className="empty-models"><KeyRound size={30} /><h3>从第一个模型开始</h3><p>添加服务商与 API Key，即可开始对话。<br />也可以显式添加 Mock 模型体验界面。</p><Button variant="primary" onClick={() => setEditing('new')}><Plus size={15} />添加模型连接</Button></div> : <div className="model-list">{models.map(model => {
      const capability = model.capabilities ?? {}
      return <article className={`model-card ${!model.enabled ? 'model-disabled' : ''}`} key={model.id}>
        <div className="model-main"><div className={`provider-icon provider-${model.adapter_type}`}>{model.adapter_type === 'mock' ? <FlaskConical size={21} /> : <Radio size={21} />}</div><div className="model-identity"><div><h3>{model.name}</h3>{model.adapter_type === 'mock' && <Badge tone="amber">MOCK 演示</Badge>}{!model.enabled && <Badge>已禁用</Badge>}</div><p>{adapters.find(adapter => adapter.value === model.adapter_type)?.label} <span>·</span> <code>{model.model_name}</code> <span>·</span> 输出上限 {formatOutputLimit(model.max_output_tokens)}</p></div><div className="model-actions"><Button variant="ghost" size="icon" aria-label={`编辑 ${model.name}`} onClick={() => setEditing(model)}><Pencil size={16} /></Button><Button variant="ghost" size="icon" busy={changing === model.id} aria-label={`${model.enabled ? '禁用' : '启用'} ${model.name}`} onClick={() => void toggle(model)}><Power size={16} /></Button></div></div>
        <div className="model-footer"><span className={model.key_configured || ['mock', 'openai-compatible'].includes(model.adapter_type) ? 'credential-status' : 'credential-status missing'}><KeyRound size={13} />{model.adapter_type === 'mock' ? '模拟运行，无需密钥' : model.key_configured ? '密钥已配置' : model.adapter_type === 'openai-compatible' ? '免密连接' : '未配置密钥'}</span><div>{['openai-compatible', 'openai'].includes(model.adapter_type) && <Button variant="ghost" size="small" disabled={!model.enabled} onClick={() => setCatalogSource(model)}>模型目录</Button>}<Button variant="ghost" size="small" onClick={() => setExpanded(expanded === model.id ? null : model.id)}>能力详情<ChevronDown size={13} className={expanded === model.id ? 'rotate' : ''} /></Button><Button size="small" disabled={!model.enabled || testing !== null} busy={testing === model.id} onClick={() => void test(model)}>测试连接</Button></div></div>
        {expanded === model.id && <div className="capabilities"><p>四项探测会请求该模型，真实 API 可能产生费用。</p><p>摘要模式单独探测一次，参数接受与实际观测分开记录；主任务参数不变。</p><div className="control-row"><Button size="small" disabled={testing !== null} onClick={() => void test(model, ['summary_non_thinking'])}>测试摘要模式</Button><Button size="small" disabled={testing !== null || !['openai', 'openai-compatible'].includes(model.adapter_type)} onClick={() => void test(model, ['json_mode'])}>测试 JSON 模式</Button></div><p className="field-hint">{capability.summary_non_thinking?.message ?? '摘要模式尚未实测。'}</p><p className="field-hint">{probeNames.json_mode}：{capability.json_mode?.status === 'supported' ? '支持 · ' : capability.json_mode?.status === 'unsupported' ? '不支持 · ' : capability.json_mode?.status === 'error' ? '测试失败 · ' : ''}{capability.json_mode?.message ?? 'JSON 模式尚未实测。'}</p><div className="probe-grid">{probes.map(probe => {
          const result = capability[probe]
          return <div className="probe-item" key={probe}><span>{probeNames[probe]}</span><span className={result?.status === 'supported' ? 'probe-success' : result?.status === 'error' ? 'probe-error' : 'muted'}>{result?.status === 'supported' ? <Check size={14} /> : result?.status === 'unsupported' ? <X size={14} /> : <CircleHelp size={14} />}{result?.status === 'supported' ? '支持' : result?.status === 'unsupported' ? '不支持' : result?.status === 'error' ? '测试失败' : testing === model.id ? '测试中' : '未测试'}</span>{result?.message && <small>{result.message}</small>}</div>
        })}</div>{model.tested_at && <span className="test-time">上次测试：{new Date(model.tested_at).toLocaleString('zh-CN')}</span>}</div>}
      </article>
    })}</div>}
    <div className="settings-footnote"><CircleHelp size={16} /><p>模型支持的能力以实际测试为准。禁用连接会保留历史对话；配置导入会新增连接，不覆盖已有设置。</p></div>
    {catalogSource && <CatalogModal source={catalogSource} onClose={() => setCatalogSource(null)} onImported={refresh} />}
    {editing && <ModelForm initial={editing === 'new' ? undefined : editing} onClose={() => setEditing(null)} onSaved={refresh} />}
  </main>
}
