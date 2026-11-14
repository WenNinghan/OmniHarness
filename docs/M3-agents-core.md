# M3 Deep Agents 受控执行核心（独立组件）

本组件在 `src/omniharness/m3_agents.py`，使用锁定版本的 Deep Agents 构建主 Agent 与显式子 Agent。**尚未接入产品 API、Worker、持久任务控制或工具沙箱，不构成 M3 已交付。**

`build_controlled_agent(coordinator=..., subagents=..., hooks=..., checkpointer=...)` 接收显式 `AgentRole`（名称、描述、模型、指令、可信工具列表）和每 Run 共享 `BoundaryHooks`。禁止 SDK 自动选择模型；只能调用角色显式提供的 `BaseTool`，委派只能指向显式注册的子 Agent，默认文件和 shell 工具不因框架自带而自动授权。

主 Agent 和每个子 Agent 都有独立 middleware 实例，但共享宿主 hooks 和单个 `asyncio.Semaphore(2)`。模型前调用 `check_control('model_calls', role, None)`，然后调用 `reserve('model_calls', role, None)`；工具前对应 `tool_calls`。委派前检查 `delegation`，获取信号量后再次检查并预留工具预算，避免等待期间已请求暂停却继续启动子任务。子 Agent 不能再次调用 `task`；这比计划允许的最大委派深度更保守，**不表示深度 2 的委派已实现**。信号量只保证同一组件实例的并发数，宿主必须确保一个 Run 不被两个执行实例并发驱动。

hooks 只收到调用类型、角色及已授权工具名，不收到模型对象、消息、原始调用参数或 Provider 响应。未知工具名称不传给 hooks。hooks 可抛异常中断继续派发；是否持久化暂停、当前在途请求如何结束、图与业务状态一致性均由宿主处理，不能将普通异常当成已保存的暂停检查点。

Deep Agents SDK 的默认摘要中间件可能直接调用模型、绕过 `awrap_model_call`；本组件在主 Agent 和子 Agent **显式替换默认摘要为无操作中间件**。因此当前不具备自动上下文压缩，宿主在接入前必须限制上下文长度；未来摘要能力也必须接入同一权限、控制及原子预算。

每次主、子和孙 Agent 模型请求在检查控制命令后、预留预算和发出调用前，按 `Settings.max_chat_messages` 与 `max_chat_characters` 检查实际消息及 system 消息；包括结构化内容与 JSON 序列化开销。超限时不执行该次模型调用；并发操作结果不明则保留 `pending_review/CONTEXT_LIMIT`，否则以 `failed/CONTEXT_LIMIT` 结束。这是保守字符与消息数上限，**不是 token 计数或自动压缩**。真实 PostgreSQL 与离线测试记录见 `.ai-governance/pre-implementation-M3-context-limits.md`。

## 持久恢复与安全边界

- `reserve` 的原子性和执行权 fencing 完全由宿主提供；示例测试的内存锁只是离线验证用，不能当多进程保障。
- LangGraph 中断恢复可能重新进入外层 `task` 工具，即使其 tool_call_id 不变。组件新增显式 `require_durable_ids=True` 严格模式，将稳定身份与输入哈希传入 `reserve_invocation`；独立 SQLite 两进程测试已覆盖一次重入。旧三参数 `reserve` 接口仅兼容非严格离线模式，不适合持久恢复。幂等预算不等于外部操作可重放；在宿主完成执行权 fencing、操作结果对账及故障测试之前，仍不得接入生产持久恢复。
- 用户消息、工具参数及模型输出可能被 Deep Agents / LangGraph 检查点保存。本组件不记录这些内容，并不代表检查点不含用户数据。调用方必须启用本地数据保护与内容审计。
- 测试检查了一个仅存在模型对象中的合成 Key 不出现在 SQLite 检查点文件和 hooks 元数据；这不代表全部 Provider 和异常路径已经完成完整隐私审计。调用方必须明确关闭云端追踪，不能将密钥写进图状态、消息或工具参数。
- 本组件不执行权限沙箱；明确传入的 Python 工具属于调用方信任代码。沙箱、工作区、MCP 与外部动作审批仍属 M4。

## 本轮验证

2026-11-08，在已安装 Deep Agents / LangGraph 依赖下：

```text
python -m pytest tests/test_m3_agents.py -q --basetemp <独立测试目录>
8 passed
python -m ruff check src/omniharness/m3_agents.py tests/test_m3_agents.py
All checks passed!
```

测试使用确定性本地 `BaseChatModel` 驱动真实 Deep Agents 循环，验证三次委派最多两次并发、主子 Agent 边界共享 hooks、模型/工具预算拒绝、子 Agent 内暂停检查、排队后重新检查暂停、未授权内置工具与子 Agent 再委派拒绝、默认摘要中间件被替换、合成模型 Key 不进入测试检查点和 hooks。没有发起真实 Provider 请求；未测试产品 Worker、真实用户暂停恢复、数据库 fencing、实时 Agent 事件、对模型任务质量或生产安全的结论。

调用身份与幂等预算增量验收见 [M3 幂等预算](M3-budget-reservations.md)。
