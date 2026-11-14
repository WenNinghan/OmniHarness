# M4 独立候选环境

这是本机 Windows + WSL 的联调部署，和 `omniharness` 生产实例使用不同的 Compose 项目、数据库卷、服务凭据、端口和快照目录。M4-A 22 项真实隔离检查已通过；本部署验证不代表 M4 整体已验收。

`deploy/m4-preview.broker.json` 与 `deploy/v6-preview.broker.json` 是仅供参考的示例；其中 `EXAMPLE_PROJECT` 与 `omniharness-example` 均为占位值。使用前必须根据实际项目目录、密钥位置、WSL 映射及 Linux 快照目录重新配置，并核验权限；不要直接运行示例。该预览 profile 不是跨机器通用安装器。

| 服务 | 地址或目录 |
|---|---|
| Web/API | `http://127.0.0.1:18792` |
| OpenSandbox controller | 宿主 `127.0.0.1:19090`；应用内部 `sandbox-controller:8080` |
| Windows 文件 broker | WSL Docker bridge `172.17.0.1:19092`，独立服务鉴权 |
| 注册授权来源 | broker 每次向 `127.0.0.1:18792/api/internal/workspaces/...` 查询，不缓存授权 |
| 原生文件辅助程序 | 固定 `.venv\Scripts\python.exe -I src\omniharness\workspace_io.py --native-rpc` |
| 工具快照 | `/var/lib/omniharness-m4-preview/snapshots`，WSL 私有文件系统 |
| 私有凭据/工件/恢复材料 | `G:\harness\.omniharness\m4-preview` |
| 数据库 | `omni-m4-preview_preview-postgres`，不暴露宿主端口 |

18787–18791 已被此前 QA 栈占用，候选使用 18792；既有 QA 与生产容器均保留。

## 重现步骤

安装依赖、镜像和 WSL 必须已满足 M4-A 的前提。以下动作只针对命名的候选环境；`prepare` 私有复制本机已有 master key，`copy-configs` 只读取现有生产的加密配置，因此仅适用于已经明确授权复制这些配置的本机联调。

```powershell
. .\scripts\g-drive-env.ps1
.\.venv\Scripts\python.exe scripts/m4_preview_deploy.py prepare
.\.venv\Scripts\python.exe scripts/m4_preview_deploy.py broker-env
.\.venv\Scripts\python.exe scripts/m4_preview_deploy.py build
.\.venv\Scripts\python.exe scripts/m4_preview_deploy.py configure
.\.venv\Scripts\python.exe scripts/m4_preview_deploy.py up
.\scripts\start-m4-broker.ps1
.\.venv\Scripts\python.exe scripts/m4_preview_deploy.py copy-configs
.\.venv\Scripts\python.exe scripts/m4_preview_smoke.py
```

`copy-configs` 只允许空候选模型表，仅替换迁移产生的空阶段配置种子，不覆盖已使用候选环境。25 个 Provider / 模型记录保持密文；历史、运行和用户会话不复制。该脚本不调用模型。

`build` 为每次构建保留一个独立目录。前端包含当前 M4 组件，但全局样式固定使用此前部署的 `.omniharness/output-limits-web-20261108/src/styles.css`；不将尚未接受的全局样式随本轮发布。依赖由 `uv.lock` 和 `package-lock.json` 固定，基础镜像按实际检验的 digest 引用。`configure` 固定最终应用 image ID。

配对信息按需在本机生成，不保存到公共文档：

```powershell
wsl -d OmniHarnessCheckAlt -u root --exec docker exec omni-m4-preview-api-1 omniharness auth-code
```

`start-m4-broker.ps1` 通过隐藏的 WSL helper 启动 broker，日志和 Windows helper PID 保存在私有候选目录。启动成功必须以带服务鉴权的 `/health` 和 API `tools/status` 实测为准；当前不是安装到 Windows 开机服务的发行包。

## 已完成的联调

`scripts/m4_preview_smoke.py` 已真实完成配对、25 个模型配置查询、broker 健康检查，以及 `G:\OmniHarness-M4-Fixtures\预览注册 中文 空格` 的注册、NTFS 目录身份确认和文件列表查询。`.env` 占位文件被排除；该样例仅允许 `docs` 前缀写入，命令需要审批。证据保存在 `.omniharness/m4-preview/smoke-report.json`，模型调用数为 0。

原目录不会挂到工具容器。原生 broker 只接受已注册授权、相对路径和结构化操作；工具运行只挂过滤后的 WSL 快照。API/Worker 不接触 Docker socket，只有 controller 挂载 Docker 控制接口。模型 Key、broker Key 和 controller Key 均不挂入工具。

## 界面检查

`scripts/m4_preview_ui.py` 使用已有 Chromium，在独立浏览器上下文做只读检查；配对后阻止所有非读取 API 请求，不提交模型任务。1440×1000 桌面及 390×844 移动端的真实截图位于 `.omniharness/m4-preview/ui`。第一轮发现工作区弹窗处在较低父叠层，移动端关闭按钮被运行详情面板遮挡；修正为 portal 后实际点击通过，并补上表单横向留白，无页面脚本错误或横向溢出。修前截图保留在 `ui-before-modal-fix`。

新候选数据库尚无真实工作区 Run 时，工具调用、审批及工件区明确标为未验证，待真实执行产生记录后检查，不用模拟状态截图代替。

配对码为实例内单次有效值。多个验证脚本必须顺序配对；并行生成会使前一个码失效。

## 真实审批与恢复验收

`scripts/m4_approval_ui_smoke.py` 分为 `start / pending / restart / approve / finish` 五步；必须等其他运行结束再执行。该验收使用已测试的 DeepSeek Flash、单执行 Agent、独立只读样例目录和逐命令审批。脚本只允许通过界面批准唯一的 `python -c "print('M4_APPROVAL_OK')"`，不会批准其他操作。

修复后的实测 Run `937e3c1d-c7ef-47da-971b-fc58aea23f41` 已完成：等待审批时重启候选 Worker，同一审批仍待定且没有命令派发；Playwright 批准后实际命令仅执行一次、退出码 0、保存完整 stdout 工件、归还执行位置。重复相同审批请求未改变命令 ID 或执行结果。实际使用 2 次模型调用、1 次工具调用。证据与截图位于 `.omniharness/m4-preview/approval-ui-fixed`。

首轮验收发现 64 位操作 ID 超出上游 metadata label 的 63 字符限制，创建请求被拒绝。该失败记录及官方校验器核对证据保留在 `approval-ui`，没有改写成通过或自动重放。适配层改用可逆分块后，带真实 64 位操作 ID 的 Python/Node 与取消回收 probe 都再次通过。旧测试占位只在候选数据库依据人工核对证据归档，通用恢复规则仍保留“结果未知需要核对”的边界。

`tests/test_m4_mcp_runtime.py` 另以真实执行图、确定性本地模型和官方 MCP 2.1 HTTP 实服通过 3 项测试：审批前零副作用、批准/拒绝与重复审批、重建图恢复，以及丢失返回后保留待核对而不重试。该组测试不调用付费模型，不向外部服务发送内容。
