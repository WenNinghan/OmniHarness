# 功能类模块审查：多模型角色路由与参谋层

日期：2026-11-08。状态：审查结论 + 本轮落地范围。上位文件 `goal.md`，前端规范 `ui-monochrome.md`。

## 用户提出的三个功能

1. **角色-模型路由**：把任务拆成 Think / Plan / Execute / Review，每个角色单独指定模型，可视化界面随时改。
2. **小模型参谋**：根据任务与历史结果，建议「哪个环节用哪个模型更划算」，用户确认后生效。
3. **多 Agent 协同编排**：让上面这些环节真的协同跑起来，解决大模型成本高 / 能力冗余、便宜模型深度不足的问题。

## 审查结论

| 功能 | 能否现在加入 | 依据 |
| --- | --- | --- |
| 1 角色-模型路由（配置 + 界面） | **能，本轮落地** | 这是配置能力，不是策略决策。已有 `ModelConfig` 版本快照、`Run.config_snapshot` 冻结机制、`AgentRun.role` 预留字段，可复用 |
| 2 小模型参谋（建议层） | **不能现在加** | 属于策略决策组件，`goal.md` 明确 G4 之前禁止；且 G4 评测 Runner 至今没跑过一次真实模型评测，没有成本 / 成功率 / 耗时反馈，建议必然是瞎猜。放到 G5 只读建议器，且必须用户确认才生效 |
| 3 多 Agent 执行链 | **配置与预览能加，执行不能加** | 执行依赖 LangGraph 图执行层（检查点恢复 B 片）与真实子 Agent 并发，均未实现。本轮只做编排配置与流程链预览，**不冒充已能跑** |

一句话：**你要的「可视化选模型」可以现在给，你要的「小模型替你判断」必须等评测数据。**

## 本轮落地范围（切片 D）

- 后端：`role_bindings` 表 + 迁移 `0004`、只读 `GET /api/v1/orchestration/bindings`、写入 `PUT /api/v1/orchestration/bindings`、测试。
- 前端：新增「编排」页，严格复用 `ui-monochrome.md`：角色卡 + 模型下拉 + 流程链预览 + 保存；导航加入口。
- 冻结语义：每次保存生成 `version`；**未来**创建 Run 时把映射快照写入 `config_snapshot`，运行中改配置不影响在途任务。本轮不改 `Worker`、不改 `enqueue`。
- 明确不做：不写 Worker / 图执行层、不改 Run 状态集合、不引入策略决策组件、不自动应用任何建议、不展示虚构的成本或成功率数据。

## 复杂度判定

| 条件 | 命中 | 依据 |
| --- | --- | --- |
| H1 文件 ≥5 | 是 | `models.py`、迁移 `0004`、`schemas.py`、`services.py`、`api.py`、`tests/test_role_bindings.py`、`web/src/types.ts`、`api.ts`、`Orchestration.tsx`、`App.tsx`、`styles.css`、测试与文档 |
| H2 数据结构 | 是 | 新表 + 新迁移 |
| H3 对外接口 | 是 | 新增 GET / PUT 两个端点 |
| H4 权限/认证 | 是 | 复用现有会话与本地 Origin / `X-Omni-Request` 边界 |
| H5 金额/计费 | 否 | 不展示费用、不计费 |
| H6 公共模块 | 是 | `services.py` / `api.ts` / `types.ts` 为公共入口 |
| H7 新依赖 | 否 | 无新依赖 |
| H8 跨端协同 | 是 | 前后端契约同时变化 |
| H9 部署/环境 | 否 | 不改 Compose |
| H10 业务口径 | 否 | 角色集合与保存语义已明确 |

命中 6 项，按大变更治理：先写本设计，再改码，验收据实回填。

## 契约

角色固定白名单：`think` / `plan` / `execute` / `review`（与 `AgentRun.role` 预留值一致，另保留 `verifier` 供未来扩展）。

`GET /api/v1/orchestration/bindings` 返回：

```json
{"version": 1, "bindings": [{"role": "think", "model_config_id": "...", "model_label": "...", "enabled": true}], "updated_at": "..."}
```

`PUT` 请求体：`{"bindings": [{"role": "think", "model_config_id": "..."}]}`，服务端校验角色白名单、模型存在且启用、同一角色不重复；整组替换并递增 `version`。空 `model_config_id` 表示该角色未配置，界面显示「未指定」。

## 红线

- 配置只影响**未来**的 Run；本轮不接入执行层，不得写成「已能按角色调用不同模型」。
- 不自动根据成本/成功率调整映射，不写入任何策略或经验。
- 前端不得显示虚构的成本、成功率或推荐分数；参谋层未实现时界面如实说明「建议将在评测数据可用后开放」。

## 验收（实际结果待填写）

| 项目 | 验证方式 | 实际（2026-11-08 13:50） |
| --- | --- | --- |
| 后端契约 | 白名单校验、重复角色、模型不存在 / 停用、整组替换与 version 递增 | 通过：7 项新测试；停用模型 422、重复角色 409、不存在模型 404、非法角色 422 |
| 权限 | 未配对 / 缺请求头被拒 | 通过：401 / 401 / 403 |
| 前端 | npm test、npm run build、编排页截图 | 通过：6 passed；构建 ✓ 4.25s；`06-orchestration.png` 显示 `CONFIG v1`、`2 / 5 已指定`、流程链与角色卡正确 |
| 回归 | ruff、pytest（独立 basetemp）、verify_g1、磁盘边界 | 通过：ruff All checks passed；`108 passed, 4 skipped in 182.87s`；PG `4 passed in 3.04s` + 恢复 42 + G1 OK；边界 result OK |

**执行层未读取 `role_bindings`**：保存配置只持久化并展示，当前 `enqueue` / `Worker` 仍按单个 `model_config` 运行，保存不会改变实际模型选择。不得把配置页、构建或后端单测记为多 Agent 执行已实现。参谋层（G5）与多 Agent 执行链仍未实现；Docker / 容器链路仍未实测。
