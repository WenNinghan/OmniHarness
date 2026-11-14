# M3 图执行基础组件（尚未接入产品）

该组件是一个内部、单操作的 LangGraph 图构件，不是完整 Agent 工作流。其代码在 `src/omniharness/m3_graph.py`；现有 Worker/API 未接线，不改变普通聊天行为。

## 宿主契约

- `build_graph(checkpointer, ledger, execute, should_pause)` 构建检查点图；`start(graph, context)` 和 `resume(graph, run_id)` 均以 Run ID 作为 LangGraph `thread_id`，并使用 `durability="sync"`，使前一步 checkpoint 在下一节点执行前完成持久化。
- `OperationContext` 冻结 run、node、invocation、workflow version 与输入 SHA-256。checkpoint 只包含这些标识/指纹、暂停标记和宿主返回的非敏感结果引用；不接受模型 Key、用户输入正文或模型输出正文作为图状态。回调按 ID 从宿主受控存储读取输入。
- `should_pause(context)` 返回布尔值；安全边界决定暂停后，LangGraph interrupt 可由 `resume` 以内部继续信号恢复。
- `execute(context)` 是受信任的宿主异步回调，返回不含凭据的短引用。它不代表开放任意工具；必须由后续工具/权限网关提供具体操作。
- `postgres_checkpointer(url, initialize=True)` 显式初始化官方 saver；正式生产流程须先备份并决定表初始化时机。测试可注入 SQLite saver。Windows 必须在启动事件循环前设置 Selector 策略。

## 副作用与已知关键限制

`OperationLedger` 复用现有 `OperationRecord` 和登记服务。`(run_id, node_id, invocation_id)` 是逻辑操作身份；第一次操作须先提交 running，再调用执行回调。相同身份及输入可复用已完成结果，输入/版本变化报错；新逻辑操作需要新 invocation ID。留下 running 或执行异常时标记 uncertain，不自动重放。

**当前还没有所有权 fencing：`OperationLedger` 自己打开的数据库会话不会校验 Worker 的执行权。** 当 Worker 的 PostgreSQL advisory lock 连接中断、新 Worker 取得锁、旧操作仍在途时，单靠检查最初持锁不足以阻止旧执行者继续调用或写入。必须先实现并验证贯穿领取、操作登记、执行前后及写入的宿主 fencing / 锁丢失处理，再接入生产外部副作用。现有组件只可供离线隔离测试，不能宣称已安全执行外部操作。

官方 checkpoint 与业务操作记录也不在同一事务。若外部动作成功而完成记录未能保存，结果为 uncertain，不能自动重放；若完成记录已提交而 checkpoint 尚未更新，可重入读取完成记录并复用结果。

## 实测与边界（2026-11-08）

- `pytest -q tests/test_m3_graph.py`（配置专用 PG URL）→ **6 passed in 13.04s**。SQLite 覆盖 checkpoint 关闭/重连、操作身份与输入绑定、异常及遗留 running 不自动重放，另在操作回调内读取 checkpoint 验证前一节点状态已持久化。
- 使用专用 `omniharness_test` 和随机 schema 的真实 Windows PostgreSQL 测试用三个独立 Python 进程依次保存 interrupt、提交一个测试副作用、从 interrupt 恢复；数据库证据中副作用计数为 1。该测试模拟业务完成记录已提交但图尚未推进，并非进程在任意机器指令处被强杀的完整故障注入。
- 测试中调用的是确定性本地回调，不是实际 Provider、Deep Agents 多 Agent 或 MCP。未验证锁断连期间的执行权转移、端到端 Worker 暂停恢复、活跃预算/子 Agent 或真实外部副作用。
- PG 测试仅接受专用数据库且保留随机 schema 供检查，不删除用户数据；无测试数据库时显式 skipped。生产使用依然需 PG / Docker 环境下重测。
