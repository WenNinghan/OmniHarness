# M3：100K 输出上限与最高档参考价

2026-11-08 已更新本机 http://127.0.0.1:8787/。

## 本次行为

- 新模型默认最大输出 100,000 tokens，表单允许 16–131072；可自行调小。
- 原有 25 个模型均设为 100000。24 个通过正常模型配置接口更新，另一个已满足目标而跳过；Key及其他连接参数保留。
- AI 规划沿用所选模型上限，移除额外的 2048 限制。
- 模型设置、聊天选择器、阶段选择器及规划选择器显示实际输出上限，100000显示100K。
- 25 个中转站模型都有最高档参考价。DeepSeek取最高时段倍率，GLM5.1取最高输入档；其他模型按核对的公开单档价。
- 默认比较口径为输入/输出3:1、DeepSeek Flash=1×，用户可改；价格来源、时间和最高档口径可以展开查看。

## 价格来源

读取 https://e-flowcode.cc/api/pricing，并核对本站部署的前端 `https://e-flowcode.cc/static/js/index.1215fc395c.js`。

普通倍率输入单价 = 2×model_ratio，输出 = 输入×completion_ratio。本站公式p/c系数与普通倍率经同一展示转换，单位可比较。当前25模型公共分组相同，组倍率在相对比较中抵消。

快照时间 2026-11-08T13:35:03.388731Z，版本 a42d372ccf0b5dd13ecf71203521f9d2。保留7天有效期；过期后显示未知，需要更新快照。用户填写的报价优先；不混用不同口径、站点或来源的价格。它是无缓存、同组最高档参考，不是账户实际扣费。

审计原始来源与完整25项表保存在 `.omniharness/pricing-audit-20261108/`。

## 验证

| 项目 | 结果 |
|---|---|
| 后端契约与协议 | test_api、test_providers、test_ai_routing、test_response_protocol：103 passed |
| 前端函数 | 全lib测试33 passed；25项单价与审计数据一致 |
| 构建 | TypeScript/Vite通过，沿用线上基础样式 |
| 本地真实浏览器＋隔离API/Worker | test_response_browser：1 passed；包括100K可见、全部25项有价、来源、桌面/移动、新建表单、消息分离和刷新 |
| 已部署服务 | API健康，Worker运行，init退出0；页面0个JS错误，OpenAPI和新建表单默认100000，25模型均100000 |
| 真实网关工具往返 | deepseek-flash，POST https://e-flowcode.cc/v1/chat/completions，上限100000，加法工具调用与结果回传通过，15.11秒 |

真实工具测试使用现有本机凭据，调用前已向用户说明模型与地址。它证明本次请求接受100K参数并完成工具协议往返，不证明模型实际连续输出100K，也不代表另外24个模型已验证。

## 保留与限制

旧Run冻结配置及缺字段旧快照的2048兼容值保持。服务请求超时和上下文100条/20万字符限制没有随输出上限放大。未增加金额硬预算、自动摘要、项目工具或长期记忆。详细边界见 [当前能力核对](M3-runtime-boundaries.md)。

共享前端仅新增一条组件CSS显示规则；全局样式未修改。部署使用 `.omniharness/output-limits-web-20261108/` 的选择性构建，避免顺带上线工作区内其他未发布样式。

## 部署与回滚材料

- 当前镜像：`sha256:2faef542222e27fb3917e96dc80d005e6f8414a88b83dfdec6503ddcf263403c`。
- 旧镜像：`omniharness:pre-output-limits-20261108`。
- 备份：`.omniharness/data/backups/pre-output-limits-config-20261108-215227.json`、`pre-output-limits-20261108-215252.dump`；数据库备份已用pg_restore目录检查。
- 截图：`.omniharness/output-limits-ui/deployed-model-picker.png`、`deployed-price-basis.png`、`deployed-model-default.png`，另有隔离浏览器移动端截图。

没有数据库结构变更。回退上限时只处理仍对应本次写入版本的配置，保留用户后续修改；不要直接批量覆盖。
