"""Immutable versions and stage prompts for the stage workflow."""
import hashlib

STAGE_V1_WORKFLOW_VERSION = "stage-agent-v1"
STAGE_WORKFLOW_VERSION = "stage-agent-v2"
INTERACTIVE_STAGE_WORKFLOW_VERSION = "stage-agent-interactive-v1"
STAGE_KEYS = ("think", "plan", "dispatch", "execute", "review")
STAGE_V1_PROMPTS = {
    "think": "你负责理解任务。简要说明目标、约束、已知信息和需要澄清的问题；不展示隐藏思维过程。",
    "plan": '根据上下文给出计划，仅输出 JSON 对象 {"steps":[{"id":"s1","task":"...","acceptance":"...","depends_on":[]}]}。最多8步，依赖必须引用先前步骤。',
    "dispatch": '根据上面的计划分配任务，仅输出 JSON 对象 {"tasks":[{"id":"t1","step_id":"s1","instruction":"...","depends_on":[]}]}。每项必须引用计划中的步骤，最多8项，依赖必须引用先前任务；计划步骤的所有依赖必须体现在对应任务的依赖中。',
    "execute": "根据已分配任务，利用上下文中提供的材料执行。你没有文件、命令或联网工具，不得声称读取、修改、检索或测试过；缺少材料时说明阻碍。",
    "review": "独立审查目标、计划及执行结果，指出问题、证据与未验证项。你没有验证工具，不能将推测说成实测通过。",
}
STAGE_PROMPTS = {
    **STAGE_V1_PROMPTS,
    "review": ('独立审查目标、计划及执行结果；没有工具，不能将推测说成实测通过。'
               '仅输出 JSON 对象 {"summary":"总体结论",'
               '"findings":[{"location":"问题位置","severity":"HIGH|MEDIUM|LOW|INFO",'
               '"description":"问题与依据","status":"PASS|FAIL|UNVERIFIED|BLOCKED|DISPUTED",'
               '"recommendation":"建议"}],"unverified":["未验证事项"]}。'
               '没有问题时 findings 可为空，无法验证的事项必须明确列出。'),
}


def _versions(prompts):
    return {key: "builtin-" + hashlib.sha256(prompts[key].encode("utf-8")).hexdigest()[:20]
            for key in STAGE_KEYS}


STAGE_V1_VERSIONS = _versions(STAGE_V1_PROMPTS)
STAGE_VERSIONS = _versions(STAGE_PROMPTS)
STAGE_ROLE_BINDINGS = {key: key for key in STAGE_KEYS}

