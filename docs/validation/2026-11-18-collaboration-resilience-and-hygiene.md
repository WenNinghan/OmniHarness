# 多阶段协作结构化契约、无冲突多路并行与工程治理收尾验证说明

日期：2026-11-18（Asia/Shanghai）。状态：开发预览，已完成离线单测、集成回归与公开仓库预检验证；本文不包含任何供应商私有端点、本机路径、凭据或原始模型响应。

## 本次变更解决什么

结合既有真实任务测试与公开 CI 记录，本轮在不改动任何已冻结阶段提示词哈希（`role_version`）、不新增数据库迁移、不放宽安全与计账边界的前提下，完成了五项定向改进：

1. **结构化 JSON 唯一闭合围栏提取与可选 `json_mode` 绑定（`structured_json.py`、`m3_stages.py`、`ai_routing.py`）：**
   - 此前阶段输出与 AI 分工规划仅接受纯 JSON 或全文精确等于单个代码围栏（`re.fullmatch`），模型在合法 JSON 代码块前后附带礼貌说明即会触发格式失败。现升级为精确扫描闭合 Markdown 围栏：当所有围栏均闭合且仅存在唯一一个无歧义 JSON 围栏块时提取内部原文校验；未闭合围栏（截断）、多个候选 JSON 围栏块（歧义）或无围栏纯文本说明继续严格拒绝，绝不猜测或修补残缺输出。
   - 在无外部工具调用的纯结构化阶段（`plan`、`dispatch`、结构化 `review` 与前置澄清判定），若冻结模型配置已显式声明支持 `json_mode`（`capabilities.json_mode.status == "supported"` 或 `parameters.omni_stage_json_mode == true`），自动绑定 `response_format={"type": "json_object"}`；默认不向未验证端点盲发，也绝不用于带工具的执行阶段或自由文本阶段。
2. **交互式工作流打通审查降级交付与只读探索后澄清（`m3_stages.py`）：**
   - 将可选的 `OMNI_REVIEW_FAILURE_POLICY=deliver_unreviewed` 从 `v4`—`v6` 扩展兼容至交互式工作流（含 `v7`—`v9`），并在交互式最终汇总阶段注入显式约束：当后置独立审查降级未完成时，禁止最终答复声称独立审查已完成或通过。
   - 细化子任务中途请求用户澄清（`needs_user`）的工具副作用防线：区分零外部写副作用的已完成只读工具（`workspace_list`、`workspace_read`、`workspace_search`、`artifact_read`）与写/执行工具（`workspace_patch`、`run_command`、`mcp_call`），并识别前序已完成子任务的工具归属（`host_task_id`）。允许模型在只读勘察代码后向用户发起澄清；一旦当前子任务已发生写/执行操作或全任务存在任何未结清/占槽工具，继续严格阻断重入（`CLARIFICATION_REQUIRES_REVIEW`）。
3. **无冲突 `write_paths` 子任务多路并行（`m4_collaboration.py`）：**
   - 将 `independent_batch` 从固定最多并发 2 个子任务提升至贪心选取最多 4 个两两写路径完全不相交（含大小写不敏感与父子目录前缀碰撞检测）的就绪子任务同批执行；`write_paths` 为 `null`（写范围未知）时继续严格单任务串行。
4. **复合 Shell 命令退出码遮蔽元数据（`shell_command_audit.py`、`m4_tools.py`、`review_evidence.py`）：**
   - 词法识别字符串命令中未加引号的 `;`、换行、单管道 `|` 与 `||`（排除短路连接 `&&`、参数数组 `list[str]`、注释及 `>|` 重定向）。当存在末尾退出码遮蔽前序失败的结构时，在工具结果与审查证据包中附加只读提示 `compound_exit_masking` 与 `exit_code_scope="terminal_segment_only"`，不修改原命令文本、绑定指纹或消息认证码。
5. **测试套件零弃用告警与公开同步预检工具（`tests/conftest.py`、`scripts/sync_public_repo.py`）：**
   - 在 `tests/conftest.py` 统一处理 Windows `SelectorEventLoop` 策略并移除 20 个测试文件中已弃用的 `event_loop_policy` 夹具重写，消除 CI 中的 69 条 `PytestDeprecationWarning`。
   - 新增可复用的白名单同步与隐私预检脚本 `scripts/sync_public_repo.py`，自动核验禁止追踪目录、Python AST 等价性及新增差异行的敏感模式。

## 验证范围与已知边界

- **自动化验证：** Ruff 静态检查、新增专项测试（结构化围栏正反例、`json_mode` 阶段隔离、`v9` 审查降级与只读澄清放行/写操作拦截、四路无冲突并发分批、复合命令遮蔽词法识别、公开同步隐私扫描）及完整前后端 GitHub Actions（含专用 PostgreSQL 17 服务容器）通过，弃用告警清零。
- **未覆盖边界：** 本轮未新增真实外部模型 API 调用，尚未对各供应商端点的 `json_mode` 做实网探测；复合命令元数据仅提示退出码覆盖范围，不等同于自动拆解或重跑子命令。
