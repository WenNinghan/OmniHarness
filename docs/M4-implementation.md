# M4 实施记录

状态（2026-11-09）：**M4-A—E 已实施并部署到本机8787。真实代码验收6/6通过、流程完成5/6；一次摘要触限保留为失败。** 用户已明确批准 G2 方案，同意原项目受控补丁、命令与测试只运行过滤快照。操作与权限说明见 [工具和上下文](M4-tools-context.md)。这不是完整v0.1/M7发行验收。

## 已完成的基础实现

- `workspace_io.py` / `workspace_broker.py`：固定授权版本、Windows 原生文件身份与路径检查、读取/搜索、单文件补丁、预期摘要冲突检查、恢复材料和未知结果核对；独立服务凭据与实例鉴权；没有模型可用的宿主命令入口。
- `sandbox.py`：OpenSandbox SDK 1.2.0 薄适配，固定镜像和快照范围、关闭遥测/隐式重试、受限命令参数、完整日志回调、有限预览、先保存沙箱身份后派发、取消后确认删除。
- `sandbox_stream.py`：复用 SDK 类型和 HTTP response hook，拒绝损坏/截断/超限 SSE，避免上游跳过错误数据后误报完整结果；HTTP 错误保留状态码，不记录错误正文。
- 工具运行镜像、独立 Controller 配置和 M4-A 可重复探针；M4-A 阶段没有修改生产数据库。随后已准备独立候选环境与受控生产升级脚本。

本机依赖新增 `opensandbox==1.2.0`；现有 Deep Agents 0.7.32、LangChain 1.4.9、LangGraph 1.2.20 保持。WSL 专用发行版内安装 Python 3.12.16，数据仍位于 G 盘的发行版 VHD。Windows 原生 helper 当前使用既有项目 Python 环境，不代表 M7 免 Python 发行包已经完成。

复用范围、固定版本和本次相关许可证见 [M4 来源清单](M4-reuse-and-licenses.md)。

## 目前实测结果

| 层次 | 实际验证 | 当前结论 |
|---|---|---|
| SDK 薄适配 | `pytest tests/test_m4_sandbox_adapter.py -q` | 21 passed；离线契约，包括取消、回收、固定 profile 与长操作 ID 标签编码，不能替代真实隔离测试 |
| SSE 协议 | `pytest tests/test_sandbox_stream.py -q` | 36 passed；真实 SDK + MockTransport，覆盖完整/损坏/超限/原样中文换行/HTTP错误脱敏 |
| 工作区 | 设置 `OMNI_M4_WSL_DISTRO=OmniHarnessCheckAlt` 后执行 `pytest tests/test_m4_workspaces.py -q` | 32 passed / 1 skipped；跳过 Windows symlink 创建权限，junction/hardlink/祖先句柄/并发冲突已实测 |
| 原目录与快照 | WSL -> 固定 Windows helper -> 中文空格 NTFS 目录 -> WSL ext4 | 实际读取、补丁、恢复与过滤快照通过；修改快照后原文件不变 |
| 不同盘符 | 在 F 盘独立随机样例中执行 Windows/WSL 同对象与 NativeWorkspaceIO 两项测试 | 2 passed；与之前 G 盘样例分别实测；第一次测试目录父路径未建立的 setup 错误修正后通过 |
| 原版 OpenSandbox Docker | 独立 Controller 19080、固定 BOM 镜像、非 root 样例 | Controller 鉴权、资源限制、仅快照挂载、通常状态下断网、日志和超时通过；以下阻塞使整体不通过 |
| M4 本地适配变体 | `OMNI_M4_SANDBOX_LIVE=1` 下运行 `pytest tests/test_m4_sandbox.py -q` | **22 passed in 50.87s**；真实 Docker、原生 SDK 和应用适配器，包含 11 项基线、6 项认证/挂载、3 项网络故障/回收、2 项应用 SDK |

以上 M4-A 样例与控制服务独立于 `omniharness-api-1` / Worker / 数据库，M4-A 探针没有调用付费模型。后续真实 Provider 验收另记录在下方。

## M4-A 找到的阻塞与处理

1. Docker runtime 对 `secure_access=True` 返回 400，此选项只支持 Kubernetes。不能将这个选项当作 Docker 已有认证。
2. 原版无 token 的 execd 可从同 bridge 访问。加 `EXECD_ACCESS_TOKEN` 后无 token 请求为 401，但工具进程可获得 token，且未鉴权 `/internal/init` 能重设认证。UID 0 请求被拒绝，未观察到宿主越权或提 root；准确问题是管理凭据/认证边界未成立。
3. 正常 default-deny 下网络探测拒绝；主动停止 egress 侧车后，18 次对自有 canary 的探测有 3 次成功，存在短暂 fail-open。未访问外部敏感目标。
4. SDK `kill()` 只承诺请求被接受；真实本机探测在返回后验证 get_info 404、主/侧车容器与原宿主 PID 消失。适配层等待确认后才允许归还执行位置。

已完成基于固定 BOM 源码 `25873227f60424bdc59bfa83b963bbc00e871297` 的小补丁变体：execd 禁止内部重初始化与非 M4 入口、服务器端强制 UID/GID、复用上游 hardener、隔离 supervisor 凭据；egress 保留 deny 与 DNS 规则直到共享网络命名空间销毁。上游原生 launcher 在部分降权/seccomp 失败时只记录错误后继续执行的路径，也改为立即停止并核验真实子进程权限。补丁、构建文件与 Apache-2.0 归属见 [本地适配说明](../deploy/patches/README.md)。原版失败报告保留 `upstream-` 前缀，不覆盖成成功。

实际工具进程为 UID/GID 10001，五组 capability 全部为零、NoNewPrivs=1、Seccomp=2，内核报告两个过滤器；可信 execd 为 PID 1，仅持有 SETUID、SETGID、SETPCAP、KILL 四项能力。工具既读不到 daemon token 的环境/进程数据，也不能改写 runtime 或硬化配置；没有 Docker socket、宿主盘或其他项目挂载。非法宿主目录及指向其的快照 symlink 均被拒绝。UID 0 请求被精确身份校验拒绝，执行标记没有生成；该拒绝目前沿用上游 `RUNTIME_ERROR` 的 HTTP 500，不将任意 500 视作验收通过。

网络故障复测要求侧车确实退出、工作负载仍运行，并持续观察退出后的连接尝试；故障前后从独立位置确认 canary HTTP 200。适配后退出期间和退出后均无连接成功，消除了本机原版复现的短暂放开窗口。删除验收同时检查 API 404、主容器、侧车及原宿主 PID，不只看 SDK 删除请求已接受。

应用 `SandboxExecutor` 已完成真实 Python 调用 Node.js、中文/空行原样日志、回调先落身份再派发，以及启动子进程后的取消与完全回收。实际接线中另修正固定 44772 self-proxy 的 Gin 流式状态，以及严格 SSE 校验对官方 `ping` 事件的兼容；继续拒绝其他端口代理、损坏/截断流和缺少终态的结果。

SDK 薄适配已改用独立 execd 原生认证，并强制验证同 Controller 代理下的 `/omni/profile`；这不是忽略 Kubernetes-only 参数错误后继续运行。未修补的原版服务缺少此 profile，会在派发命令前被拒绝。

调查与原始结果位于 `.omniharness/m4a/`：`last-report.json`、`native-auth-report.json`、`fault-report.json`、`adapter-report.json`、`pytest-live.xml` 与 `deployment.json`。报告包含固定样例输出、状态和布尔结论，不保存测试 token。

本机已验证的内容地址为：

| 镜像 | 本地固定 image ID |
|---|---|
| execd M4.3 | `sha256:6b5a76c64ef66f500d38e068b1a50d68eb2f7fbc6d18cddbce525d86faeca0e1` |
| egress M4-A.1 | `sha256:cad71a020c0990470d4ebe12eb01fc4e6e12afb431d67ae6b655626f7539b8e3` |
| Python / Node 工具运行时 | `sha256:95ece64f0e9b4196a12fcc4c5098664bc5af993bd0ddd14e9957f29bf0857d9e` |

这次结论针对 Windows 上专用 WSL Docker 环境和过滤后的 ext4 快照；原目录的受控写入由独立 broker 承担。未安装 gVisor，也不宣称普通 Docker 共享内核等同虚拟机隔离、所有 Linux/Windows 环境已通过或 M7 发行验收已完成。

## 分阶段验收顺序

M4-A 门禁通过后，依次接入产品数据库、工具执行子图、持久审批与恢复、上下文和 UI。独立候选完成验收后才切换生产；M4-A探针没有直接修改原生产容器和数据库。真实模型请求前告知具体模型与请求地址；单 Agent / 多 Agent 的效果对照使用相同工具和独立验收条件。

## M4-B—D 产品接线与验证

- 迁移 `0009_tools_workspaces_context` 保存授权、工具操作、审批、快照和工件。API 只接收命令；Worker 驱动原 LangGraph，工具循环复用 LangChain `create_agent`，没有另起调度器。
- v3 任务继承五阶段模型选择，工作区授权、模型与上下文策略随 Run 冻结。旧 v1/v2 任务保留原路径，不自动获得工具权限。新增 single 执行方式供同工具公平基线。
- 具体审批绑定操作与参数，等待阶段退出执行位；在途并发操作未收尾时不提前释放。恢复复用已保存工具结果，未知不重放。终态未知工具只有匹配原服务证据才能核对释放，仍标记 `UNVERIFIED`。
- 摘要复用上游 SummarizationMiddleware，但模型调用、来源工件与用量经过同一持久账本。当前目标、完整工具交换和当前阶段指令受保护；原聊天不覆盖。大工具结果保存完整工件，模型与用户都可分页读取。
- MCP 2.1.0 使用明确注册的 Streamable HTTP 工具。真实官方 SDK 本地服务已验证远端 schema、精确审批、重建图恢复、拒绝、重复批准和返回丢失不重试。

| 验证 | 已观测结果 | 证据 |
|---|---|---|
| 最终完整离线回归 | 544 passed / 113 skipped / 41 pytest-asyncio弃用提醒，264秒；Ruff通过 | `.omniharness/m4-preview/offline-release.xml` |
| 实际 LangGraph/Agent 集成 | 12 passed；模型和工具容器在此组明确为测试替身；覆盖最新工具结果顺序与补充要求仅处理一次 | `tests/test_m4_runtime.py` |
| PostgreSQL 最终专项 | 25 passed；摘要18＋运行恢复4＋资源互斥2＋并发核对1 | `.omniharness/m4-preview/postgres-final.xml` |
| MCP 官方 SDK/HTTP | 6 契约测试＋3 实际图/审批/HTTP 测试通过 | `test_m4_mcp.py`、`test_m4_mcp_runtime.py`、`mcp-runtime-tests.xml` |
| 原目录与核对 | 35 passed / 3 环境 skip；核对接口19 passed | `test_m4_workspaces.py`、`test_m4_reconciliation.py` |
| 普通聊天上下文 | 2 passed；摘要计入总用量，取消时不启动主请求、未知摘要保留 | `test_chat_context_integration.py` |
| 浏览器登记 | 桌面/390px窄屏实测；修复弹窗叠层与留白 | `.omniharness/m4-preview/ui/report.json` |
| 前端契约/构建 | 20 passed；TypeScript/Vite 构建通过，仍有大于500KB的bundle提示 | `npm --prefix web test`、`npm --prefix web run build` |
| 实际 OpenAPI | 从候选鉴权接口导出 OpenAPI 3.1.0，共42条路径；工作区/审批/工件/核对接口存在 | `.omniharness/m4-preview/openapi-final.json` |

这些组之间可能重叠，不能相加作为独立测试总数。未配置环境的测试明确跳过，不算通过。

## 真实模型验收中发现的问题

使用已向用户告知的 `deepseek-flash`、`deepseek-v4-pro`，请求地址 `https://e-flowcode.cc/v1/chat/completions`。生产配置与历史没有被候选测试覆盖。

1. Pro 首次工具探测被接口拒绝：兼容适配器强制 `tool_choice=required` 与该接口思考模式不兼容。改为和实际执行一致的 `auto` 后，仍严格检查真实调用、19+23工具结果及模型读回42，完整往返通过。输出配置保持100000。首次错误与成功探测分别保存，不覆盖。
2. 第一条混合模型 TypeScript 任务在 dispatch 阶段停止：模型正常结束，但公开正文包含三个阶段，不能作为单个合法任务 JSON。发现上下文 helper 将旧阶段指令移到末尾，修正为保留历史问答顺序、仅保护当前阶段，并附上经过校验的计划。严格解析保留，没有猜取最后一段 JSON。首次失败记录保留在 `m4-eval-d21341f78b25443b`。
3. 人为设置很小的上下文烟测验证了拒绝行为：400摘要输出无法完整保留结果，5000输入目标在递进摘要时容纳不了提示、前摘要与下个完整消息，分别以 `SUMMARY_INCOMPLETE` / `CONTEXT_GROUP_LIMIT` 停止。原消息和已取得的摘要证据保留。此为配置边界测试，不算摘要成功。
4. 第二条 TypeScript 任务的供应商报告摘要输出2613 tokens，字符估算却超过4000，导致误判。摘要输出现在优先使用有效的供应商 `output_tokens`；缺失时才明确使用估算。下一次请求的完整输入检查仍保留，不借此放宽输入限额。失败批次 `m4-eval-d34721f1aa8947cb` 保留。
5. 官方 Controller 的标签值最多63字符，原64位操作指纹使命令在创建前被拒绝。改为无损分段编码长标识，保持操作 ID 不变；实际64位 ID、Python/Node日志及取消回收复测通过。原未知操作保留原状态和单独的测试夹具退役记录，未放宽产品对缺少沙箱 ID 的核对规则。
6. 第三条 TypeScript 任务已通过隐藏验收，但后续重复读取和测试直到上下文超限。根因是同一任务说明每轮都被移到最新工具结果之后。修复为任务说明在执行历史前部保留一次，新补充要求仅首次作为末尾新输入；后续最新工具结果保持在末尾。94项上下文测试、12项图运行集成和4项真实 PostgreSQL 恢复测试通过。失败批次 `m4-eval-d3cedbdc7ef649be` 保留。

上述三轮评测失败合计41次模型调用、30次工具调用、193371已知 tokens，实际费用未知；见 `.omniharness/m4-evaluation/failed-trial-ledger.json`。它不包含连接探测、审批和普通聊天烟测费用，不与最终批次混为一次成功执行。

## 已完成的真实审批与上下文验证

- Flash Run `937e3c1d-c7ef-47da-971b-fc58aea23f41` 在未批准时没有派发命令；Worker 重启后保留同一审批 ID 和指纹。浏览器批准后实际容器输出 `M4_APPROVAL_OK`、退出码0。重复批准没有重复执行，沙箱已回收。证据：`.omniharness/m4-preview/approval-ui-fixed/state.json` 与 `completed-artifact.png`。
- 普通聊天使用独立 Flash 配置做真实长历史验收：7000输入目标、4000摘要输出，主输出仍100000。六轮聊天产生5份摘要，代号、验收数字、交付形式、禁止动作和工期全部正确回忆，原始消息没有被改写。证据：`.omniharness/m4-preview/context-smoke-balanced/extended-report.json`。这些测试配置只在隔离候选环境，原25个模型未修改。
- 工件按 UTF-8 字节游标分页，新增中文与 emoji 的1/2/4/7字节小页回归，拼接后与完整原文相同；拒绝从字符中间开始的游标，不以替换字符损坏日志。

独立 Python/TypeScript 验收器已用固定坏源码和参考实现校准：各自 FAIL/PASS 均符合预期，测试代码仅在独立快照容器执行。

## M4-E 六组真实效果对照

最终 build7 六组已完成收证：**5/6 个 Run 完整交付，6/6 份代码通过隐藏验收，固定配置比较通过。** 共77次模型调用（含15次摘要）、67次工具调用、325666已知 tokens，估算费用和实扣费用均未知。批次为 `.omniharness/m4-evaluation/results/m4-eval-932a475d164a4c0e/`，完整矩阵见 [评测结果](M4-evaluation.md)。

Python 单 Agent 一组实际代码已正确，但第2次摘要返回 `finish_reason=length`：真实4000输出 tokens 中2987为推理用量，正文没有完整结束。输入估算10014低于16000目标；不是先前的字符估算误判。框架按既定策略以 `SUMMARY_INCOMPLETE` 失败，原代码与证据保留，没有重试或扩大预算。

TypeScript 单 Agent约57秒，同模型五阶段约191秒，多模型五阶段约176秒；Python两组五阶段约194秒、216秒。失败终态耗时不能当作成功交付速度。当前小任务没有证明多模型整体更快或更省，后续优化应针对任务粒度、信息交接和摘要开销；不因为流程增加了Reviewer就宣称质量提升。

## 原8787部署与上线实测

- 固定应用镜像 `sha256:a596d6108779a275829c22abf2ef0516531634d317db909f7777becba758cc8d`；生产数据库由0008升级至0009。原地址健康、Agent与工具就绪；生产Controller/Broker分别使用19096/19098，与候选隔离。
- 停机前预演及停机后最新备份均在独立PostgreSQL中恢复并迁移验证。最新备份为 `.omniharness/m4-production/backups/20261108T174436-50b318`。原25模型、100K上限、价格、2个会话/24条消息和12个Run核对一致；没有用候选测试数据覆盖生产。
- 默认启动读取 `.omniharness/deployment.env` 并复用固定镜像，不重新构建未发布的全局样式。恢复与边界见 [本机部署](m4-deployment.md)。未对原实例执行破坏性回滚演练，不能将恢复预演称为完整运行期备份验收。
- 正式服务烟测 Run `ed96d5b6-2e34-4930-8e13-2fed77e8cc11`：实际Flash、3次模型调用、2次工具，先创建原项目 `result.txt`，再在快照中断言其内容及 `.env` 已排除。命令退出0并输出 `M4_PRODUCTION_OK`；快照专有文件未回灌，原合成凭据标记保留，25模型配置前后逐值相同。烟测增加自己的会话和工件，不修改既有历史。
- 实际8787浏览器展示工具结果及 `stdout.log`，0个JavaScript错误。烟测完成后仅对原 `deepseek-v4-pro` 再做正常工具能力探测，真实工具往返通过；只更新该模型的 `capabilities/tested_at`，其他模型、价格、连接参数与全体100K保持。证据 `smoke/pro-tools-probe.json`、`production-tools.png`、`production-stdout.png`，截图已目视核对。
- 证据：`.omniharness/m4-production/post-cutover-readiness.json`、`deployment.json`、`smoke/state.json`。没有自动提交、推送或部署用户工作区。
