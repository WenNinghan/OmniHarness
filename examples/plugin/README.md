# 最小 Verifier 插件

这个示例通过 `omniharness.plugins` 入口点注册 `RequiredSectionsVerifier`，使用平台 `PluginManifest` 的 API 版本 `1`。它仅检查指定字符串是否出现在文本中，返回每项的字符位置；不验证事实，也不保证匹配内容实际是文档标题。

在已安装 OmniHarness 的开发环境中，从项目根目录运行：

```sh
uv pip install --python .venv/bin/python --no-deps -e examples/plugin
```

Windows 将 Python 路径改为 `.venv\Scripts\python.exe`。安装后重启本地开发 API，插件发现接口应显示 `example.required-sections`。生产容器使用锁定构建；不要在运行中的容器里临时安装后期待重建仍保留插件。

直接调用的输入示例：

```python
import asyncio
from omniharness_example import RequiredSectionsVerifier

result = asyncio.run(RequiredSectionsVerifier().verify(
    {"content": "# Methods\nExample method.\n# Results\nExample result."},
    {"required_sections": ["# Methods", "# Results"]},
))
assert result["status"] == "PASS"
```

缺少文本或有效检查规则时返回 `UNVERIFIED`；有待查字符串缺失时返回 `FAIL`。该例不访问文件、网络、模型或凭证。

M0–M2 提供发现接口和开发者直接调用示例，尚未接通自动审查工作流、工具执行或 MCP 沙箱。插件发现成功不能作为工作流已执行的证明。

只安装可信 Python 包。包导入本身就可能执行代码，插件注册机制不是安全沙箱。
