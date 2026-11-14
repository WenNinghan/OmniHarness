# M3 / G2 预源原子计数（预算切片）

实现前设计：.ai-governance/pre-implementation-M3-budget.md。实际验收输出见 .ai-governance/progress.md。

## 范围

复用迁移 `0002` 已有的 `run_budgets` 表，**不改 schema**。每个 Run 首次计数时懒创建一行并把当时的上限冻结进去；所有计数走数据库的单条条件 `UPDATE`，主 Agent、未来的子 Agent 与其它连接池共享同一行，不会因进程内变量各自计数而超预算。

当前执行层仍是 M0–M2 单一模型流，因此**只有 `model_calls` 接入了 `Worker.process`**：在 provider 停用检查、上下文上限检查之后，`stream_fn` 发起模型流之前预留一次。`tool_calls` / `revisions` 具备同样的原子预留能力，但没有真实业务调用点；`active_seconds` 保持 0，待图执行层实现。本节不代表子 Agent 或图执行已交付。

## 服务层（`services.py`）

| 函数 | 作用 |
| --- | --- |
| `BudgetLimits` | 冻结上限的数据载体（默认 50 / 100 / 2 / 1800） |
| `budget_limits(settings)` | 从 `Settings` 读 4 个静态可配置上限 |
| `ensure_budget(db, run_id, limits)` | `INSERT ... ON CONFLICT DO NOTHING` 幂等建行；已有行不覆盖 |
| `reserve_budget(db, run_id, kind, limits)` | 原子预留一个配额；`kind` 白名单为 `model_calls` / `tool_calls` / `revisions`，返回 `bool` |
| `budget_output(db, run_id, limits)` | 只读快照，**读取不创建、不修改**预算行 |

原子点是这一条语句（计数器名来自白名单常量）：

```sql
UPDATE run_budgets
SET model_calls = model_calls + 1, updated_at = :now
WHERE run_id = :run_id AND model_calls < max_model_calls
```

`rowcount == 1` 才算拿到配额。数据库在写锁内重新判定条件，因此并发调用方不会一起读到同一余量。

## 配置（`settings.py`）

| 字段 | 默认 | 约束 |
| --- | --- | --- |
| `OMNI_BUDGET_MAX_MODEL_CALLS` | 50 | 1–10000 |
| `OMNI_BUDGET_MAX_TOOL_CALLS` | 100 | 1–100000 |
| `OMNI_BUDGET_MAX_REVISIONS` | 2 | 0–1000 |
| `OMNI_BUDGET_MAX_ACTIVE_SECONDS` | 1800 | 1–86400 |

上限在建行时冻结，改配置不影响在途 Run。这是静态可配置参数，不含策略学习结果，也不会由运行经验自动写回默认值。

## 只读接口

`GET /api/v1/runs/{identity}/budget`，需会话 Cookie（与现有 API 同一鉴权与本地 Origin 边界）。Run 不存在返回 404。

```json
{
  "run_id": "...",
  "initialized": true,
  "max_model_calls": 50,
  "model_calls": 1,
  "max_tool_calls": 100,
  "tool_calls": 0,
  "max_revisions": 2,
  "revisions": 0,
  "max_active_seconds": 1800,
  "active_seconds": 0,
  "budget_remaining": {
    "model_calls": 49,
    "tool_calls": 100,
    "revisions": 2,
    "active_seconds": 1800
  },
  "updated_at": "..."
}
```

预算行尚未创建时 `initialized=false`、计数为 0、上限取当前配置、`updated_at=null`。`budget_remaining` 对应 `evaluation-protocol.md` 预留字段，只做记录与展示，不参与任何调度或策略选择。接口不含凭据、Provider 用量或费用；费用未知仍为 `null`。

没有写入或调整预算的接口。

## Worker 行为

- 正常完成：`model_calls` +1，事件序列、消息内容、SSE 游标、状态机不变（预算不写 `run_events`）。
- 预留失败（超限）：本次模型流**尚未发起**，直接以 `failed` + `BUDGET_EXCEEDED` 终态结束，不重试、不重放、不缓冲文本。
- provider 停用、上下文超限：在预留之前失败，不产生计数，也不创建预算行。
- 排队阶段暂停、安全点暂停、`CHECKPOINT_REQUIRED` 拒绝重放：都不额外计数。

## 明确未实现

LangGraph 图执行、检查点恢复、真实子 Agent 并发派发、委派深度与并发上限 2、工具/修订/活跃时间的真实计数接入、前端控制界面、MCP/沙箱、任何策略决策（G4 之前禁止）。`strategy_id` 仍固定 `manual`。

一次 Run 目前只有一次模型流，50 次上限在 M2 单流下不会触及；超限行为通过把上限临时调小来验证，不代表已达到真实多节点预算压力。Docker / Compose / 容器挂载链路仍未实测。
