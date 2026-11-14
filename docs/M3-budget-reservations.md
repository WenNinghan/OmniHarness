# M3 调用身份与幂等预算（独立组件）

本切片新增 `0005_budget_reservations` 迁移、`RunBudgetReservation` 元数据、`m3_budget.reserve_invocation` 和 `m3_agents` 严格模式。**未接产品 Worker/API，不代表 Agent 已具备可安全恢复的完整执行路径。**

## 行为

- 使用主/子节点 `runtime.execution_info` 的 checkpoint namespace、task ID 和工具调用 ID 构造哈希身份；模型消息或工具参数只产生绑定指纹，原文不存预算登记表。严格模式需要检查点、`reserve_invocation` hooks、thread ID、task ID 和必要的 tool call ID，缺失直接拒绝。
- 旧三参数 hooks 继续可用于默认的非严格离线模式。产品持久恢复必须显式启用 `require_durable_ids=True` 并使用经验证的宿主实现；否则不能声称预算幂等。
- 宿主须在 Run 创建时先冻结 `run_budgets` 行，并在 owner-bound、有效的事务内调用 `reserve_invocation(db, ...)`。新身份消耗预算，同身份同类型/绑定返回 `ALREADY_RESERVED`，同身份不同绑定抛 `ReservationConflict`；超限不会登记调用，缺预算行或事务会失败。
- 模型输入和工具参数的 SHA-256 指纹只用于绑定，不是加密。相同输入可能被关联，低熵内容理论上可能遭受猜测；不要把指纹描述成加密存储。
- `ALREADY_RESERVED` **只表示计数已经存在，不提供模型/工具执行或重放许可**。执行权、操作结果、已完成复用、未知结果待核对和图/业务对账由宿主的 fencing、ledger、checkpoint 共同决定。

## 接线前待完成

1. 宿主必须核对 `InvocationIdentity.thread_id` 与当前 Run ID，防止跨 Run 使用身份。
2. `binding_fingerprint` 目前绑定角色、工具名、工具参数或消息/系统提示；它未绑定完整模型提供商配置、工具 Schema、工作流/插件版本。宿主须将冻结配置与版本纳入自己的绑定检查，不能默认为全部输入已冻结。
3. `m3_budget` 不验证传入 session 确属有效 Worker 锁连接；当前通过传入外部事务支持 owner-bound session，但由宿主证明 fencing，不能在普通连接上直接声称具备失锁防护。
4. SQLite 两进程测试只覆盖一次确定性子 Agent interrupt/resume；并非对所有 LangGraph 重试、第三方 middleware、真实 Provider 或在途网络故障的稳定身份证明。
5. 子 Agent 再委派、持久补充要求、自动摘要和完整多 Agent 运行仍未实现；GraphInterrupt 不能被宽泛异常处理吞掉。

## 验证记录

2026-11-08 定向测试 `tests/test_m3_agents.py tests/test_m3_budget_reservation.py` 在真实 PostgreSQL 专用测试库和 SQLite 临时库执行：**14 passed**。涵盖 PG 双连接池 8 并发对同一身份仅首次扣费、不同身份独立计数、冲突、上限、事务回滚；SQLite 和 PG 对 Alembic `compare_metadata` 的新表无差异；Deep Agents 两个独立 Python 进程中断/恢复时外层 `task` 重入只计一次，模型 4 次、工具 2 次；严格身份缺失 fail closed。

Ruff 对新增模块、迁移、元数据和测试通过。以上是组件与数据库验证；还没有真实付费模型调用、Worker 接线或生产用户故障注入验证。
