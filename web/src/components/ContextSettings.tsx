import { useState } from 'react'
import type { ContextConfiguration } from '../lib/types.ts'

type Version = 'context-v1' | 'context-v2' | 'context-v3'

export function ContextSettings({ initial }: { initial?: ContextConfiguration }) {
  const policy = initial?.policy
  const saved = policy?.version
  const [version, setVersion] = useState<Version>(
    saved === 'context-v1' || saved === 'context-v2' || saved === 'context-v3' ? saved : initial ? 'context-v1' : 'context-v2')
  const modern = version !== 'context-v1'
  const v3 = version === 'context-v3'
  return <fieldset className="context-settings"><legend>上下文整理</legend>
    <p className="field-hint">保留原始记录，摘要与恢复计入任务预算。主回答输出上限保持独立，保存后仅影响新任务。</p>
    <label>整理策略<select name="context_version" value={version} onChange={event => setVersion(event.target.value as Version)}>
      <option value="context-v3">v3 · 先确定性压缩旧工具结果，摘要失败时可降级继续</option>
      <option value="context-v2">v2 · 摘要去重，允许一次有界恢复</option>
      <option value="context-v1">v1 · 原始摘要策略</option>
    </select></label>
    <div className="form-columns">
      <label>输入目标 Token<input type="number" name="context_input_target" min={1000} key={version} defaultValue={policy?.version === version && policy?.input_target_tokens ? policy.input_target_tokens : v3 ? 32000 : 16000} /><span className="field-hint">填写模型总窗口后，实际可用输入还会扣除主回答输出预留。</span></label>
      <label>开始整理的比例（%）<input type="number" name="context_threshold" min={1} max={99} defaultValue={(policy?.summarize_at ?? .8) * 100} /></label>
    </div>
    <div className="form-columns">
      <label>摘要生成上限<input type="number" name="context_summary_output" min={128} defaultValue={policy?.summary_output_tokens ?? 4000} /><span className="field-hint">包含提供商计入的推理用量。</span></label>
      <label>模型总窗口 Token<input type="number" name="context_window" min={1} placeholder="未知，留空" defaultValue={initial?.model_limits?.context_window_tokens ?? ''} /></label>
    </div>
    {modern && <>
      <div className="form-columns">
        <label>摘要正文目标<input type="number" name="context_summary_target" min={128} defaultValue={policy?.summary_target_tokens ?? 1200} /><span className="field-hint">估算长度；完整代码和日志保留为工件。</span></label>
        <label>唯一恢复的生成上限<input type="number" name="context_recovery_output" min={128} defaultValue={policy?.summary_recovery_output_tokens ?? 8000} /></label>
      </div>
      <label className="checkbox-label"><input type="checkbox" name="context_recovery" defaultChecked={(policy?.summary_recovery_attempts ?? 1) === 1} />确认摘要因输出额度截断时，每个任务最多恢复一次</label>
      <label>摘要推理模式<select name="context_summary_thinking" defaultValue={policy?.summary_thinking ?? 'disable_if_verified'}><option value="disable_if_verified">实测支持时关闭，仅影响摘要</option><option value="inherit">继承所选模型模式</option></select></label>
    </>}
    {v3 && <>
      <label className="checkbox-label"><input type="checkbox" name="context_mask_tools" defaultChecked={policy?.mask_tool_results ?? true} />先把较早的工具结果替换为工件引用（不调用模型，原文可用 artifact_read 读回）</label>
      <div className="form-columns">
        <label>保留原文的最近工具交互<input type="number" name="context_mask_keep" min={1} max={20} defaultValue={policy?.mask_keep_recent_exchanges ?? 4} /></label>
        <label>摘要失败时<select name="context_summary_failure" defaultValue={policy?.summary_failure ?? 'degrade'}><option value="degrade">放得下就保留原记录继续</option><option value="fail">停止任务（同 v2）</option></select></label>
      </div>
      <label>回放推理的计数<select name="context_replayed_reasoning" defaultValue={policy?.replayed_reasoning ?? 'count'}><option value="count">计入（保守）</option><option value="ignore">不计入 · 仅限用量证据显示网关不对回放推理计费的模型</option></select></label>
    </>}
    {modern && <p className="field-hint">未知能力不会自动切换；超时或结果未知不会重发。上下文采用明确标注的估算，不代表供应商精确计数。</p>}
  </fieldset>
}
