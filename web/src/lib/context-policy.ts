import type { ContextConfiguration } from './types.ts'

type ContextVersion = 'context-v1' | 'context-v2' | 'context-v3'

function selectedVersion(data: FormData): ContextVersion {
  const value = data.get('context_version')
  if (value === 'context-v1' || value === 'context-v2' || value === 'context-v3') return value
  // Older form markup only had the v2 checkbox.
  return data.get('context_v2') === 'on' ? 'context-v2' : 'context-v1'
}

export function contextFromForm(data: FormData, current?: ContextConfiguration): ContextConfiguration {
  const previous = current?.policy ?? {}
  const version = selectedVersion(data)
  const modern = version !== 'context-v1'
  const v3 = version === 'context-v3'
  const policy: NonNullable<ContextConfiguration['policy']> = {
    version,
    input_target_tokens: Number(data.get('context_input_target') || (v3 ? 32000 : 16000)),
    summarize_at: Number(data.get('context_threshold') || 80) / 100,
    summary_output_tokens: Number(data.get('context_summary_output') || 4000),
    keep_recent_tokens: previous.keep_recent_tokens ?? (v3 ? 8000 : 4000),
    window_margin_tokens: previous.window_margin_tokens ?? 1024,
    max_messages: previous.max_messages ?? 100,
    max_characters: previous.max_characters ?? 200000,
  }
  if (modern) Object.assign(policy, {
    counter_profile: 'unicode-weighted-v2',
    summary_target_tokens: Number(data.get('context_summary_target') || 1200),
    summary_recovery_output_tokens: Number(data.get('context_recovery_output') || 8000),
    summary_recovery_attempts: data.get('context_recovery') === 'on' ? 1 : 0,
    summary_min_new_tokens: previous.summary_min_new_tokens ?? 512,
    summary_min_gain: previous.summary_min_gain ?? .2,
    post_summary_target: previous.post_summary_target ?? .6,
    summary_thinking: data.get('context_summary_thinking') === 'inherit' ? 'inherit' : 'disable_if_verified',
  })
  if (v3) Object.assign(policy, {
    replayed_reasoning: data.get('context_replayed_reasoning') === 'ignore' ? 'ignore' : 'count',
    mask_tool_results: data.get('context_mask_tools') === 'on',
    mask_keep_recent_exchanges: Number(data.get('context_mask_keep') || 4),
    summary_failure: data.get('context_summary_failure') === 'fail' ? 'fail' : 'degrade',
  })
  return { schema_version: 1, policy, model_limits: { ...current?.model_limits,
    context_window_tokens: data.get('context_window') ? Number(data.get('context_window')) : null } }
}
