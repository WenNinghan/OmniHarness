# M3 命令接口（开发预览）

状态：2026-11-08 已实现并通过回归。设计依据见 `.ai-governance/pre-implementation-M3-commands.md`，验证结果见 `.ai-governance/progress.md`。

本接口提供**控制意图与聊天状态机**，并有预算原子计数和前端控制界面；图执行与检查点组件仍未接入产品 Worker，产品行为仍是 M0–M2 聊天，`/health` 与 `/bootstrap` 的 `stage` 仍为 `M0-M2`。

## 接口

`POST /api/v1/runs/{identity}/commands` — 需本机配对会话与 `X-Omni-Request: 1`。

```json
{"command_type": "pause", "client_request_id": "cmd-1"}
```

`command_type`：`pause` / `resume` / `append_requirement`。`append_requirement` 必须带非空 `content`（≤ 50000 字符），其余命令不得带。

响应 202：

```json
{"run_id": "...", "command_id": "...", "sequence_number": 1,
 "command_type": "pause", "status": "pending", "run_status": "queued"}
```

`GET /api/v1/runs/{identity}/commands` 返回该 Run 的命令列表（按序号升序）。

同 Run 内 `client_request_id` 幂等：内容一致返回原命令，内容不同返回 `409 REQUEST_ID_CONFLICT`。序号在事务内锁 Run 行后按 `max+1` 生成。

Run 进入终态时，同一事务按序将仍待处理的命令标为 `rejected` 并先写入 `run.command`，再写入最终状态事件；终态之后的新命令返回 `409 RUN_TERMINAL`，先前命令的相同 ID 重试仍返回原结果。旧版本遗留的终态 Run 如存在待处理命令，Worker 会将其标为 rejected，但不在既有终态事件之后补写事件。

## 语义

命令提交**只记录意图**，状态不变；Worker 在现有循环内按序号消费，状态变更与命令标记在同一事务完成。每消费一条写一条 `run.command` 事件，payload 含 `command_id`、`sequence_number`、`command_type`、`result`、`reason`、`run_status`；`append_requirement` 另含 `content`，并保存在 `run_commands.payload`，不新增 user Message。

| 命令 | 当前状态 | 结果 |
| --- | --- | --- |
| pause | queued | applied → paused |
| pause | running | applied → pause_requested，执行循环在安全点落 paused |
| resume | paused 且未开始执行 | applied → queued |
| resume | paused 且已开始执行 | rejected `CHECKPOINT_REQUIRED` |
| resume | pause_requested | applied → running |
| append_requirement | queued / running / pause_requested / paused | applied，只记事件 |
| 任意 | 不适用的非终态 | rejected `INAPPLICABLE_STATE` |
| 任意新命令 | 终态 | HTTP 409 `RUN_TERMINAL`；既有命令的幂等重试仍返回原结果 |

**恢复边界**：没有 LangGraph 检查点之前，已发出模型请求的 Run 不会重新派发。resume 明确返回 `rejected` + `CHECKPOINT_REQUIRED`，保留已保存文本，不伪装成功、不自动重放。

`claim()` 增加同会话互斥：会话内存在 `running` / `pause_requested` / `paused` / `cancel_requested` 的 Run 时，该会话后续 `queued` 任务不越过；其他会话照常运行。Worker 重启时 `pause_requested` 转为 `paused` 并保留文本，不标 `interrupted`。

## 未实现

命令消费依赖 Worker 运行；Worker 未启动时，未终结任务的命令保持 `pending`。预算目前仅模型调用接入真实计数。图执行、实际检查点恢复、MCP/沙箱及策略决策均尚未接入产品执行路径。
