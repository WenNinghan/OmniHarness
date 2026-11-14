# M0–M2 开发预览实施报告

日期：2026-11-05。版本：`0.1.0a1`。本轮范围来自用户确认的首轮 M0–M2 计划。

已实现可运行的 BYOK 聊天框架，并在 Windows 本机接通真实模型。当前地址是 `http://127.0.0.1:8787`，使用显式 SQLite 开发模式。**M0 的 Docker/PostgreSQL 与挂载验收仍未完成，因此这不是完整 v0.1 发行，也不表示 M0–M2 所有验收条件已经通过。**

## 已交付行为

- FastAPI API、独立 Worker、SQLAlchemy 数据模型、Alembic 初始迁移、REST 与持久 SSE。
- React/TypeScript/Vite 控制台：一次性配对、会话历史、流式聊天、取消、运行状态、模型设置、四项连接测试、配置导入导出。
- OpenAI、Anthropic、Gemini、OpenAI-compatible 适配；显式 Mock 演示。真实请求失败不会伪装为模拟成功。
- 凭据加密、导出去凭据、配置版本冻结、持久入队、消息幂等、停止保留文本、浏览器重连补事件。
- 可信 Python 插件入口点与 API 版本边界；可安装的 Verifier 示例。
- 依赖锁、Compose/Dockerfile、Windows/Linux 启停诊断脚本、Windows 无 Docker 开发入口、MIT 许可证和 CI 配置。
- Deep Agents/LangGraph 技术探针，以及 10 项研究、5 项 Python、5 项 TypeScript 固定任务。

普通聊天直接使用模型适配器。Deep Agents/LangGraph 目前用于独立 M0 探针，多 Agent 工作流未作为聊天产品功能开放。

## 实际验证结果

| 项目 | 验证方式 | 实际结果 |
|---|---|---|
| 后端回归 | `.venv/Scripts/python.exe -m pytest -q` | **64 passed, 3 skipped**；跳过项为未配置数据库的 PG 集成测试 |
| Python 检查 | `.venv/Scripts/python.exe -m ruff check .` | All checks passed |
| 依赖锁 | `uv lock --check` | 成功，91 个包 |
| 框架机制 | `python scripts/runtime_probe.py` | 实际 Deep Agents 工具循环通过；两子 Agent 的 max_parallel=2；SQLite 关闭重连后恢复结果 42 |
| 探针边界 | 返回 `mode=offline-deterministic-model` | 无需托管账户；不证明真实模型规划质量或 PG 检查点恢复 |
| 前端构建与测试 | 前端子任务执行 `npm run build`、`npm test` | 构建通过、3 项状态归并测试通过；本次 npm audit 为 0 项漏洞 |
| 浏览器实操 | 前端子任务 Playwright：桌面和 390px 移动视口 | 配对页、真实历史、模拟发送、刷新恢复、设置、新增模型、能力测试可操作；无页面错误；移动无横向溢出 |
| 时区 | 新增 2 项后端回归；前端子任务重读 API/页面 | SQLite 时间含 `+00:00`，上海浏览器显示正确本地时间 |
| 重启保留 | `dev.ps1 stop`、`start -NoBrowser -NoPair` 后读取真实 Run | 原 Run 仍为 completed/live，未再次请求模型 |
| 插件 | 本地安装 `examples/plugin`，CLI 与 `/api/v1/plugins` 查询 | `example.required-sections` 为 available，无需修改内核 |
| Python 包 | `uv build --wheel`，检查 ZIP 目录 | `dist/omniharness-0.1.0a1-py3-none-any.whl` 含 CLI 和包内迁移；wheel 不含 Web 分发包 |
| 脚本 | 部署子任务：PS AST、Shell 语法、本机路径/ACL 检查 | 随机部署密码、重复初始化、中文/空格脚本路径通过；诊断正确报告 Docker 缺失 |
| 固定任务 | 部署子任务：JSON/字段/ID/引用、Python AST、TS7 strict noEmit | 20 项结构通过；不是 20 次模型评测，尚无成功率、成本或质量提升结论 |
| CI | `.github/workflows/ci.yml` 配置与解析 | 已配置 Python3.12/PG17 和 Node24 两个 job，未在远端运行 |

本地 UI 检查图像保存在 `.omniharness/qa/`，其中 `02-saved-real-history.png` 为真实历史，`07-mobile-home.png` 为移动视图；该目录不进入 Git。

## 真实 Provider 验证

使用用户明确授权的本地配置，并在调用前告知来源和地址。模型为 `deepseek-flash`，Base URL 为 `https://e-flowcode.cc/v1`，请求地址为 `https://e-flowcode.cc/v1/chat/completions`。

四项真实探测均完成：聊天、流式、工具调用后回传结果的完整往返、结构化输出。工具协议探测不代表已经开放沙箱工具。

另执行 API → 持久队列 → Worker → 真实 Provider → 保存回复/事件的完整调用。Run 为 `392f6154-f122-49ef-9fcb-e18a5cec0cae`，结果 completed/live，回复“OmniHarness 实际 API 调用已连通。”。Provider 返回输入 70、输出 57、总计 127 tokens；费用未知，保留 null。重启后读取该 Run 成功。

凭据只进入本地加密存储，未写入普通导出、代码或报告。只实测这一组兼容 Endpoint/模型；OpenAI、Anthropic、Gemini 原生 Endpoint 未实测，不能由离线测试推断全部可用。

## 尚未完成的验收

1. 本机没有可用 Docker，未构建/启动容器、未验证 Docker Desktop 中文目录挂载。脚本不自动安装系统组件。
2. 未启动实际 PostgreSQL，官方 PostgreSQL Checkpointer 的进程重启恢复、PG 迁移和 Worker 排他锁待实测。3 项 PG 集成测试只接受专用 `omniharness_test` 数据库；无环境时跳过。
3. Worker 重启将进行中的远端流记为 interrupted，保留已保存文本。完整 Run 暂停/补充要求/检查点恢复属于 M3。
4. 工作区授权、沙箱、MCP、外部动作审批、研究/代码工作流、Reviewer/Verifier 自动循环及 React Flow 运行图属于 M3–M6。
5. 两个真实 Provider、干净机器安装、备份恢复演练、版本化镜像与公开发行属于后续发布验收。本轮没有提交或推送仓库。

## 现在如何使用

服务已在本机后台运行。打开 `http://127.0.0.1:8787`；如要求配对，在项目目录执行：

```powershell
.\scripts\dev.ps1 pair
```

模型设置中已保存 `DeepSeek Flash · 本机配置`，可以选择并开始聊天。Mock 另有明确标记。停止用 `dev.ps1 stop`，启动用 `dev.ps1 start`；历史保留在开发数据库。

下一步先补 Docker/PostgreSQL 环境验收，再进入 M3，继续复用现有配置、事件、权限和模型层。
