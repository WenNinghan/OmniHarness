import test from 'node:test'
import assert from 'node:assert/strict'
import { latestProgress } from './progress-state.ts'
import type { RunEvent } from './types.ts'

const event = (event_type: string, payload: Record<string, unknown> = {}) => ({ event_type, payload }) as RunEvent
test('progress shows only an actually recorded event and known stage', () => {
  assert.equal(latestProgress([]), '等待运行事件')
  assert.equal(latestProgress([event('agent.model_started', { role: 'plan' })]), '任务规划 · 模型调用开始')
  assert.equal(latestProgress([event('tool.failed'), event('message.delta')]), '工具调用失败')
  assert.equal(latestProgress([event('agent.model_started', { role: 'untrusted text' })]), '模型调用开始')
})
