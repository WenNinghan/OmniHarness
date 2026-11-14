# M3 / G2 前端控制界面（开发预览）

状态：2026-11-08 已实现并通过构建与回归。设计依据见 `.ai-governance/pre-implementation-M3-controls.md`，实际验证输出见 `.ai-governance/progress.md`。

本切片**只消费已落地的后端接口**，没有新增端点、改 schema、改事件序列或改评分逻辑。`/health` 与 `/bootstrap` 的 `stage` 仍为 `M0-M2`，产品行为仍是单流聊天。

## 界面能力

「运行详情」面板新增三个区块：

1. **运行控制**：暂停 / 继续按钮 + 补充要求输入框。按钮可用性由 `lib/control-state.ts` 的 `availableCommands(status)` 决定——`queued` / `running` 可暂停，`paused` / `pause_requested` 可继续，四种非终态都可补充要求，终态全部禁用。
2. **预算（只读）**：展示 `model_calls` / `tool_calls` / `revisions` / `active_seconds` 的「已用 / 上限」，来自 `GET /runs/{id}/budget`。`initialized=false` 时说明尚未开始计数。不提供写入入口，不展示费用（费用未知保持未知）。当前只有模型调用接入了真实计数，界面明确说明其余三项尚无业务调用点。
3. **控制命令**：`GET /runs/{id}/commands` 的序号、类型与结果。后端列表返回 `status` 但不含 `reason`，拒绝原因从 SSE 的 `run.command` 事件 payload 按 `command_id` 关联取得。`CHECKPOINT_REQUIRED` 显示「已拒绝 · 已执行过的任务需要检查点恢复，当前不会重放」，不隐藏失败。

## 交互语义

- 命令提交后后端状态仍是 `pending`，界面提示「等待 Worker 执行」，**不立即把 Run 标成已暂停**。SSE 收到 `run.command` / `run.paused` 时重新拉取 Run、命令列表与预算。
- `paused` 是非终态：SSE 保持连接，但「停止生成」按钮在 `paused` / `pause_requested` 时禁用（`cancel` 对这两态无效），助手消息不再显示流式光标，输入框占位文案改为「任务已暂停」。
- 命令 ID 使用 `crypto.randomUUID()`；只有 `append_requirement` 带 `content`。

## 明确未实现

LangGraph 图执行与检查点恢复、真实子 Agent 并发、工具/修订/活跃时间的真实计数接入、预算写入接口、工作流视图、任何策略决策（G4 之前禁止）。浏览器端实操若未执行，不记为功能验收。
