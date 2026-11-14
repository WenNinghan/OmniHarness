# 独立沙箱创建与回收服务

2026-11-14 源码实现，后续由 Sol 完成关键路径及成对快照/离线恢复的隔离验收，**未部署到 8787**。最新维护能力和验证见 快照与恢复核验，前轮完整服务范围见 Sol 验收结果。完整设计见 `.ai-governance/pre-implementation-superagent-code-20261114.md`。普通 `compose.yaml` 不自动启用此服务。

## 请求路径

新 Run 在 `freeze_workspace` 固定 `creation_service={url,service_id}`。Worker 保存工具预约和槽位后向内部服务请求创建；服务在业务库行锁内核验运行、工作区、快照、审批和账本，依次写独立 witness、journal、引擎 marker，再请求 OpenSandbox 创建。创建回执写入两份记录后才返回 ID。Worker 保存 ID，连接 SDK，重新检查授权后执行原命令。

独立服务只负责创建、查询和回收。命令执行、完整日志、预算和模型调用仍复用现有 Worker。未知派发不自动重试；恢复后的 DISPATCHED 不能取得新的进程内派发权。

命令结束或失败时通过服务核验回收：完整 CREATED 绑定、控制器 404、引擎中沙箱和侧车均缺失且 marker 完整，缺一项则保留不确定状态。控制器先返回 404 而引擎仍在清理时，只在已配置的有限次数内继续只读核验；不再因第一次 False 就放弃，也不提前释放。回调抛异常仍拒绝，取消传播。重复回收允许核验“已经删除”，但从不声称命令成功。Worker 失联后可用原有认证核对 API 退休终态操作；它执行两次业务绑定复核并仍将命令记为 unknown。

旧 Run 没有冻结此服务，不能套用新凭证。2026-11-12 的旧未知操作已于 2026-11-14 通过独立的已关闭内核/引擎周期和受控维护证据退休，槽/租约释放，命令仍 unknown/UNVERIFIED，没有补写沙箱 ID；见 维护验收。这不表示创建服务已部署，也不能将该一次性维护证据套用于仍在原执行周期中的其他未知操作。

## 持久性边界

- journal 与 witness 使用不同身份、互不嵌套的专用目录；witness 必须在应用备份、Docker data-root、容器可写层及沙箱挂载目录之外。Compose 的两个 bind source **必须在部署时检查解析后的实际宿主路径及挂载关系**，容器内看到不同路径本身不能证明宿主存储独立。
- 文件缺失、身份不符、两边不一致、声明/回执部分写入均停止派发或回收，不自动修复。优先保留一次未知占位，避免重复副作用。
- 服务启动和每次访问都按主日志→witness 顺序持有双库写锁，流式核对全部记录；历史操作的单侧回滚也会阻止不同 ID 的新派发。提交间崩溃可留下不一致，后续拒绝而非补写。核对时间随历史规模增长，尚无生产规模性能验收。
- 单独回滚应用/创建日志、单独回滚 witness、或仅清理 Docker marker，不能重新取得已有操作的派发权。marker 被删也会阻止回收证明；这是保守可用性代价。
- **不抵御 journal、witness、Docker 三者协调回滚或管理员篡改**，也尚未验证断电耐久性。全盘恢复须先停调度，隔离全部旧创建执行者，清点资源并人工核对；不能用相同身份初始化空记录后继续旧 Run。
- 业务事务和两份 SQLite 事务不构成分布式事务；服务故障可能留下永久未知，不能用自动重试抹平。

## 配置与显式初始化

服务运行在 Linux/WSL。需要现有的 PostgreSQL、已固定的 OpenSandbox 镜像/控制器，以及仅服务可写的存储。路径父目录需服务用户所有且 0700，数据库文件 0600，祖先目录不得共享可写；拒绝链接及多硬链接。

API/Worker 配置：`OMNI_SANDBOX_CREATION_URL`、`OMNI_SANDBOX_CREATION_SERVICE_ID`、`OMNI_SANDBOX_CREATION_KEY_FILE`。三项要完整配置。URL 和身份固定进新 Run；恢复时不允许改用另一个服务，也不在服务错误时降级直连。

服务另需：

| 环境变量 | 用途 |
|---|---|
| `OMNI_CREATION_JOURNAL_PATH` / `JOURNAL_ID` | 主记录文件及随机 64 位十六进制身份 |
| `OMNI_CREATION_WITNESS_PATH` / `WITNESS_ID` | 独立见证文件及不同的随机 64 位十六进制身份 |
| `OMNI_CREATION_ENGINE_ID` | 预先核实的 Docker engine ID |
| `OMNI_CREATION_MARKER_IMAGE` | 已存在的本地 `sha256:` 镜像 ID |
| `OMNI_CREATION_SOCKET_PATH` | 默认为 `/var/run/docker.sock` |
| `OMNI_CREATION_HOST` / `PORT` | 独立进程默认 `127.0.0.1:19083`；容器只暴露内部网络 |

新环境先配置上述存储身份及与 Worker 一致的 instance/controller/image/snapshot_root。以下为配置步骤；初始化和服务运行已在专用隔离实例验证，生产尚未启用：

1. `python -m omniharness.sandbox_creation_service identity` 输出非秘密服务身份；写入三方相同的 `OMNI_SANDBOX_CREATION_SERVICE_ID`。
2. `python -m omniharness.sandbox_creation_service initialize` 仅可初始化不存在的两份存储。部分初始化失败时调查，不能靠删除既有文件“重试”。
3. `python -m omniharness.sandbox_creation_service serve` 装配独立服务，不启动 Worker、不迁移业务库。

生产 Compose 可在受控部署时显式加入 `deploy/creation-service.compose.yaml`；已用占位配置通过静态解析，未按 overlay 部署。它不映射宿主端口，只有创建服务挂 Docker socket；API/Worker 不挂 socket。必须提供实际 socket group、独立宿主目录和服务密钥。计算初始身份时可在未启动任何服务的独立进程内执行 `identity`；Compose overlay 的身份变量为必填，初次计算不可因这一点启动 Worker。

现有 `m4_production_deploy.py` 的备份/恢复流程**没有自动涵盖新增两份存储和 overlay**。旧发布工具的拒绝条件保留：查询到创建服务配置或已冻结该服务的 Run 时，在停止服务/准备升级之前拒绝；带新增外部存储的备份也不能套用旧恢复流程。原 ownership 校验同时会拒绝未识别的 Compose overlay。新增的独立快照命令不解除这一拦截：发布还要将 PG、broker、controller、双记录和执行者隔离纳入统一维护协议。当前运行实例配置没有改变。

## 成对快照与离线恢复核验

运维入口复用同一模块，不增加管理 HTTP API。生产服务的只读容器没有额外备份卷；维护时需要由工具显式挂载独立私有备份目录，使用原服务 UID、存储/服务/引擎身份及配置。目录不得落在 journal/witness 内，POSIX 父目录须为当前用户私有。示例中的 `/creation-backups` 代表另行准备的持久卷，不能用容器临时目录冒充持久备份。

```text
python -m omniharness.sandbox_creation_service checkpoint --directory /creation-backups/cp-001
python -m omniharness.sandbox_creation_service verify-checkpoint --directory /creation-backups/cp-001
python -m omniharness.sandbox_creation_service rehearse-checkpoint --directory /creation-backups/cp-001 --target /creation-rehearsals/test-001
python -m omniharness.sandbox_creation_service verify-retained --directory /creation-backups/cp-001
```

- `checkpoint`：双库固定顺序写锁期间，用独立只读连接调用 SQLite backup，核对两份完整历史，fsync 后最后写清单。目标必须全新，失败保留部分文件而不复用。快照只覆盖双记录，不与业务 PG 构成全局原子备份；在途 DISPATCHED 即使进入快照也仍是未知。
- `verify-checkpoint`：只读验证精确文件集合、SHA-256、SQLite 完整性、固定身份及完整历史。拒绝残留 WAL、链接、硬链接及不私有文件。SHA-256 是完整性检查，不能抵御同时篡改清单和数据的管理员。
- `rehearse-checkpoint`：在全新目录恢复两份**离线副本**，逐条检查原事务声明方法仍拒绝已有 claim。既有 DISPATCHED 保留未知。批量检查共享一次双库事务，避免每条操作重新全扫描历史；流式复制/读取，不把整份数据库读入内存。它不启动服务、不连接 SDK、不改现场记录、不释放槽，恢复副本不能直接接回生产服务。
- `verify-retained`：比较备份与现场的服务/引擎/store 身份，并要求每条备份历史保留在当前一致双库中；允许新增历史及 DISPATCHED→CREATED，拒绝丢失、降级和更换绑定/沙箱 ID。业务回退应保留较新的现场见证，不能把两份旧快照同时覆盖回来。此核验只是必要条件，不证明旧执行者已关闭或外部资源已回收。

离线验证/演练不加载生产 Settings 或密钥；checkpoint/verify-retained 会验证当前存储路径和冻结身份。全盘丢失、两份记录不一致或两边协调回滚仍需独立恢复方案，没有自动合并或修复。最新真实隔离验收包含服务创建 4 条回执、成对快照/独立恢复、4 次重复 claim 拒绝、保留现场历史、重启回执不变和 HTTP 重复 create 拒绝；DISPATCHED 未知保留另经单元及 Linux 离线检查验证。

## 接口

内部接口均要求 `X-Creation-Service-Key` 与 `X-Creation-Service-ID`。服务绑定运行、操作指纹、快照、超时；只在创建时传递 execd token，状态和回收请求不需要它，响应不包含凭据。

- `POST /internal/create`：一次性声明并创建；已有请求返回冲突，失败/超时视为未知。
- `POST /internal/status`：查询，不能凭 DISPATCHED 推断不存在。
- `POST /internal/reclaim`：核验并回收已创建资源；不执行原命令、不改工具槽。
- 原认证 `/api/v1/tool-operations/{id}/reconcile`：仅终态 Run、明确 ack、完整回收证明、稳定业务绑定后退休未知操作并释放槽/租约。
- 新认证 `GET /api/v1/runs/{id}/observation`：一致快照内读取任务、资源、输入、命令及缓存状态，不改变任务。

## 运行验收顺序

先运行本轮新增的双记录/创建客户端/授权/服务 API/执行适配器/核对/状态/请求清单测试，再相关既有回归和前端测试、构建。接着独立 PostgreSQL 锁竞态、Worker/创建服务各自强杀、真实创建响应丢失、标记清理/单侧存储恢复、真实回收与占位测试。单元测试替身不等于这些真实环境检查。最后才处理旧记录、完成部署，并按预先固定的同模型/同输出/同输入预算条件跑 V1/V3 中等题。
