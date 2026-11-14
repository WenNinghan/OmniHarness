# Docker、PostgreSQL 与 M3 前置检查记录

日期：2026-11-05。用户要求先完成 Docker 和 PostgreSQL 的环境配置与实测，再实施 M3；本记录不将尚未运行的容器验收标为通过。

## 2026-11-08 补充：隔离 Docker 与 Compose 实测

在 G 盘专用 WSL2 发行版 `OmniHarnessCheckAlt` 中安装 Docker Engine 28.3.3 与 Compose 2.36.2；WSL 发行版的虚拟磁盘与 Docker 数据位于 `.omniharness/wsl-check-alt`。Docker 调用方式为 `wsl -d OmniHarnessCheckAlt -u root -- docker ...`。Windows 原生 Docker CLI/Desktop 和 PowerShell 部署脚本尚未验证。

使用 `.omniharness/docker-qa` 中隔离副本、独立 Compose 项目和端口 18787 完成以下真实验证：

- `hello-world` 容器运行成功；Windows 宿主机的中文和空格路径通过容器读写并从 Windows 读回。
- 应用镜像构建成功；PostgreSQL healthy、init 退出码 0、迁移版本 0003、API healthy、Worker 运行，Windows 宿主 `/health` 返回 200，database=ok。
- 以明确标记的 Mock 完成 API → PostgreSQL → Worker → SSE 聊天；重启 QA API/Worker 后原消息与 Run 保留；重启 QA PostgreSQL 后健康检查恢复。
- 本轮原生 Windows PostgreSQL 集成测试再次 **4 passed**，独立检查点探针在新 Python 进程恢复结果 42；本次探针未在开始与恢复之间重启数据库。Docker Compose 未测试真实 Provider。

网络边界：项目原始 Dockerfile 中的官方 npm 源当时长时间停滞；仅 QA 副本临时使用 npm 镜像源，锁文件完整性校验保留。PostgreSQL、Node、Python、uv 基础镜像按与 Docker Hub/GHCR 官方 OCI index 完全相同的摘要从镜像源拉取。**因此隔离 Compose 实测通过，但不能声称原始联网构建设置或 Windows Docker Desktop 部署已通过。** 测试未修改项目原 Dockerfile、系统代理或防火墙。

QA 服务当前使用独立卷和秘密，未覆盖现有聊天数据；临时网络中继已停止。`dockerd` 在专用 WSL2 环境运行，日志存放 `.omniharness/wsl-dockerd.out.log` 与 `.omniharness/wsl-dockerd.err.log`。

## PostgreSQL：Windows 原生隔离测试已通过

环境子任务从 PostgreSQL 官方 Windows 下载页面指向的 EDB PostgreSQL 17.11 官方二进制包按需提取所需组件到 `.omniharness/pg17/`，没有安装系统服务。独立实例数据在 `.omniharness/pg17-test-data/`，仅监听 `127.0.0.1:55432`；测试数据库为 `omniharness_test`，随机密码保存在 `.omniharness/secrets/pg17-test-password`。这些目录均被 Git 忽略，不用于生产业务数据。

- 设置专用测试数据库的 `OMNI_TEST_POSTGRES_URL` 后，执行 `python -m pytest tests/test_postgres.py -q`：**3 passed**。覆盖迁移重复执行与数据保留、跨连接池 Worker 锁互斥、真实数据库上 API → Worker → SSE 游标恢复。
- 执行 `.omniharness/pg_checkpoint_probe.py`：官方 `AsyncPostgresSaver` 在中断时写入检查点；实际停止并启动 PostgreSQL 后，新 Python 进程以 `Command(resume=21)` 恢复，得到结果 42。
- Windows 上默认 Proactor 事件循环与 psycopg 异步连接不兼容。探针使用 `WindowsSelectorEventLoopPolicy` 才通过；M3 正式实现仍需针对 Windows 与 Docker Linux 分别验证。
- 这是原生 Windows PostgreSQL 实测，不等于 Compose 里的 PostgreSQL 和网络链路已通过。

## Docker：尚未完成

系统具有 WSL 2.7.11.0，`WSLService` 和 `vmcompute` 显示运行中。系统盘 C 可用约 1.44 GiB，G 盘约 115 GiB。Docker Desktop 官方安装器约 595 MiB，默认安装和数据位置会继续消耗 C 盘；本轮没有向 C 盘安装。

环境子任务下载并校验官方 Alpine rootfs SHA-256，在 G 盘尝试通过 `wsl --import` 注册 WSL 1 和 WSL 2 发行版；gzip 与普通 tar 均返回 `Wsl/Service/ERROR_UNHANDLED_EXCEPTION`，`wsl --list --verbose` 仍显示没有已注册发行版。错误原因尚未定位；不能据此断言是虚拟化、网络或磁盘故障。测试文件保留在 `.omniharness/`，没有删除用户数据。

因此 Docker Engine、镜像构建、Compose 服务、容器中文路径挂载与容器 PostgreSQL 均**未实测**。当前进程不是管理员，查询 Windows 可选组件状态返回需要提升；后续需在系统盘留有足够空间后，通过管理员权限诊断 WSL，再继续 Docker 安装和实测。参考 [Docker 官方 Windows 安装文档](https://docs.docker.com/desktop/setup/install/windows-install/) 和 [Microsoft WSL 问题排查中的日志收集说明](https://github.com/microsoft/WSL/issues/12695)。

## M3 状态

已整理 M3 实现前设计。按用户指定顺序，Docker 实测完成前未开始 M3 代码修改；现有 SQLite 聊天开发服务保持不变。
