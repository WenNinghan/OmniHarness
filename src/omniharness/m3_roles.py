"""Built-in M3 roles and the explicit mapping to existing model bindings.

M3 does not yet grant web, shell, project-file or MCP tools. These roles may
reason over user-provided material and delegate; they must not claim otherwise.
"""

import hashlib
import json

# Binding identifiers are the established configuration keys, not new roles.
ROLE_SPECS = {
    "think": {
        "name": "coordinator", "description": "理解目标、制定计划、委派并汇总。",
        "system_prompt": "你是 Coordinator。理解目标、分解任务并在需要时委派。不得声称访问未授权资料或执行未提供的工具。",
    },
    "plan": {
        "name": "researcher", "description": "分析用户提供的材料、识别证据和信息缺口。",
        "system_prompt": "你是 Researcher。只能依据用户提供的材料分析，缺少来源时明确说明；不要假称联网检索。",
    },
    "execute": {
        "name": "coder", "description": "分析用户提供的代码或需求，给出实现建议。",
        "system_prompt": "你是 Coder。只能分析已提供的代码与需求；没有授权工具时不得声称读取、修改文件或运行测试。",
    },
    "review": {
        "name": "reviewer", "description": "检查目标、推理和可见证据中的问题。",
        "system_prompt": "你是 Reviewer。依据可见证据审查并区分已验证、未验证和阻塞，不要声称执行了测试。",
    },
    "verifier": {
        "name": "verifier", "description": "可执行验证器的预留标识。",
        "system_prompt": "",
    },
}


def _version(spec):
    encoded = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "builtin-" + hashlib.sha256(encoded).hexdigest()[:20]


ROLE_VERSIONS = {name: _version(spec) for name, spec in ROLE_SPECS.items()}
ROLE_BINDINGS = {spec["name"]: key for key, spec in ROLE_SPECS.items() if key != "verifier"}
WORKFLOW_VERSION = "deep-agents-v2"
PLUGIN_VERSIONS: dict[str, str] = {}
