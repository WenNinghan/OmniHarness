# 本地安装与运行

当前版本是 M3 预览：提供本地浏览器配对、模型配置、连接探测、持久化流式聊天、运行事件和 Agent 任务预览。Agent 执行需要显式启用，并通过 PostgreSQL 与检查点等就绪检查；预览阶段不代表全部兼容场景已经验收。研究/代码工作流、MCP 执行和工具沙箱属于后续里程碑。

## Windows

准备 Docker Desktop，使用 Linux 容器引擎，并确认 Docker Compose v2 可用。安装和首次镜像构建需要联网。脚本不会安装系统组件、请求管理员权限、修改执行策略或重启电脑。

### 尚未安装 Docker 时的开发预览

这是显式 SQLite 开发模式，不代表 PostgreSQL 和容器部署已经验证。需要 uv、Python 3.12 和 Node.js 24；首次准备在项目目录执行 `uv sync --python 3.12`，然后进入 `web` 执行 `npm ci`、`npm run build`。

回到项目目录运行：

```powershell
.\scripts\dev.ps1 start
.\scripts\dev.ps1 pair
.\scripts\dev.ps1 status
.\scripts\dev.ps1 stop
```

`start` 在后台启动 API 和 Worker，打印本机地址与一次性配对码，并打开浏览器。`pair` 在需要时重新生成代码。`stop` 只停止此项目记录的进程，保留开发数据。默认使用 `.omniharness/data/local-dev.db`；该 SQLite 文件不会自动迁移到正式 PostgreSQL。开发预览和 Compose 不应同时占用同一端口。

### Docker 部署

PowerShell 脚本默认使用 Windows 上的 `docker` 命令。若已自行准备一个可用的**专用 WSL2 发行版**，其中已安装 Docker Engine 和 Compose v2，也可在当前 PowerShell 会话中显式选择它：

```powershell
$env:OMNI_DOCKER_BACKEND = 'wsl'
$env:OMNI_WSL_DISTRO = 'YourDedicatedDistro'
.\scripts\start.ps1 -StartEngine
.\scripts\pair.ps1
.\scripts\doctor.ps1
.\scripts\stop.ps1
```

将 `YourDedicatedDistro` 换成实际已存在的发行版名称。`-StartEngine` 在该 WSL 发行版内尚无可用 Docker daemon 时启动它；若 daemon 已运行则复用。脚本不安装 WSL 或 Docker，也不修改系统 PATH、代理或防火墙。`stop.ps1` 只停止本项目服务和保留其数据，不关闭 WSL 或 Docker daemon，避免影响其他容器。下一次打开 PowerShell 需重新设置两个会话变量；如已确认镜像就绪，可在启动时增加 `-NoBuild`。

WSL 模式要求项目位于 WSL 可访问的 Windows 盘符路径；UNC 网络共享路径暂不支持。项目配置和秘密路径由 `wslpath` 转换，配置仍保存在当前项目目录；已有 SQLite 开发库不会自动迁移到 Compose PostgreSQL。请先确认 WSL 虚拟磁盘和镜像所在磁盘有足够空间。

本机隔离验证使用了 G 盘专用 WSL2、Windows PowerShell 5.1、含中文和空格的项目路径、独立 PostgreSQL 卷与端口。验证中的 `-NoBuild` 复用了此前以临时 npm 镜像和原锁文件构建的测试镜像；这不能代替在其他机器上实测完整镜像构建。

将项目解压到自己可写的本地目录。在该目录打开 PowerShell：

```powershell
.\scripts\start.ps1
.\scripts\pair.ps1
```

浏览器打开启动脚本显示的地址，默认是 `http://127.0.0.1:8787`，输入配对命令生成的一次性代码。配对码仅用于当前本机浏览器，不要放进问题报告或截图。代码失效时重新运行 `pair.ps1`。已配对浏览器不需要每次重启重新配对。

如果 Windows 将下载文件标记为来自互联网，先检查脚本内容，再通过文件属性的“解除锁定”处理。若组织策略禁止脚本，请按组织规定运行；本项目不会自动绕过策略。

启动脚本通过绝对路径定位项目。中文和空格路径已完成脚本参数与目录权限验证，Docker 容器挂载仍需运行验证，详见 [兼容性记录](compatibility.md)。首次构建会构建 Web 静态资源并安装锁定的 Python 依赖。再次使用已有镜像时可以运行：

```powershell
.\scripts\start.ps1 -NoBuild
.\scripts\doctor.ps1
.\scripts\stop.ps1
```

`stop.ps1` 只停止服务，保留数据库卷和本地文件。`doctor.ps1` 检查引擎、Compose 配置、服务状态和健康接口，不读取或打印凭证内容，也不发起收费模型调用。

## Linux 与 macOS

准备可运行 Linux 容器的 Docker 引擎和 Compose v2。从项目目录运行：

```sh
sh scripts/start.sh
sh scripts/pair.sh
sh scripts/doctor.sh
sh scripts/stop.sh
```

可以用 `sh scripts/start.sh --no-build` 复用已有镜像。脚本把当前 UID/GID 保存为容器运行身份，因此应由同一个本地用户管理此安装。不要使用 `sudo` 启动应用脚本来绕过 Docker 访问问题。

## 服务和数据

| 项目 | 位置或职责 |
|---|---|
| `api` | 提供 HTTP API 和编译后的 Web；宿主只绑定 `127.0.0.1` |
| `worker` | 独立处理数据库中的聊天任务 |
| `init` | 一次性执行 `omniharness bootstrap`，初始化主密钥并迁移数据库；成功退出是正常状态 |
| `postgres` | PostgreSQL 17；不向宿主开放端口 |
| PostgreSQL 数据 | Compose 的 `postgres-data` 命名卷 |
| 应用数据 | 项目内 `.omniharness/data`，容器路径 `/data` |
| 本地主密钥与部署密码 | 项目内 `.omniharness/secrets`，容器路径 `/secrets` |

API 和 Worker 等初始化成功后才启动，其秘密目录挂载为只读。基础部署默认关闭工具；M4 工具部署另见 [本机部署说明](M4-preview-deployment.md)。仅独立 Sandbox Controller 接触 Docker socket；工具容器只挂载过滤快照，不挂载模型秘密、原项目或宿主整盘。

脚本生成随机数据库密码到 `.omniharness/secrets/compose.env`，主密钥由应用 CLI 生成。Windows 上秘密目录和应用数据目录限制为当前用户；Unix 上目录权限设为 `700`，部署密码文件为 `600`。不要手动覆盖该密码文件：已有 PostgreSQL 数据卷的密码不会随文件修改而自动改变。

模型 Key 在 UI 中提交后由服务加密保存。配置导出不包含 Key，导入后需要重新填写凭证。主密钥与数据库需要一起保留才能恢复已有凭证；丢失主密钥后必须重新输入模型 Key。

### 多份源码目录的 Compose 归属

PowerShell 与 Shell 部署脚本会在操作前检查同名 Compose 容器的项目目录和配置文件标签；无法核实归属、同名容器缺少必要标签，或只发现同名数据库卷而没有可证明归属的容器时，脚本会拒绝继续。此时不要删除卷来绕过检查。`COMPOSE_PROJECT_NAME` 默认由 Compose 文件决定；若有多份源码副本，分别在各自的终端会话设置不同的项目名，例如：

```powershell
$env:COMPOSE_PROJECT_NAME = 'omniharness-myproject'
```

选择新项目名会创建独立的 Compose 服务和数据库卷，不会自动迁移旧项目的数据。正常归属于当前目录的旧容器仍可使用；`-NoBuild` / `--no-build` 只会从该目录已核实的应用容器镜像 ID 派生专属标签，不会无条件复用全局 `omniharness:local`。若专属标签与当前项目容器镜像不一致，需重新构建。每个目录/项目组合使用独立镜像标签；PowerShell 和 Shell 入口的标签可能不同，但会检查当前项目容器的镜像身份。

这些检查依赖 Docker 管理的标签与文件系统的实际路径，并不是对恶意本机用户的安全隔离。PowerShell 在同一 Windows 会话内为同名项目加锁并在锁内再次检查；Shell 入口和非协作的直接 Docker 命令不共享该锁，跨入口或跨会话并发操作同名项目仍需使用者自行避免。

## 本地设置

可在项目根目录创建 `.env`，例如：

```dotenv
OMNI_ENABLE_DEMO=false
OMNI_PUBLIC_PORT=8788
OMNI_ALLOWED_ORIGINS=http://127.0.0.1:8788,http://localhost:8788
```

更改公开端口时，应同时更新允许的 Origin。`OMNI_PUBLIC_PORT` 控制宿主端口，容器仍使用 `8787`。Compose 脚本先读取可选的根 `.env`，再读取秘密目录中的部署配置；启动脚本自动管理数据库密码和 UID/GID。Docker Compose 的宿主环境变量优先级更高，不要在 shell 中意外覆盖这些值。

Agent 执行采用显式开关，默认 `OMNI_ENABLE_AGENT=false`，检查点 schema 默认 `omniharness_checkpoints`。Compose 的 init、API 和 Worker 读取同一组值；Windows WSL 模式下当前 PowerShell 进程设置的 `OMNI_ENABLE_AGENT` 和 `OMNI_CHECKPOINT_SCHEMA` 也会传给 Compose。**设置开关不等于 Agent 已就绪：产品运行时总闸、PostgreSQL 与受保护检查点的 readiness 检查也必须通过**。显式启用前应先停止活动任务并备份数据库和秘密目录；启用时 init 会初始化检查点 schema。SQLite 开发库不会自动导入 PostgreSQL。

此部署面向单用户本机使用。不要把端口映射改为公网地址来代替正式的远程部署设计。

## 演示和真实模型

默认允许显式 Mock 演示，可用于体验聊天和事件流。Mock Run 和输出会标记为模拟，不能用于证明 Provider 已连接、费用已发生或执行结果已验证。

没有真实 API Key 时，真实模型配置的请求会失败，不会自动回退到 Mock。使用真实模型前，在模型设置中填写自己的 Key，选择所需能力进行连接测试。测试会向指定 Provider 发起请求，可能产生其 API 费用。

流式请求若因 Worker 重启而中断，当前阶段保留已保存文本并标记 `interrupted`；不承诺接着原远端请求无损继续。重新发起请求会是一次新的模型调用。

## 常见问题

- **找不到 Docker 或引擎未启动**：启动 Docker Desktop，确认 Linux 引擎就绪后再运行脚本。
- **端口已占用**：在根 `.env` 同时修改 `OMNI_PUBLIC_PORT` 和 `OMNI_ALLOWED_ORIGINS`，重新启动。
- **初始化服务退出**：退出码 `0` 表示迁移完成；非零时运行诊断，并查看初始化服务的错误。不要删除数据卷来掩盖迁移错误。
- **无法配对**：运行配对脚本获取新代码，确认浏览器地址与允许的 Origin 一致。
- **模型测试失败**：在 UI 查看经过脱敏的错误，检查模型名称、接口协议、Endpoint 和 Key；不要把完整凭证贴进日志或问题报告。

诊断时不要运行并公开粘贴 `docker compose config` 的完整输出，它会展开部署密码。脚本只使用 `config --quiet`。不要上传 `.env`、`.omniharness/secrets`、数据库备份或含私有对话的应用数据。

## 升级与备份

升级前停止 API/Worker，备份 PostgreSQL 数据库以及 `.omniharness/data` 和 `.omniharness/secrets`，再构建新版本并启动。数据库备份应使用 PostgreSQL 的标准逻辑备份流程，不能在数据库运行中直接复制卷文件。首版没有自动升级和数据库降级机制；迁移失败时保留备份并停止使用新版本。项目脚本不会删除命名卷。

发布者应分别记录 Windows 安装、容器构建、PostgreSQL 迁移和真实 Provider 的实际验证结果。脚本语法检查或 Mock 测试不能替代这些运行验证。
