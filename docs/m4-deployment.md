# M4 本机更新、完整备份与恢复

> 工具镜像的 Git 支持限定于隔离快照中的本地操作；工作区导出仍过滤原项目的 `.git`，不会自动开放真实仓库历史、状态或网络远程操作。Git 镜像候选使用 `deploy/tool-runtime-git.Dockerfile`，以事先核验的本地镜像别名作为构建参数，构建时禁网；发布前须固定来源与构建结果的镜像摘要并验证真实沙箱。启用状态以实际发行配置和验收记录为准。

适用范围：`G:\harness`、`OmniHarnessCheckAlt` WSL/Docker、原 `omniharness` Compose 项目与 `127.0.0.1:8787`。这不是 M7 干净安装包。应用镜像必须是经过验收且已存在本机的完整 `sha256:<64位>` ID；脚本不构建工作区、不安装 Windows 系统组件、不打印凭据。

## 两条发行路径

| 当前数据库 | 入口 | 行为 |
|---|---|---|
| 0008 | `cutover` | 独立恢复验证 0008→0009，再备份和切换 |
| 0009 | `update` | 独立恢复后核对全部表完全一致，不执行迁移 |

`verify` 要求候选镜像的 Alembic head 恰好为 0009。其他 schema 停止，不能直接用旧程序读取不兼容数据。原 0008 的 `backup` / `verify` 入口和旧清单读取保留；旧清单仅具有当时七张表的校验范围，不能称为完整 M4 备份。

## 服务与持久状态

| 对象 | 生产位置 |
|---|---|
| API、Worker、PostgreSQL | 原 Compose 项目；数据库不暴露宿主端口 |
| Web | `127.0.0.1:8787` |
| Sandbox Controller | 宿主 `127.0.0.1:19096`，唯一拥有 Docker socket 的服务 |
| Workspace Broker | WSL `172.17.0.1:19098`，固定 helper、独立 service key |
| 数据、工件与秘密目录 | 从当前 API 的实际 `/data`、`/secrets` 挂载读取，含回退后新私有目录 |
| 原生补丁恢复材料 | broker 当前的 `native_storage_root` |
| 固定 helper | `.omniharness/m4-production/releases/<image>/src` |
| Controller 数据库及日志 | `/var/lib/omniharness-m4-production/controller` |
| 工具快照根 | `/var/lib/omniharness-m4-production/snapshots` |
| 生效配置 | `.omniharness/deployment.env` |
| 待切换配置 | `.omniharness/m4-production/staged/<image>/` |
| 备份 | `.omniharness/m4-production/backups/<timestamp>`，私有 ACL |

工具容器不挂宿主原项目、模型 Key 或 Docker socket。备份保存补丁恢复材料，不自动覆盖项目文件或回滚用户后来的修改。

## 准备与检查

```powershell
Set-Location G:\harness
. .\scripts\g-drive-env.ps1
$release = 'sha256:<已验收的64位应用镜像ID>'
.\.venv\Scripts\python.exe scripts/m4_production_deploy.py inspect
.\.venv\Scripts\python.exe scripts/m4_production_deploy.py prepare --image $release
```

`inspect` 查询归属、schema、全部非系统 schema 的表数量/哈希、活动 Run、未知操作及工具占用，不输出表正文或 Key。0009 的 `prepare` 只生成 staged 配置和固定 helper，不覆盖现用 broker/controller 配置，也不更新活动 broker 虚拟环境。原 0008 首次安装仍准备原路径及运行环境。

新 `prepare` 固定使用工具镜像 `sha256:b37700184c262593a5535bbb8196b7d28cb07514ec67c9168aca230291aa76ad`，其中含依赖锁固定的 pytest；修改脚本常量不会改变正在运行的生产配置。发布前应验证镜像及真实沙箱，旧工具镜像留存于切换前备份中。

## 0009 一致备份与独立恢复验证

下列命令会短停服务，是明确的维护动作，应在停机窗口已获授权后执行：

```powershell
.\.venv\Scripts\python.exe scripts/m4_production_deploy.py backup-verify --image $release
$backup = 'G:\harness\.omniharness\m4-production\backups\<上一步返回的目录>'
```

执行次序：

1. 核对本 checkout 的 Compose ownership；拒绝活动 Run、未知操作、未完成工具和占用槽。检查本实例沙箱，以及历史 sandbox ID 对应的 egress sidecar。
2. 停止 API、Worker 后重新检查。停机前刚排队的任务也会使操作停止，不能带着任务继续升级。
3. 停止本实例 broker 和 controller。PostgreSQL 保持运行以执行 `pg_dump`。
4. 保存整个实际应用数据库的 custom-format dump，包含全部业务表和 checkpoint schema；复制 data/工件、主秘密目录、controller 秘密目录、native 恢复材料、固定 helper、Compose 与当前配置。
5. 对 controller 状态和快照根生成独立 tar，保存逐文件 SHA-256、大小、mode、uid/gid 与目录清单。拒绝链接和特殊文件；复制前后不一致即失败。
6. 在独立 internal 网络、无宿主端口的临时 PostgreSQL 中恢复 dump；所有表完整行哈希、输出上限须与备份一致。0009 不执行迁移；0008 才执行核定迁移并检查旧字段不变。
7. Windows 文件复制到私有 `restore-tests`，Linux tar 解包到独立 `restore-tests`；重新比对清单，对恢复的 controller SQLite 执行 `quick_check`。用恢复副本中的主密钥确认原凭据可解密，只记录数量。
8. 写入绑定固定候选镜像及 manifest 哈希的 `verification.json`。成功才恢复原服务；失败保留停止状态和证据，等待明确恢复处理。

dump、归档及秘密目录包含完整私有状态，不能发布到 Git。数据库恢复容器和网络仅按本次随机名称及专用标签清理；文件恢复副本保留于私有 `restore-tests`，便于核查。后续清理只能选择确认不用的具体副本，不能删除生产根目录。

普通 `backup` 会拒绝运行中的 0009 服务。0008 旧只读备份入口保留；正式切换总会重新取得停止状态的最新备份。

## 正式更新

```powershell
.\.venv\Scripts\python.exe scripts/m4_production_deploy.py update --backup $backup --image $release
```

脚本要求已验证备份、当前 schema、helper 和固定镜像匹配，再次停止写入方、取得新备份并独立验证。通过后才激活 staged 配置并启动服务，最后核对完整数据库清单；结果与最新备份记入 `deployment.json`。

部署不修改模型 API、价格、模型列表或 100K 主输出。经批准的模型策略版本更改单独记录。失败不会自动换回旧程序读取当前数据，应检查证据后选择显式恢复。

0008 首次迁移使用相同准备、备份和验证，最后执行 `cutover --backup $backup --image $release`。上线后仍须检查 8787 页面及真实工具、审批、摘要链路，独立恢复预演不能代替上线验收。

## 显式回退

```powershell
.\.venv\Scripts\python.exe scripts/m4_production_deploy.py rollback --backup $backup
```

回退先检查归属与静止状态，并为当前新版本创建完整备份；将所选备份恢复到**新命名数据库**及新私有 data/secrets/native 目录，使用备份记录的固定旧镜像。原数据库不删除。

完整 0009 备份的 controller 和 snapshots 先独立恢复、比对，再将原 Linux 状态移入受控根下的 `retired/<timestamp>`，恢复副本放回固定挂载路径。固定 helper、controller 配置及密钥一并恢复，broker 指向恢复的 native 和密钥目录。0008 备份恢复旧应用与数据库，工具关闭。

0009 回退的工具镜像读取所选备份中由 manifest 校验的 `config/deployment.env` 的 `OMNI_SANDBOX_IMAGE`，不使用当前脚本的工具镜像常量。该项必须唯一、为完整不可变 ID 且本机镜像存在；缺失、可变 tag、重复配置或镜像不可用时，在停服务前拒绝回退。

Linux 移动检查解析后的路径位于本实例根内。回退后比较数据库清单，并报告保留的新数据库和新状态备份。项目文件差异仍由框架逐项核对，发行脚本不自动覆盖。

## 日常启停与验证范围

`scripts/start.ps1 -StartEngine` 读取持久化 WSL 后端与固定镜像，不重建当前工作区。`scripts/stop.ps1` 加 `m4_production_deploy.py broker-stop` 停止应用及本实例 helper。broker 按解释器、profile、来源和端口匹配，不按 Python 进程名批量结束。

本轮由 `tests/test_m4_release.py` 离线验证停机次序、竞态拒绝、未知工具、归档穿越/链接、文件篡改、同 schema 路由、固定镜像与 staged 配置。

2026-11-09 03:31:11 UTC，实际 `backup-verify` 已通过：备份目录 `.omniharness/m4-production/backups/20261109T033040-fa8437`，`verification.json` 记录候选 `sha256:195a1ad4c3467de5165e7df55376ad146bc3d2ba80ebf35c9161cb954da93914` 对 schema 0009 的独立恢复结果。30 张业务/检查点表完整一致，M4 文件恢复通过，25 份凭据可解密，25 个模型的主输出仍为 100K。备份内 `config/deployment.env` 记录当时的旧工具镜像 `sha256:95ece64f0e9b4196a12fcc4c5098664bc5af993bd0ddd14e9957f29bf0857d9e`；原验证文件保持不变。

验证后原 8787 服务已恢复，health 为 200/database ok；API/Worker 仍为原 `a596d610…` 镜像，数据库与备份完整清单相同，保留 25 模型、3 会话，活动 Run/未知操作/工具占用为 0。本轮尚未执行生产 `update`，不能把独立恢复通过当作新版本已上线。
