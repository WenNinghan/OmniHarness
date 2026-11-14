# M3 PostgreSQL Checkpointer 执行权保护组件

状态：独立组件已实现并在专用 PostgreSQL 测试库验证；**未接入 Worker、API、Deep Agents 或产品启动流程**。Agent API 目前仍不可用；本组件不等于 M3 的暂停、恢复、用户命令或外部动作幂等已经交付。

## 能力和调用边界

`src/omniharness/checkpoint_fencing.py` 对官方 `AsyncPostgresSaver` 提供独立版本化 schema、执行权登记和单 Run saver。需要由未来的受信任宿主显式调用：

1. 停止相关运行并备份后，在迁移阶段调用 `initialize_checkpoint_schema`。它在 schema 级 advisory lock 下初始化官方 saver 表与 fence 表、三个 trigger 和组件版本；现有无组件所有权标记的 schema 会被拒绝，不覆盖用户表。完整就绪后的版本、函数内容或 trigger 缺失/更改会 fail closed，不静默修复。
2. Worker 获取并持续持有 `WorkerLock` 后，用**持锁连接本身**调用 `register_fence`，为 Run 注册或增加 epoch，绑定 `thread_id`、workflow 版本、持锁 backend 的 PID、backend_start 和 advisory lock key。已有工作流版本不一致时拒绝自动接管。
3. `open_fenced_saver` 显式建立单独 psycopg 异步连接，重新验证初始化版本、官方 saver 版本、函数与 trigger，并验证 ticket、持锁 backend 与数据库实际锁状态。连接绑定单一 thread、epoch 和 schema；`aget_tuple`、`alist`、delta 读取、写入均校验 Run，删除/复制/清理及在运行时 `setup` 被禁用。
4. 在三个 saver 数据表的每行写事务中，trigger 对 fence 行加 `FOR SHARE`，同时验证 session 固定身份、epoch、schema、持锁 backend 的 PID、backend_start 和 PostgreSQL advisory lock。新 owner 使用行锁更新 epoch，会等待在途旧写事务完成；旧 session 不能在新 epoch 后写入。thread ID 更新被禁止。
5. context 退出时关闭 saver 的物理连接，不使用连接池自动重连。Windows 异步 psycopg 使用前由宿主配置 Selector 事件循环；连接错误应让宿主停止执行，不能静默换连接继续。

本组件自管理的 schema 版本是 `2`，只接受当前锁定的 `langgraph-checkpoint-postgres==3.2.0`。三个 trigger 均使用 `ENABLE ALWAYS`，会话处于 `session_replication_role=replica` 时仍执行。启动校验还要求 trigger 不带 `WHEN` 条件；条件表达式可能使保护不生效。官方表的 `checkpoint_migrations` 必须完整匹配当前依赖的版本列表；升级库前必须重新检查所有写表、trigger、迁移与测试，不能自动按新版 setup 覆盖现有 ready schema。现有应用 Alembic/SQLite schema 未改变。

### 已有版本 1 的明确升级路径

旧版 schema 的普通 trigger 在复制模式下可能不执行，因此版本 2 的普通初始化和 saver 打开会拒绝版本 1，**不自动更改**。停止 Worker 和初始化任务，按备份流程备份数据库后，受信任的维护流程可显式调用 `await upgrade_legacy_checkpoint_schema(database_url, schema=...)`。该函数在一个事务中尝试获取 Worker 与初始化 advisory 锁、严格核对版本 1 的官方迁移/函数/无条件 trigger 定义、将三个 trigger 改为 `ENABLE ALWAYS` 并更新版本到 2；无法获得锁、发现任何不兼容或数据库错误时拒绝或回滚。它保留 checkpoint、fence 和用户数据，不删除或重建 schema。升级后须用版本 2 初始化入口重新验证。不能用该入口处理未知 schema、未完成的 v1 初始化或其他版本，应先排查而不是覆盖。

## 实际验证

`tests/test_checkpoint_fencing.py` 在单独的 `omniharness_test` 数据库、随机 schema 中执行，**9 passed**：

- 两个并发初始化调用（独立异步连接）通过非阻塞 advisory try-lock 串行安装；ready schema 幂等，未知既有 schema 不被覆盖，版本或 trigger 被改动后 fail closed。
- 在实际官方 saver 上，Worker 持锁 backend 被终止后旧 saver 失效；新 owner 注册新 epoch 后恢复根图和两个并行子图。
- 不同 thread、不同 checkpoint schema 的越权写入被拒绝；无法修改记录的 thread ID；Run saver 不能删除、清理或复制。
- 在途 250 条 checkpoint 写入与新 owner 的 epoch 变更依数据库行锁排序；取消阻塞中的批量写不留下部分结果。
- 强制终止测试专属 saver backend 后，未提交的 100 条写入回滚，旧 saver 不会自动重连；数据库连接错误使用固定文本，不暴露模拟口令。

未设置 `OMNI_TEST_POSTGRES_URL` 时，PostgreSQL 项目测试会明确跳过；仅 schema 名校验是离线测试。实验与日志均不能替代产品多 Worker、Docker PG 或真实 Provider 的故障注入验收。

实现前的独立探针和更完整测试边界见 checkpoint fencing 设计。

## 必须保留的限制

- 同一个 PG backend 若自行释放 advisory lock 又重新获得同一把锁，PID、backend_start 和旧 epoch 会保持一致；必须由 Worker 生命周期**禁止这种复用**：失锁立即永久标记并关闭 owner session，新连接接管时先递增 epoch，再打开 saver。
- 持锁校验与 PG 写保护不会撤回已经发到模型服务的请求；模型或工具调用需要独立的 fenced 操作登记。执行结果未知时进入待核对，不能自动重放或声称 exactly-once。
- 本组件不接入或验证 `m3_graph.OperationLedger` 的独立数据库会话，也不提供用户命令与检查点的原子提交。宿主在接入前必须解决命令 ID、状态对账、预算调用身份和业务写 fencing。
- 检查点本身可能包含用户消息与工具结果。凭据不能进入 state/messages/configurable/interrupt，须在真实 PG、真实适配器序列化、子图和日志中使用模拟密钥做 canary 测试。
- Python 插件属于受信任进程内代码。数据库角色若能任意更改 fence、session 参数或 trigger，该组件不构成针对恶意插件的安全边界；部署角色最小权限需在接线时单独设计和验证。
