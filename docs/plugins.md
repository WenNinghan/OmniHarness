# 插件边界

OmniHarness 的插件用于复用模型接入、工具与验证逻辑。M4 已接入内置工作区工具、OpenSandbox 和显式注册的 MCP 工具；Python 包入口点仍主要提供发现与接口版本边界。发现一个 Python 插件不等于已授予它执行权限，也不意味着它会自动进入 Agent 的工具列表。

Python 插件属于会在本地执行的第三方代码。只安装已审查并信任的包。UI 不提供任意 Python 代码上传、自动 `pip install` 或未经确认的热加载。

## 配置与凭证

模型等配置可以通过 UI 编辑，也可以使用带 `schema_version` 的配置导入导出。导出文件不包含 API Key；导入创建新配置，不覆盖已有项，真实模型需要重新配置凭证。环境变量、部署设置与用户导入的模型配置是不同层，不应把数据库密码写进模型配置文件。

插件不应自行读取全局凭证库或记录运行期模型配置。Provider 运行所需的 Key 由服务在调用时提供；工具与 Verifier 只接收任务所需的显式输入。

## 稳定接口

接口与入口点以 `src/omniharness/plugins.py` 中发布的契约为准。当前入口点组为 `omniharness.plugins`，入口点加载一个无参工厂或类，返回带 `PluginManifest` 的实例。Manifest 声明 ID、名称、版本、`api_version`、类型、权限需求、副作用类别和配置 Schema。平台当前兼容 API 版本 `1`。插件自行校验输入并返回结构化输出；输入/输出约束不能只依赖提示词。

`examples/plugin` 提供可安装的 `RequiredSectionsVerifier`：只做区分大小写的字符串存在性检查，并给出字符位置。它展示 `async verify(artifact: dict, requirements: dict) -> dict` 接口。另有 `ToolPlugin.execute(arguments, context)`、Provider、WorkflowFactory 和 SearchProvider 协议供后续阶段接入；目前声明协议不代表已有对应的产品执行入口。

发现结果中的 `available` 表示插件成功加载且 Manifest 版本兼容，`incompatible` 表示版本不匹配，`error` 表示加载失败。这里的 `trusted` 是安装方对本地代码的信任前提，不代表平台已完成安全审计。插件执行与权限授权仍是独立步骤。

## 执行边界

- **Provider 插件**运行在受信任的模型调用层，可以接触单次请求所需的凭证；这是高信任扩展。
- **工具插件**的 Python 协议目前不自动接入产品工具列表。M4 内置工具经过统一操作记录和工作区授权，任意命令只在快照沙箱运行；外部工具可通过明确注册的 MCP 接入。
- **Verifier 插件**返回结构化验证结果。声明验证通过必须有实际检查依据，不能把模型评论或 Mock 输出当成执行证明。
- **MCP Server**是外部程序，MCP 协议本身不提供沙箱或权限隔离。M4 使用官方 SDK 2.1.0 的 Streamable HTTP，冻结本地允许的工具及 Schema，并对副作用逐次审批；不能自动继承 API/Worker 的凭证环境。配置见 [工具与上下文](M4-tools-context.md#受信任-mcp-服务)。

插件返回值、检索内容和 MCP 输出作为不可信数据处理，不能修改平台权限或覆盖用户指令。只有独立 Sandbox Controller 挂载 Docker socket；模型生成代码所在工具容器不获得该接口或模型凭据。
