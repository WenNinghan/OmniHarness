# M4：先验证链路，再比较效果

`scripts/m4_evaluate.py` 通过现有 REST API 创建实际 Agent Run。第一轮固定为两个小代码任务 × 三种配置，共六个 Run。它用于验证多模型、工具、持久化、验收和记录是否接通，不能用六次试验宣称多 Agent 更准确、更便宜或更快。

| 条件 | 实际执行方式 | 比较目的 |
|---|---|---|
| A | `agent_strategy=single`，一个执行 Agent，使用选定的强基线模型及全部相同执行工具 | 有工具、可自主规划的单 Agent 基线 |
| B | `agent_strategy=staged`，思考、规划、分配、执行、审查均用 A 的模型 | A/B 检查分工流程的影响 |
| C | 同一五阶段流程，使用明确冻结的多模型分配 | B/C 检查模型分工的影响 |

C 的模型方案须在试验前确定，可来自用户确认的 AI 推荐。Runner 不会边测边更换模型，也不把无工具聊天或刻意选弱的模型作为 A。所有模型必须已启用、不是 Mock，且已有工具调用能力测试记录；它不会为了评测自动探测模型或补装依赖。

兼容接口的工具探针使用和实际执行相同的 `auto` 选择方式，并继续要求工具调用、精确参数、工具结果回传和最终回答全部通过。原因是一些思考模型允许工具调用，却拒绝强制 `required` 选择；例如 [DeepSeek 官方 Chat Completions 说明](https://api-docs.deepseek.com/api/create-chat-completion/) 明确给出这种区别。探针失败仍保留失败记录，不通过修改能力状态绕过它。

## 两个任务与独立验收

示例位于 `examples/evaluation/m4/python` 与 `examples/evaluation/m4/typescript`，分别对应现有版本 `1.0.0` 固定任务集中的 `python-001` 区间合并、`typescript-001` 数字排序。示例故意含缺陷；公开 smoke 只覆盖基本情形。

每次试验创建新的工作区，只授权写入 `candidate.py` 或 `candidate.ts`，读权限、网络策略和工具相同。Runner 验证初始源码与任务集完全一致，不重置、清空或复用上一轮工作区。提示词只包含任务输入、接口及约束，不包含 `expected`、参考实现或隐藏断言。

任务终止后，Runner 读取实际落盘源码，在工作区之外创建独立快照，将任务集的验收代码与该源码放入一个新 OpenSandbox 容器。它复用应用的 `SandboxExecutor`、固定工具镜像、非 root 执行、网络拒绝、超时、完整日志和容器回收。固定宿主脚本仅传输文件；模型修改的代码不在 Windows/WSL 宿主执行。

隐藏验收不会通过 Agent 工具接口提供，也不会被发送回任务让 Agent 按答案修复。通过要求真实退出码为零且验收代码运行到独立完成标记；公开 smoke、模型自述或缺失退出码都不能代替通过。验收开始前还会在相同容器环境校准两份已知缺陷代码应失败、两份固定参考实现应通过。校准失败时不提交任何模型任务。

TypeScript 当前由固定镜像中的 Node.js 24 内建类型擦除执行，检查函数运行行为。**TypeScript 编译器类型检查仍标为 `UNVERIFIED`**，不将运行通过写成完整类型验收。隐藏测试属于固定小任务功能检查，不是针对恶意候选程序的防作弊评测。

## 运行前配置

创建一个本地 JSON 计划。这里只保存配置 ID 和凭据文件位置，不保存 Provider Key。示例中的镜像 ID 必须换成当前已验证、固定的实际镜像，所有本机目录使用绝对路径：

```json
{
  "schema_version": 1,
  "api_url": "http://127.0.0.1:18792",
  "baseline_model_id": "已测试的强基线模型配置ID",
  "stage_models": {
    "think": "模型配置ID-1",
    "plan": "模型配置ID-1",
    "dispatch": "模型配置ID-2",
    "execute": "模型配置ID-2",
    "review": "模型配置ID-1"
  },
  "workspace_root": "G:/OmniHarness-M4-Fixtures/evaluation",
  "output_root": "G:/harness/.omniharness/evaluation-results",
  "session_cookie_file": "G:/harness/.omniharness/private/evaluation-session.txt",
  "sandbox": {
    "endpoint": "http://127.0.0.1:19090",
    "key_file": "<你的私有控制器密钥文件路径>",
    "image": "sha256:替换为已验证的64位镜像ID",
    "snapshot_root": "/var/lib/omniharness-m4-preview/snapshots",
    "wsl_distro": "OmniHarnessCheckAlt"
  }
}
```

`session_cookie_file` 由本机配对流程得到，仅含会话 cookie 值。不要在终端参数或报告中粘贴 cookie/Key。WSL 文件传输只作用于计划指定的独立快照根目录；Windows 示例复用现有受控 WSL 发行版。Linux 运行可省略 `wsl_distro`，由当前用户在该独立根目录创建快照；该用户不能是容器工作负载 UID 10001。验收文件只读，快照保留供复核，不自动删除主机目录。

先检查计划，不访问网络、不写文件、不提交模型调用：

```powershell
.venv/Scripts/python.exe scripts/m4_evaluate.py --plan G:/harness/.omniharness/evaluation-plan.json
```

确认实际模型和请求地址、允许的调用预算后再运行：

```powershell
.venv/Scripts/python.exe scripts/m4_evaluate.py --plan G:/harness/.omniharness/evaluation-plan.json --execute
```

Runner 使用模型配置当前版本号与 Provider 版本号提交 `stage_overrides`，服务端冻结后再执行；不会修改全局阶段默认配置。选定模型的输入策略、实际可用输入和主输出上限必须一致，输出 100K 不会为了评测被自动降低。记录实际冻结的预算、模型、上下文与沙箱配置；不完整或不一致时公平性标为 `UNVERIFIED` / `FAIL`。

试验顺序按公开 seed 打乱，串行提交，避免三组争用同一个 Worker。它不会自动批准高风险动作；遇到审批、暂停、待核对或超时，只取消自己创建的 Run 并保留原因。取消无法确认时停止后续试验，避免失控增加调用量。创建请求、Provider 调用和验收命令均不自动重试。

## 结果读取

每个试验目录保存输入、初始与最终源码哈希、Run、模型操作、工具操作、审批、预算、工件元数据、消息、完整持久事件及独立验收日志。最终源码另存一份，方便复核。`report.json` 关联这些原始记录，包括：

- 端到端耗时：提交任务至终态，包含排队、规划、执行、摘要与审查；独立验收耗时单独记录。
- 活跃时间、模型/工具调用计数、摘要次数、实际用量记录、执行结果与独立验收结果。
- 有效审查发现和误报在未人工标注时为 `null`，不以“审查提出了几个问题”代替有效发现。
- 估算费用与实扣费用缺少可信来源时均为 `null`。用量缺失不能当作零 token。
- `leakage_check=UNVERIFIED`；安全测试由 M4 权限/隔离专项报告负责，这两个功能任务不能证明没有泄露。

`status=completed` 只说明试验计划已跑完；是否通过要看各次 `acceptance.status`，配置是否可比要看 `fairness.status`。失败、阻碍和不同结论都原样保留，不仅发布成功样例。

正式效果比较至少每条件重复三次，并扩大到不同复杂度、长上下文和可并行任务；同时记录冷热启动条件、模型版本、费率来源及日期。`--repetitions 3` 仍只重复这两个小任务，不会自动变成 20 任务评测。完整 20 任务 × 三条件 × 至少三次的 180 Runs 需要另行确定费用上限和研究验收器，**不在本次自动执行范围内**。

## 2026-11-09 实际链路烟测

最终候选 build7 的六例已实际运行：**Run 完整完成 5/6，落盘代码的独立隐藏验收 6/6 通过**。最后一例的代码已修复，但摘要输出被模型截断，任务未完整交付，不能把它计为工作流成功。四项已知缺陷/参考实现校准全部通过，冻结配置比较 `fairness.status=PASS`。Runner 因最后一例失败将报告标为 `blocked`，报告内已保存全部六例；没有自动重试或剩余评测任务在后台执行。

这次 A、B 使用配置模型 `deepseek-v4-pro`；C 的思考、规划、审查使用同一 Pro，分配、执行使用 `deepseek-flash`。请求均经用户授权的兼容接口 `https://e-flowcode.cc/v1/chat/completions`。它们是所请求的模型标识，不将中转接口标识当作底层模型版本的独立证明。三组都保留主输出上限 100,000、输入目标 16,000、80% 摘要触发、摘要上限 4,000，以及每 Run 50 次模型请求、100 次工具调用、2 轮修订、1,800 秒活跃执行预算。没有为了让某个条件通过而放宽配置。

| 任务 | 配置 | Run 完成 | 隐藏功能验收 | 墙钟秒 | 模型调用（含摘要） | 工具调用 | 摘要次数 | 输入 tokens | 输出 tokens | 总 tokens |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| TypeScript 数字排序 | A 单 Agent / Pro | PASS | PASS | 56.734 | 7 | 8 | 1 | 22,612 | 2,678 | 25,290 |
| TypeScript 数字排序 | B 五阶段 / Pro | PASS | PASS | 191.281 | 12 | 9 | 2 | 35,779 | 10,970 | 46,749 |
| TypeScript 数字排序 | C 五阶段 / 多模型 | PASS | PASS | 176.422 | 15 | 13 | 2 | 51,668 | 13,095 | 64,763 |
| Python 区间合并 | A 单 Agent / Pro | FAIL：摘要截断 | PASS | 122.422¹ | 9 | 10 | 2 | 31,891 | 8,295 | 40,186 |
| Python 区间合并 | B 五阶段 / Pro | PASS | PASS | 194.250 | 11 | 9 | 1 | 35,879 | 12,037 | 47,916 |
| Python 区间合并 | C 五阶段 / 多模型 | PASS | PASS | 216.344 | 23 | 18 | 7 | 79,677 | 21,085 | 100,762 |

¹ 这是提交到失败终态的耗时，不能与成功交付的耗时直接解释为速度优势。墙钟时间包含摘要和审查，不包含任务终止后独立隐藏验收的容器时间。六例合计 77 次模型调用（其中 15 次摘要）、67 次工具调用、325,666 tokens；各例 `usage_complete=true`。模型费用估算和实扣费用均为 `null`，**未知不是零，也不能用 token 数直接代替跨模型费用**。六例的流程修订次数均为零；未额外人工标注审查有效发现和误报，仍为 `null`。TypeScript 类型检查仍为 `UNVERIFIED`。

最后一例 `8215da38-0b70-4f65-a585-684fb2841071` 的第二次摘要明确返回 `finish_reason=length`、`output_status=truncated`：实际输出 4,000 tokens，其中供应商用量报告的 reasoning 为 2,987 tokens，公开正文为 3,795 字符。该摘要输入的本地保守估算为 10,014（小于 16,000），供应商实际输入用量为 3,159。框架因此记录 `SUMMARY_INCOMPLETE` 并停止；这次不是把字符数误当输出 tokens。推理正文按设计没有保存，不能从用量数字推算其实际字符长度。9 次模型请求已包含这两次摘要及其消耗，未补发请求，也未提高 4K 上限。

这两个小任务没有呈现多模型的普遍优势：TypeScript 的 A 比 B、C 都快；C 相对 B 的单次 TypeScript 用时略低，但模型调用和 tokens 更多；Python 的 C 比 B 更慢且 tokens 更多。Python A 的交付失败说明固定摘要策略仍存在实测限制，不能从一次失败推导“单 Agent 更差”。六次单次试验仅支持链路和当前样例行为判断，不支持稳定质量提升、费用降低或统计显著性结论。

本机证据目录为 `.omniharness/m4-evaluation/results/m4-eval-932a475d164a4c0e/`；入口 `report.json` 关联六个案例目录中的 `run.json`、`operations.json`、`tools.json`、`events.json`、`acceptance.json`、实际源码和日志。最后失败摘要另存为 `006-python-001-A-r1/eb387c07-f1b7-4d1b-965c-607b1699c5b8.json`。本轮应用镜像固定为 `sha256:a596d6108779a275829c22abf2ef0516531634d317db909f7777becba758cc8d`，独立验收与任务工具镜像均为 `sha256:95ece64f0e9b4196a12fcc4c5098664bc5af993bd0ddd14e9957f29bf0857d9e`。

此前三批失败没有覆盖或删除：`m4-eval-d21341f78b25443b` 为阶段指令顺序导致的 `STAGE_OUTPUT_INVALID`；`m4-eval-d34721f1aa8947cb` 为中文摘要字符估算被误用于输出预算；`m4-eval-d3cedbdc7ef649be` 为执行任务反复移至工具结果之后，造成重复执行及 `CONTEXT_LIMIT`（其代码已通过隐藏验收）。三批各保存一例，修复后使用新目录和新 Run 重测，原失败不会转成通过。它们合计 41 次模型请求、30 次工具调用、13 次摘要、193,371 tokens，费用未知；汇总见 `.omniharness/m4-evaluation/failed-trial-ledger.json`。这些调试消耗与上表六例分开记录；两者合计 118 次模型请求、97 次工具调用、28 次摘要、519,037 tokens，不包含能力探针、审批 UI 或独立聊天烟测。
