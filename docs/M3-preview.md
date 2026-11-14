# M3 Agent 开发预览

更新：2026-11-08。当前 M3 路径已接入项目 Worker、受保护的 PostgreSQL 检查点和 Deep Agents；以显式开关启动，并以服务端就绪检查控制界面入口。它是开发预览，尚不是完整 v0.1 发行验收。

## 运行结构

Web 控制台通过 FastAPI 提交聊天或 Agent Run；业务数据库保存会话、命令、运行节点、预算和事件。独立 Worker 获取执行权，普通聊天使用模型流；Agent 使用固定版本的 LangGraph 工作流和 Deep Agents，按 Run ID 关联 PostgreSQL 检查点。关键事件先持久化，浏览器可通过 SSE 游标补拉。

角色为 Coordinator、Researcher、Coder 和 Reviewer。它们可对用户提供的内容进行分析、委派和汇总；没有授予联网搜索、项目文件访问、Shell 或 MCP 工具。Verifier 是后续可执行验证器的预留标识，不要求单独绑定模型。运行详情中的每一行是实际模型调用节点，不等于完整的 Agent 生命周期或父子委派图。

创建 Agent Run 时冻结工作流与角色版本、模型绑定、加密凭据快照和预算；运行期间修改配置影响后续任务。模型须通过工具调用能力测试；不符合要求的模型不会静默切换到 Mock。模型服务实际返回的用量才能计入展示，未知费用保持未知。

## 启用与使用

Agent 默认关闭，仅支持 PostgreSQL。使用 Compose 前请先停止活动任务并备份已有数据库和秘密目录；在项目根目录的 `.env` 设置：

```dotenv
OMNI_ENABLE_AGENT=true
OMNI_CHECKPOINT_SCHEMA=omniharness_checkpoints
```

在 Windows 专用 WSL2 环境也可在当前 PowerShell 会话先设置：

```powershell
$env:OMNI_DOCKER_BACKEND = 'wsl'
$env:OMNI_WSL_DISTRO = 'YourDedicatedDistro'
$env:OMNI_ENABLE_AGENT = 'true'
.\scripts\start.ps1 -StartEngine
.\scripts\pair.ps1
```

将发行版名称换成已安装并配置好 Docker Engine 的实际名称。`init` 在迁移后初始化官方 PostgreSQL Checkpointer 和项目保护结构；API 对数据库、版本和保护结构进行就绪检查。检查不通过时 Agent 不可选，不会自动回退成聊天。配对后配置模型、测试工具调用，再在输入区选择 Agent 模式。完整启停、Windows 原生 Docker、Shell 和备份说明见 [安装说明](install.md)。

SQLite `dev.ps1` 为普通聊天开发模式，不支持 M3 Agent。SQLite 会话、模型设置和凭据不会自动迁入 Compose PostgreSQL；更换部署后需要在新控制台配置模型。切换不会自动删除旧数据；需要历史迁移时应先备份，并另行设计和验证迁移流程。

## 暂停、继续和待核对

暂停、继续和补充要求先成为持久命令；暂停在安全调用边界形成检查点并让出 Worker。继续沿用原 Run，在持有执行权并检查工作流版本及检查点后恢复。补充要求以稳定标识进入后续模型输入；聊天开发模式中的同名操作仅记录，不代表已发给模型。

取消会保留已保存文本和事件，尽力停止在途调用。数据库锁丢失、外部操作结果不明确、部分崩溃后执行时间无法核算或版本不兼容等情况可能停止自动恢复并进入“待核对”。已经发给模型的请求不能保证撤回；结果不明的调用不会自动重放，也不能宣称外部调用恰好执行一次。

模型与工具调用、委派和活跃时间受 Run 预算约束。默认上限是 50 次模型调用、100 次工具调用、2 轮修订和 1800 秒活跃时间，可通过设置项调整；同一 Run 的主子 Agent 共享预算。最多两个活跃子 Agent，委派最多两级；等待后代的父任务会让出执行位置。工具和修订的预算结构不代表已开放对应业务能力。

自动摘要目前关闭。每次模型调用前检查消息数和序列化字符数，超限则明确停止；该检查不是 token 计数，也不自动压缩对话。

## 验证证据与限制

- **真实模型：**对已配置的 OpenAI-compatible `deepseek-flash` 进行一次完整 Agent 冒烟，实际记录 Coordinator → Researcher → Coordinator，3 次模型调用、1 次委派，Run 完成且答案通过检查。此结果只证明该次模型、端点和任务；费用数据未提供，保持未知。另有真实调用因连接错误进入待核对，未自动重放。
- **产品容器与浏览器：**独立 WSL2 Docker、PostgreSQL、API、Worker 与本机假模型中，验证了发送、子 Agent 委派、暂停、继续、SSE、节点展示及 API/Worker 重启后历史保留；真实浏览器交互与刷新也经过检查。假模型不证明其他 Provider 兼容性。
- **恢复与安全：**真实专用 PostgreSQL 故障测试覆盖子 Agent 检查点恢复、双层委派与并发上限、失锁后旧 Worker/检查点写入防护、取消与不确定操作。使用本机假服务和合成 Key 检查了异常脱敏、PostgreSQL 检查点及云追踪禁用；这不是针对所有 Provider、恶意插件或所有故障模式的审计。
- **安装边界：**本机使用 WSL2 Docker Engine 验收过 Compose 和中文、空格路径。隔离构建临时使用 npm 镜像源，保留原锁文件的完整性校验；Windows Docker Desktop 原生路径及官方 npm 源原样完整联网构建仍未验证。

首次完整 Agent 运行需要工具调用能力合格的模型。除上述单次真实模型冒烟外，其它 Provider 原生端点、完整 20 项任务质量评测、独立 Linux 主机安装、备份恢复演练与 M4/M5 功能均不能由本轮结果推断通过。

检查点可能保存用户消息与工具结果；模型 Key 仅由受信任宿主在模型运行时提供，不应写进消息、事件或日志。Python 插件属于受信任代码，当前无恶意插件隔离。运行详情仅显示真实模型调用节点，不提供完整委派关系图。
