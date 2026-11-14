import test from 'node:test'
import assert from 'node:assert/strict'
import { displayedWorkflowType, previewActiveNotice, previewBadge, previewNotice } from './preview-state.ts'
import type { Run } from './types.ts'

test('preview versions reflect the frozen selection and never guess unknown versions', () => {
  assert.equal(previewBadge('stage-agent-v7'), '交互预览')
  assert.equal(previewBadge('stage-agent-v8'), '交互预览')
  assert.equal(previewBadge('stage-agent-v9'), '交互预览')
  assert.equal(previewBadge('stage-agent-v10'), '交互预览')
  assert.match(previewActiveNotice({ workflow_type: 'agent', workflow_version: 'stage-agent-v10' } as Run), /stage-agent-v10/)
  assert.match(previewNotice('stage-agent-interactive-v1', 'stage-agent-v9', 'agent', true, 'compact', '精简协作'), /交互候选/)
  assert.match(previewActiveNotice({ workflow_type: 'agent', workflow_version: 'stage-agent-v9' } as Run), /stage-agent-v9/)
  assert.match(previewActiveNotice({ workflow_type: 'agent', workflow_version: 'stage-agent-v8' } as Run), /stage-agent-v8/)
  assert.equal(previewBadge('stage-agent-v6'), 'V6 预览')
  assert.equal(previewBadge(undefined), '预览版本未知')
  assert.match(previewNotice('stage-agent-interactive-v1', 'stage-agent-v7', 'agent', false, 'staged', '完整协作'), /无项目 Agent 将使用交互候选/)
  assert.match(previewNotice('stage-agent-v2', 'stage-agent-v6', 'agent', false, 'staged', '完整协作'), /原阶段流程/)
  assert.match(previewNotice(undefined, undefined, 'agent', true, 'staged', '完整协作'), /版本未知/)
  assert.match(previewNotice('stage-agent-interactive-v1', 'stage-agent-v7', 'agent', true, 'single', '单 Agent'), /单 Agent/)
  assert.match(previewNotice('stage-agent-interactive-v1', 'stage-agent-v7', 'chat', true, 'staged', '完整协作'), /普通聊天/)
})

test('active notice uses frozen run mode, workspace, version and strategy rather than draft choices', () => {
  const run = { workflow_type: 'agent', workflow_version: 'stage-agent-v7', workspace_id: 'w', agent_strategy: 'compact' } as Run
  assert.match(previewActiveNotice(run), /当前任务：工作区 Agent · 交互候选（stage-agent-v7） · 精简协作/)
  assert.equal(displayedWorkflowType(true, run, 'chat'), 'agent')
  assert.match(previewActiveNotice({ ...run, workflow_version: 'stage-agent-v6' }), /V6（stage-agent-v6）/)
  assert.match(previewActiveNotice({ ...run, workspace_id: null, workflow_version: 'stage-agent-interactive-v1' }), /无项目 Agent/)
  assert.match(previewActiveNotice({ ...run, agent_strategy: 'single' }), /单 Agent/)
  assert.match(previewActiveNotice({ ...run, workflow_type: 'chat' }), /当前任务：普通聊天/)
})

test('missing active run fields stay unknown and terminal switch returns to draft selection', () => {
  const missing = {} as Run
  assert.match(previewActiveNotice(missing), /当前任务模式未记录/)
  assert.equal(displayedWorkflowType(true, missing, 'chat'), 'unknown')
  assert.match(previewActiveNotice({ workflow_type: 'agent' } as Run), /工作区信息未记录.*工作流版本未记录.*协作模式未记录/)
  assert.equal(displayedWorkflowType(false, { workflow_type: 'agent' } as Run, 'chat'), 'chat')
  assert.match(previewNotice('stage-agent-interactive-v1', 'stage-agent-v7', 'chat', true, 'staged', '完整五阶段'), /下次任务将使用普通聊天/)
})
