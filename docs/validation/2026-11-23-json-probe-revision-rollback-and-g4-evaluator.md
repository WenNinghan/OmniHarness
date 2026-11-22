# JSON 模式探测、受限修订闭环、安全补丁回滚、时间线与评测引擎验证说明

日期：2026-11-23（Asia/Shanghai）。状态：开发预览，已完成离线单测、模拟全流程集成回归、前端构建与单测、确定性评测自检及日常脚本加固；本文不包含任何供应商私有端点、本机路径、凭据或原始模型响应。

## 本次变更解决什么

在不改动任何已冻结阶段提示词哈希（`role_version`）、不新增数据库迁移、不放宽安全与预算边界的前提下，本轮围绕模型能力可测性、过程可审计性、受控纠错恢复与离线可复现评测完成了六项工程能力建设：

1. **单项 `json_mode` 能力探测与前端入口（`schemas.py`、`request_policy.py`、`providers.py`、`services.py`、`structured_json.py`、`ModelSettings.tsx`）：**
   - 在模型连接测试白名单中新增独立探测项 `json_mode`（默认 4 项常规探测保持不变，避免额外盲发请求）；仅针对 OpenAI / OpenAI-compatible 适配器执行单次最小化 `response_format={"type": "json_object"}` 请求，并严格校验返回体为未经代码块包裹的合法 JSON（`{"ok": true}`）。
   - 将受限保留参数 `omni_stage_json_mode` 纳入白名单并通过 `provider_parameters` 剥离，确保不会透传给上游供应商、也不影响能力指纹绑定。UI「模型连接」面板提供独立「测试 JSON 模式」按钮与状态徽章，`capabilities.json_mode` 与模型配置快照绑定后，无工具结构化阶段可安全自动启用或显式关闭（`omni_stage_json_mode: false`）原生 JSON 输出。

2. **统一 Diff 补丁对比视图与复合 Shell 命令警告（`diff-view.ts`、`ToolActivity.tsx`、`WorkspaceTools.css`）：**
   - 工具活动明细不再只显示紧凑字节哈希摘要：`workspace_patch` 工件自动渲染目标路径、`+N / -M` 行增减徽章、`before_sha256 -> after_sha256` 转换与带行内高亮的统一 Diff 代码块。
   - 当 `run_command` 结果携带 `exit_code_scope == "terminal_segment_only"` 时，界面以琥珀色提示徽章明确标出「仅末段退出码（前序子命令失败可能被遮蔽）」，提示人工或审查核对完整标准输出与错误日志。

3. **默认关闭的受限 `Review -> Revise` 自动修订闭环（`settings.py`、`compose.yaml`、`scripts/common.ps1`、`services.py`、`m3_worker.py`、`m3_stages.py`）：**
   - 新增环境开关 `OMNI_REVIEW_REVISION_POLICY`（可选 `"off"` / `"bounded"`，默认 `"off"`，启动时冻结入任务快照）。
   - 仅当策略为 `"bounded"`、非交互式审查返回可定位结构化失败（`FAIL` / `NEEDS_REVISION` 且含具体修复项）、工作区状态安全且无未知结果外部调用（`UNSAFE_STATE` 防线）时，通过原子预算预留 `reserve_budget(db, run_id, "revisions", limits)` 触发有限轮次 `execute -> review -> revise -> review`。
   - 修订子任务与上游请求消息使用轮次作用域标识（如 `revision-{r}` / `revise-{r}` 与 `:r{r}`），并借助 `agent.revision_triggered` 与 `agent.revision_skipped` 事件保障崩溃恢复重放幂等性；预算耗尽或状态不安全时拒绝重入并保留原始故障状态。

4. **终态受控安全手动补丁回滚（`workspace_io.py`、`workspace_broker.py`、`workspace_client.py`、`m4_rollback.py`、`m4_api.py`、`api.ts`、`ToolActivity.tsx`）：**
   - 在工具运行记录表与受控文件系统 IO 层实现按操作身份回滚的端到端链路 `POST /api/v1/tool-operations/{identity}/rollback`：严格要求所属任务处于终态、目标工作区仍处于启用状态且不存在活跃租约（`WorkspaceLease`）。
   - 底层校验 `prepared.json`、`result.json`、`before.bin` 及磁盘实时文件 SHA256，仅当当前文件内容与补丁写入后的 `after_sha256` 完全一致时方可执行恢复；若补丁属于新建文件则在释放读句柄后安全删除，若检测到后续人工或他处改动则拒绝并返回 `version_conflict`。支持 `client_request_id` 幂等重入、防重复回滚（`already_rolled_back`）并落库 `workspace.patch_rolled_back` 审计事件；前端提供二次勾选确认与「已人工回滚」标记。

5. **极简纯黑单色阶段时间线与日常自愈脚本加固（`stage-timeline.ts`、`RunDetails.tsx`、`scripts/doctor.ps1`、`scripts/m4_production_deploy.py`、`scripts/stop.ps1`）：**
   - 任务详情页升级阶段时间线统计：计算并渲染各阶段耗时占比条、修订轮次标记、跳过原因以及完整阶段/工具事件中文标签。
   - `scripts/doctor.ps1` 保持默认只读诊断语义，同时新增 `-Fix` 参数支持在健康检查异常时自动拉起 WSL Docker 引擎与 `scripts/start.ps1 -NoBuild`；加固 `common.ps1` / `m4_production_deploy.py` / `stop.ps1` 中的后台常驻拉起、本地代理绕过（`NO_PROXY`）与停止清理流程。

6. **确定性 20 题 G4 评测评分引擎与 CLI 运行器（`eval_scoring.py`、`scripts/m5_evaluate.py`、`tests/test_m5_eval_scoring.py`）：**
   - 实现针对 `examples/evaluation/tasks.json` 固定 20 题（10 道结构化研究推理 + 5 道 Python + 5 道 TypeScript）的确定性离线评分器：固定 A(8) / B(6) / C(6) 分割、`build_research_prompt` 防泄题模板构建、严格 `python -s -E` 与隔离临时目录 `tsc` 编绎执行验证，以及保留缺失费用的指标汇总（区分 `resolved` 与 `delivered_unreviewed`）。
   - `scripts/m5_evaluate.py` 默认只读输出计划（`--plan`），提供 30 项离线自检（`--self-test`：10 个坏初始代码全败 + 20 个参考解全过），对仅传 `--score-file` 的 holdout Split C 设置隔离护栏，并预留显式授权、可断点续跑的 `--run-live` 入口。

## 验证范围与已知边界

- **自动化验证：** `ruff check src tests scripts` 静态检查通过；专项新增及核心后端回归测试（`json_mode` 探测正反例、受限修订闭环与崩溃幂等、安全补丁回滚与并发/版本冲突拦截、20 题评测引擎自检与分割护栏）通过，Zero 弃用告警；前端 Vitest 单元测试与 Vite 生产构建通过。
- **CI 覆盖升级：** `.github/workflows/ci.yml` 后端流水线已补充锁定版本的 Node 24 与 `web/node_modules` TypeScript 工具链安装，使 Linux CI 中的 PostgreSQL 17 全量回归同样完整覆盖全部 5 道 TypeScript 评测验证与 `m5_evaluate.py --self-test`。
- **未覆盖边界：** 本轮保持零外部真实模型付费调用；真实模型对照评测（`--run-live`）需另行提供明确模型配置与预算上限后触发；既有本机生产容器镜像升级亦未在未执行备份恢复演练前直接覆盖切换。
