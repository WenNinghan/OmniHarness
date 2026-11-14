# Deep Agents 暂停恢复：离线探针与接线风险

日期：2026-11-08。本记录基于本项目锁定安装的 Deep Agents 0.7.32、LangGraph 1.2.20 和 LangChain middleware。探针使用确定性本地模型、模拟工具与本地 SQLite 或内存检查点，没有发起真实模型请求；结果只说明这些特定版本与测试交错。

## 真实子 Agent 中断与重入

运行 `.venv/Scripts/python.exe .omniharness/probe_agent_interrupt.py`。探针在子 Agent 的 `awrap_model_call` 调用边界触发 `interrupt()`，之后以 `Command(resume='continue')` 继续：

1. 首次中断前，主 Agent 已调用一次模型并发起一次 `task` 委派；子 Agent 的工具尚未运行。
2. 恢复时，主 Agent 的 `task` 工具 wrapper 再次执行，同一个委派再次进行预算预留；随后子 Agent 的模拟工具执行一次。当前 `SharedBoundaryMiddleware` 的计数器因此把一次逻辑委派重复计入预算。
3. 工具执行后的下一个子 Agent 模型边界再次触发中断，因为探针刻意对该角色的每个模型边界都调用 `interrupt()`；这是新的中断点，不能假定一次恢复就使整个任务完成。

用同一版本真实库做两个并发子 Agent 的附加内存探针：child0 在模型调用前中断，child1 工具在首次调用时完成一次；恢复后 child1 工具没有在该次实验中重跑，但 child0 对应父级 `task` wrapper 仍然重入。这个观测不能推广为任意并行、重启和故障场景都不会重复副作用。

## 跨进程身份与检查点内容

运行下列命令分别创建和恢复一个新的测试 SQLite 检查点（脚本使用固定测试 thread，重复运行前须选择新的隔离测试文件/线程，不能覆盖产品数据）：

```powershell
.\.venv\Scripts\python.exe .omniharness/probe_agent_persistent_ids.py start
.\.venv\Scripts\python.exe .omniharness/probe_agent_persistent_ids.py resume
```

两次命令由不同 Python 进程运行。结果：父 `task` 的 `request.tool_call.id`、父工具节点 `request.runtime.execution_info.task_id` 与 `checkpoint_ns` 在恢复后相同；子 Agent 被中断的模型节点 task ID 与 namespace 也相同。子 Agent 执行工具后进入的下一个模型节点 task ID 不同。

因此可研究以 `(run_id, checkpoint_ns, task_id, kind, tool_call_id)` 或等价的稳定逻辑调用身份作为数据库幂等键，而不是只用角色名/工具名；缺失或重复的模型生成 tool-call ID 需要明确处理。跨并行、子图版本升级、节点重试和 PostgreSQL 并发的稳定性仍须另测。对于预算，单次 SQL 原子加一仍不够，还须确保同一逻辑调用恢复时不重复扣减；外部 SDK 自行重试是否也计入模型请求上限需要单独明确和验证。

在上述 SQLite 检查点关闭后，对数据库文件的原始字节进行 canary 搜索：模拟 model 对象中的 API Key canary 未出现，用户消息 canary 出现。检查点可能包含用户消息与工具内容；该结果不能证明所有 Provider、运行配置、子图或 PostgreSQL 的密钥均安全。不得把凭据放入 state、messages、工具参数、interrupt value 或可持久化 configurable；接入真实适配器前还须使用无效模拟密钥对 PostgreSQL 存储、事件和日志做 canary 检查。

## 内置工具审批中断与恢复边界

本地 `langchain.agents.middleware.HumanInTheLoopMiddleware` 支持 `interrupt_mode='batched'` 与 `'per_call'`。后者在 `awrap_tool_call` 中基于工具调用发起可恢复中断；未列入 `interrupt_on` 的工具默认自动放行，因此它不能单独代替 OmniHarness 的统一权限网关。模型调用边界不由该工具审批组件覆盖。

LangGraph `interrupt()` 恢复后会从**节点开头**重跑，同一节点内多个中断按该节点调用顺序匹配恢复值；在中断前执行不幂等动作或因外部条件改变中断顺序，都需特别处理。`GraphInterrupt` 是 `Exception` 子类，通用 `except Exception` 或 `except BaseException` 若吞掉它会破坏暂停语义；不能把整个可中断 Deep Agent 调用直接塞入当前 `OperationLedger.execute_once` 的宽泛异常处理。

本地 LangGraph 的 `ainvoke` 默认 durability 为 `async`；`durability='sync'` 会在下一步骤开始前同步持久化，但**不使业务数据库事件、命令消费与 LangGraph 检查点成为同一事务**。补充要求需要持久 command ID、checkpoint 内已纳入 ID 和重入去重/对账；不能先将命令标记已消费，再假设 checkpoint 必定保存成功。对每个关键交错必须用真实 PostgreSQL 和 Worker 进程退出故障注入验证。

当前图组件和 Deep Agents 组件均尚未接入产品 Worker。以上是接线前的研究结果，并非暂停恢复、多 Agent 幂等或完整 M3 已交付的证明。
