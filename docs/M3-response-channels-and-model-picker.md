# M3 响应通道与模型选择器

日期：2026-11-08。来源需求与 G1/G2 见 .ai-governance 实现前设计，用户已回复“好的”确认实施。

## 行为变化

- 普通聊天分别保存、推送与显示正式回答和供应商明确提供的推理。推理默认折叠，展开不改变模型参数，也不增加 API 调用；“复制回答”只复制正文。
- 每条失败、取消或截断消息保留自身状态和错误；继续聊天或刷新不会留下只有头像的空失败消息。
- `length/max_tokens`、只有推理、过滤、缺少结束标记分别诊断，保留已有内容；截断不再当作成功。完整性校验同时用于阶段产物和 AI 规划。
- 真实诊断复现了网关在 length 时先返回推理，再把同一全文复制到 content 的情况。只有 **明确截断、两通道全文严格相等** 才移除正文副本。普通 stop、部分前缀、存在任何差异和网络错误不会套用这一规则。
- 不按自然语言、模型名字或裸 `<think>` 字符串猜测通道。原先已经混入正文而没有结构标记的历史保持原样。
- 多模型任务的任务分析、计划、分工、执行结果和审查意见仍作为业务产物展示。跨阶段不传递另一模型的私有推理状态；同模型工具请求按协议回传原生状态。

## 模型选择与参考倍率

聊天、五阶段编排、AI 规划面板共用搜索选择器：按模型系列分组，模型名去重，连接域名单独显示，支持键盘和小屏底部面板。继承默认会显示具体模型。

默认 DeepSeek Flash 为 1×，输入/输出按 3:1、无缓存比较；基准和比例可以调整。阶段已经填写预计用量时，使用该阶段的用量。点击价格可展开依据，选择模型不会自动重新规划。

参考价有两种独立来源：

- 已有浏览器中的用户填写估计；不覆盖已有填写内容，不完整填写也不偷偷替换为公开价。
- 2026-11-08 读取的 `https://e-flowcode.cc/api/pricing` 公开快照。只覆盖精确连接 `https://e-flowcode.cc/v1` 下 13 个已核实的同组线性公式，使用当前上海时段系数，注明 `1_vip_china` 同组、无缓存假设；不代表账户实际扣费。报价版本为 `a42d372ccf0b5dd13ecf71203521f9d2`，7 天后停止给出该快照倍率。

两种来源不混算。其余 12 个已配置模型的计价单位/动态条件尚不足以可靠比较，显示价格未知。公开报价目录更大不会扩大已登记的 25 模型目录；没有执行服务器提供的表达式代码。运行实际费用仍为未知，不能用参考倍率填账单。

## 数据与接口

迁移 `0008` 为 `messages` 增加 `reasoning_content` 和 `output_metadata`，均有兼容默认值。旧正文和模型配置不改写。

- 消息快照：新增 `reasoning_content`、`output_metadata`、该消息所属 Run 的 `error_code/error_message`。
- SSE：原 `message.delta` 仍表示正式正文；新增 `message.reasoning_delta` 与 `message.output_state`。
- 终态 `run.*` 附带 `message_id/output_metadata`；前端各通道独立重放、去重并与快照合并。
- 输出元信息记录 `finish_reason/output_status`；失败阶段另记 stage，显示为该阶段未完成。
- 可信 Python 插件的旧 `ModelDelta(text, usage)` 保持构造兼容。真实 compatible 传输严格检查结束信息，未知旧检查点不伪造停止原因。

兼容适配以已锁定的 `langchain-openai 1.7.4` 为基础，通过隔离子类保留其默认丢弃的网关字段。升级该依赖时应运行 `tests/test_response_protocol.py`；不使用全局 monkey patch，不引入新运行时依赖。

## 验收记录

| 检查 | 结果 |
|---|---|
| Provider、归一化、阶段、AI 规划定向回归 | 87 passed，包含 HTTP loopback、原生 Gemini/Anthropic 转换、跨模型原生状态边界及截断回显 |
| PostgreSQL 定向回归 | 32 passed；独立 `omniharness_test`，覆盖迁移、Worker 锁、阶段、旧工作流/检查点与规划隔离 |
| 前端状态/价格等单测 | 29 passed |
| 浏览器聊天/选择器 | 1 passed：真实本地 API/Worker，合成模型输出；搜索、键盘、默认折叠、复制、失败历史、刷新与小屏 |
| 浏览器 AI 规划流程 | 1 passed：隔离 PostgreSQL；生成、手动改审查模型、提交、刷新恢复及过期结果隔离 |
| 前端构建 | 通过；已有主 bundle 大于 500 kB 提示，未为此次改动引入拆包重构 |
| 实际 DeepSeek Flash 正常协议调用 | stop/complete；正文 43 字符、推理 669 字符分别收到；输入 52、输出 256、合计 308 tokens |
| 实际低上限诊断 | length/OUTPUT_TRUNCATED；一例正文与推理均 680 字符且逐字符相等。该协议形状已转成离线回归；没有保存原始思考文本或密钥 |
| 全量离线最终回归 | 271 passed，56 skipped；跳过项按环境条件标记，不能算通过 |
| 本地服务更新后验收 | 真实 API→Worker→PostgreSQL→SSE→浏览器通过；Run `08ab0967-2616-4489-a968-5020aa27ab56` completed，正文 92 字符、推理 560 字符、stop/complete；输入 72/输出 167/合计 239 tokens，实际费用未知；思考默认收起，聊天 25 选项、阶段 26 选项（含继承），控制台 JS 错误 0 |

真实调用始终事前说明模型和准确 URL；未对另外 24 个模型进行收费遍历测试。测试通过不等于宣称所有网关模型的行为已经实测。

## 发布与恢复

当前全局样式包含此前未发行的用户修改，本轮发行构建沿用正在运行版本的基础样式，仅追加新选择器和消息组件样式；不覆盖工作区原样式文件。源码正常构建及该选择性发行构建均通过。

已部署镜像：`omniharness:response-channels-candidate`，镜像 ID `sha256:71603fcc4f00c7dc871c576f38c19ecc600cd2561b742871a383f939e4761abd`，同时更新既有项目专用标签。保留旧镜像：`omniharness:pre-response-channels-20261108`。

更新前先停止 API 入口，再确认未完成 Run 为 0、停止 Worker、备份 PostgreSQL；备份为 `G:\harness\.omniharness\data\backups\pre-response-channels-20261108-204523.dump`，79,619 bytes，经 `pg_restore --list` 验证。之后升级到迁移 0008，API healthy、Worker running、init exit 0。25 个模型与原会话 14 条消息保持不变；Agent 就绪状态继续启用。

回滚须注意旧版 bootstrap 不认识 0008：在仅增量字段兼容的情况下，可保留数据库、切回旧镜像并仅启动 API/Worker（`--no-deps`），不能直接运行旧版 bootstrap。完整数据库回退需要对应备份，避免丢失更新之后的新消息。

已参考 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的输出契约与折叠思考，以及 [OpenCode](https://github.com/anomalyco/opencode) 的 compatible 字段与结束状态处理。此次没有复制它们的实现或替换 OmniHarness 的 LangGraph 多模型内核。
