# M4 复用与来源

本表记录本次实际接入的执行底座和新增依赖。版本及 Python 包许可证取自本机已安装分发元数据和 `uv.lock`；不是所有传递依赖、基础镜像及操作系统包的完整发行清单，M7 仍须汇总发行材料。

| 组件 | 固定版本 | 许可证 | 用途 |
|---|---|---|---|
| Deep Agents | 0.7.32 | MIT | 保留原有 Agent 能力与兼容路径 |
| LangChain | 1.4.9 | MIT | `create_agent` 工具循环、摘要中间件 |
| LangGraph | 1.2.20 | MIT | 五阶段图、执行子图、中断与恢复 |
| PostgreSQL Checkpointer | 3.2.0 | MIT | 持久图检查点 |
| OpenSandbox Python SDK | 1.2.0 | Apache-2.0 | 命令、日志、容器生命周期与回收 |
| MCP Python SDK | 2.1.0 | MIT | 已注册 Streamable HTTP 服务的工具往返 |

OpenSandbox server 使用发行版1.2.0固定内容地址。execd/egress 基于发行 BOM 对应源码 `25873227f60424bdc59bfa83b963bbc00e871297` 进行小范围适配，包含强制工具进程权限、管理认证和侧车退出时保持拒绝规则。原始作者声明、完整 Apache-2.0 文本和变更说明保留在 [补丁目录](../deploy/patches/README.md) 与生成镜像中。这是本地适配变体，不代表未经修改的上游默认配置已通过本机安全验收。

DeepSeek Harness 的审批、权限和上下文整理契约是设计参考，未将其 TypeScript/Cordis 运行内核嵌入 Python 服务。调查锁定的 commit 与官方链接见 已批准方案。OpenHands 仅参与方案比较，没有加入运行依赖。

OmniHarness 自身负责模型分阶段选择、预算、工作区授权、操作与审批持久记录、工件和用户界面。工具循环、图调度、检查点、摘要算法与容器生命周期沿用上述库；没有新增另一套工作流调度器。

实际隔离、恢复、模型和浏览器结果分别见 [M4 实施记录](M4-implementation.md)。本机容器采用普通 Docker 共享内核，未接入 gVisor 或虚拟机隔离。
