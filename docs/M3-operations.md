# M3 / G2 检查点恢复切片 A：副作用幂等登记

状态：2026-11-08 已实现并通过回归。实现前设计见 `.ai-governance/pre-implementation-M3-checkpoint.md`，实际验收输出见 `.ai-governance/progress.md`。

**这不是「检查点恢复已交付」**。检查点恢复拆成 A / B / C 三片，本文只覆盖 A：为将来重入的图节点准备一套不会被重复执行的副作用登记机制。LangGraph 图执行层、`AsyncPostgresSaver` 接入、Worker 恢复路径与 `resume` 解锁（B / C）都尚未实现，需各自另写实现前设计。

## 为什么先做这个

`pre-implementation-M3.md` 第 5 条要求：LangGraph `interrupt` 会让节点重进，副作用必须有稳定 operation ID、参数绑定与结果状态；**结果未知不得自动重复，转待核对**。没有这套登记就接图执行层，节点重入会重复调用模型、重复产生外部副作用。

## 表与迁移

迁移 `0003_operation_records.py` 新增 `operation_records`：`operation_id`（String(64) 主键）、`run_id`（FK runs.id）、`node_id`、`parameters_hash`、`status`、`attempts`、`result`、`error_code`、`workflow_version`、`created_at` / `started_at` / `finished_at`。

`operation_id` = `sha256` of 规范化 JSON `{run_id, node_id, parameters_hash}`；`parameters_hash` = 参数规范化 JSON 的 SHA-256 前 32 位。同一节点、同参数重入 → 同一 ID；参数变化 → 不同 ID。

## 状态机

```
pending → running → completed   (带 result)
                 → failed       (带 error_code，明确失败)
                 → uncertain    (结果未知，永不自动重放)
```

| 函数 | 语义 |
| --- | --- |
| `begin_operation(db, run_id, node_id, parameters, workflow_version)` | 返回 `(row, executable)`；`INSERT ... ON CONFLICT DO NOTHING` 的 `rowcount == 1` 才发放执行权。任何已存在记录（含 failed）都返回 `executable=False` |
| `finish_operation(db, op_id, status, result, error_code)` | 条件 `UPDATE ... WHERE status='running'`，只生效一次 |
| `mark_uncertain(db, op_id, reason)` | 结果未知转 `uncertain`，同样只生效一次 |
| `may_retry(operation)` | 仅 `failed` 为 True；`uncertain` 永不 |
| `operation_output(row)` | 只读投影，不含凭据 |

## 边界

- **不写 `run_events`**：这是内部执行证据，不是用户可见事件，避免破坏既有 SSE 游标与事件序列断言。
- **不自动重放**：`uncertain` 没有自动处置入口，需人工核对后显式处理；这是刻意的保守设计。
- **不改执行路径**：`Worker.process` / `recover` / `claim` 一行未动；`Run` 状态集合未加 `pending_review`（留给 C 片）。
- **不新增 API 端点**、不改前端、不改评分逻辑或评测数据。
- PG 跨连接池并发幂等本轮未单独验证（同类 `ON CONFLICT DO NOTHING` 写法已由预算切片在 PG 验证），留 B 片补充。

产品行为仍是 M0–M2 聊天，`/health` 与 `/bootstrap` 的 `stage` 仍为 `M0-M2`。
