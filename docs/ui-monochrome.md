# 视觉规范：黑白科技感（2026-11-08）

状态：已落地并通过浏览器截图验收。实现方式是 `web/src/styles.css` 末尾的**追加覆盖层**——原有布局规则完全保留，只覆盖颜色、边框、阴影与字体，便于随时回滚或换肤。

## 原则

- 纯黑白灰，**不使用彩色语义色**。白色是唯一强调色。
- 1px 细边框、小圆角（4–8px）、无彩色渐变、无发光阴影。
- 等宽字体用于标识与数值：Run ID、Token、预算、时间戳、模型 ID。
- 克制的网格背景（`#ffffff05`）与极淡的顶部径向层次（`#ffffff08`），不产生脏光斑。
- 信息层级靠亮度与边框区分，不靠色相；错误态用更亮边框 + 反白图标，而不是红色。

## 色板

| 用途 | 值 |
| --- | --- |
| 页面底 | `#0a0a0a` |
| 侧栏 / 运行面板 | `#0e0e0e` / `#0d0d0d` |
| 卡片 / 输入框 / 次级面板 | `#111111` / `#141414` / `#161616` |
| 边框 | `#1f1f1f`（分区）、`#262626` / `#2a2a2a`（控件）、`#3d3d3d`（悬停） |
| 主文字 | `#e9e9e9` / `#f5f5f5` |
| 次文字 | `#8a8a8a` / `#9a9a9a` |
| 弱文字 / 占位 | `#6a6a6a` |
| 主按钮 | 背景 `#f5f5f5`，文字 `#0a0a0a` |
| 错误态 | 背景 `#1a1a1a`，边框 `#4a4a4a`，文字 `#f5f5f5` |

`badge-blue / green / amber / red`、`orb-blue / green / red`、provider 图标等所有原有彩色语义类，全部改为灰阶变体（靠亮度与边框区分），不引入新配色。

## 字体与控件

- 正文：`Inter, "Segoe UI", "Microsoft YaHei", sans-serif`。
- 等宽：`"Cascadia Code", Consolas, monospace`，用于 `.run-id`、`.usage-grid strong`、`.budget-grid strong`、`.event-row time`、`.cost-row strong`、`.model-identity code`、`.test-time`、`.control-row > span`。
- `:root { color-scheme: dark }` 让原生下拉、复选框、滚动条自动深色；`scrollbar` 自定义为深灰。
- `focus-visible` 用 2px 白色描边，保证键盘可达性。

## 验收

以下截图由隔离浏览器测试生成。

| 截图 | 内容 |
| --- | --- |
| `01-pairing.png` | 配对页 |
| `02-welcome.png` | 欢迎页 + 网格背景（光晕已修） |
| `03-models.png` | 模型连接设置页 |
| `04-chat-run.png` | 真实 Mock 对话 + 运行详情（运行控制 / 只读预算 / 控制命令 / 用量） |
| `05-mobile.png` | 390px 移动端视口 |

`npm run build`（tsc + vite）与 `npm test`（6 项）通过。**前端类型检查、构建与截图不等于后端功能验收**；截图只证明视觉与页面可达性。

## 新增页面必须遵守

以后任何新界面（尤其是编排配置页）必须复用本规范：纯黑白灰、白色主按钮、1px 边框、等宽字体用于 ID/数值，不引入彩色语义色或独立配色体系。
