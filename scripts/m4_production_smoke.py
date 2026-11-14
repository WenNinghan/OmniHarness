"""Post-cutover live smoke: controlled original patch + isolated snapshot command.

Prepare/run only on the root agent's explicit cutover signal. Uses existing model
configuration read-only. Never changes service configuration or restarts services.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import time
from pathlib import Path
from uuid import uuid4

import httpx
from m4_production_deploy import PRIVATE, SECRET, wsl
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"
OUTPUT = PRIVATE / "smoke"
STATE = OUTPUT / "state.json"
COOKIE = SECRET / "m4-production-smoke-session.json"
CHROME = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright/chromium-1243/chrome-win64/chrome.exe"
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "pending_review"}
EXPECTED = "M4_PRODUCTION_OK\n"
COMMAND = [
    "python",
    "-c",
    "from pathlib import Path; assert Path('result.txt').read_bytes() == b'M4_PRODUCTION_OK\\n'; assert not Path('.env').exists(); Path('snapshot-only.txt').write_text('ONLY_IN_SNAPSHOT\\n', encoding='utf-8'); print('M4_PRODUCTION_OK')",
]


def save(state):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def request_client():
    return httpx.Client(
        base_url=BASE,
        trust_env=False,
        timeout=30,
        headers={"Origin": BASE, "X-Omni-Request": "1"},
        cookies=json.loads(COOKIE.read_text(encoding="utf-8")),
    )


def get(http, path):
    response = http.get(path)
    response.raise_for_status()
    return response.json()


def post(http, path, body):
    response = http.post(path, json=body)
    response.raise_for_status()
    return response.json()


def start():
    if STATE.exists():
        raise RuntimeError(
            "A production smoke already exists; inspect/resume it instead of creating another Run"
        )
    health = httpx.get(BASE + "/health", trust_env=False, timeout=10)
    health.raise_for_status()
    if health.json().get("stage") != "M4-preview":
        raise RuntimeError("The production M4 cutover has not completed")
    code = wsl("docker", "exec", "omniharness-api-1", "omniharness", "auth-code").decode().splitlines()[-1]
    with httpx.Client(
        base_url=BASE, trust_env=False, timeout=30, headers={"Origin": BASE, "X-Omni-Request": "1"}
    ) as http:
        post(http, "/api/v1/auth/pair", {"code": code})
        active = [run for run in get(http, "/api/v1/runs") if run["status"] not in TERMINAL]
        if active:
            raise RuntimeError("Existing production tasks must finish before this smoke")
        tools = get(http, "/api/v1/tools/status")
        assert tools["enabled"] and tools["broker_ready"]
        models = get(http, "/api/v1/model-configs")
        assert len(models) == 25, "Expected the original 25 configurations, not preview test models"
        model = next(row for row in models if row["model_name"] == "deepseek-flash" and row["enabled"])
        if model.get("capabilities", {}).get("tools", {}).get("status") != "supported":
            raise RuntimeError(
                "Existing Flash tool capability is unverified; do not modify configuration here"
            )
        assert model["max_output_tokens"] == 100000
        COOKIE.write_text(json.dumps(dict(http.cookies)), encoding="utf-8")
        fixture = Path("G:/OmniHarness-M4-Fixtures") / ("production-" + uuid4().hex[:8])
        fixture.mkdir(parents=True)
        # A clearly synthetic marker verifies exclusion; it is not a credential.
        (fixture / ".env").write_text("M4_SYNTHETIC_MARKER=not-a-real-secret\n", encoding="utf-8")
        workspace = post(
            http,
            "/api/v1/workspaces",
            {
                "name": "M4 正式服务验收",
                "host_path": str(fixture),
                "read_allow": ["."],
                "write_allow": ["result.txt"],
                "command_approval": False,
            },
        )
        conversation = post(http, "/api/v1/conversations", {"title": "M4 正式服务 · 文件修改与沙箱验证"})
        state = {
            "stage": "registered",
            "service": BASE,
            "fixture": str(fixture),
            "workspace_id": workspace["id"],
            "conversation_id": conversation["id"],
            "model_id": model["id"],
            "model_name": model["model_name"],
            "models_before": models,
            "original_result_existed": (fixture / "result.txt").exists(),
            "requested_command": COMMAND,
        }
        assert state["original_result_existed"] is False
        save(state)
        result = post(
            http,
            f"/api/v1/conversations/{conversation['id']}/messages",
            {
                "content": "执行一个正式服务的最小验收，严格按顺序做两次工具调用，不要规划额外步骤。"
                "第1步：调用 workspace_patch 创建 result.txt，content 精确为 JSON 字符串 "
                + json.dumps(EXPECTED)
                + "，expected_sha256 为 null；已确认这是刚创建的空目录，目标文件不存在，不需要读取或列举。"
                "等第1步实际成功后，第2步：调用 run_command，command 原样使用数组 "
                + json.dumps(COMMAND)
                + "，cwd='.'，timeout_seconds=30。不要并行，不要重复。"
                "这条命令只在离线快照里检查文件、确认 .env 已过滤、创建一个仅快照存在的标记。"
                "不要调用其他工具，不要改其他原目录文件；得到实际命令结果后用一句话汇报。",
                "model_config_id": model["id"],
                "client_request_id": "production-smoke-" + uuid4().hex,
                "workflow_type": "agent",
                "workspace_id": workspace["id"],
                "agent_strategy": "single",
                "stage_overrides": [
                    {
                        "stage": stage,
                        "model_config_id": model["id"],
                        "model_version": model["version"],
                        "provider_version": model["provider_version"],
                    }
                    for stage in ("think", "plan", "dispatch", "execute", "review")
                ],
            },
        )
        state.update(stage="started", run_id=result["run_id"])
        save(state)
    print(json.dumps({"stage": "started", "run_id": state["run_id"], "service": BASE}))


def verify(state):
    with request_client() as http:
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            run = get(http, f"/api/v1/runs/{state['run_id']}")
            budget = get(http, f"/api/v1/runs/{state['run_id']}/budget")
            if run["status"] in TERMINAL:
                break
            if run["status"] == "awaiting_approval":
                post(http, f"/api/v1/runs/{state['run_id']}/cancel", {})
                state.update(stage="stopped-unexpected-approval", run=run, budget=budget)
                save(state)
                raise RuntimeError("Unexpected approval requested; no unknown operation will be approved")
            if budget.get("model_calls", 0) > 4 or budget.get("tool_calls", 0) > 2:
                post(http, f"/api/v1/runs/{state['run_id']}/cancel", {})
                state.update(stage="stopped-budget", run=run, budget=budget)
                save(state)
                raise RuntimeError("Smoke exceeded its bounded calls; stopped its own Run without replay")
            time.sleep(1)
        if run["status"] not in TERMINAL:
            post(http, f"/api/v1/runs/{state['run_id']}/cancel", {})
        operations = get(http, f"/api/v1/runs/{state['run_id']}/tools")
        artifacts = get(http, f"/api/v1/runs/{state['run_id']}/artifacts")
        approvals = get(http, f"/api/v1/runs/{state['run_id']}/approvals")
        fixture = Path(state["fixture"])
        actual = (fixture / "result.txt").read_bytes() if (fixture / "result.txt").is_file() else None
        models_after = get(http, "/api/v1/model-configs")
        checks = {
            "run_completed": run["status"] == "completed",
            "original_file_exact": actual == EXPECTED.encode(),
            "snapshot_writeback_absent": not (fixture / "snapshot-only.txt").exists(),
            "original_marker_retained": (fixture / ".env").read_text(encoding="utf-8")
            == "M4_SYNTHETIC_MARKER=not-a-real-secret\n",
            "model_configs_unchanged": models_after == state["models_before"],
            "two_expected_tools": [op["tool_name"] for op in operations]
            == ["workspace_patch", "run_command"],
            "tool_results_complete": all(
                op["status"] == "completed" and not op["slot_claimed"] for op in operations
            ),
            "no_approvals": approvals == [],
            "bounded_model_calls": 2 <= budget.get("model_calls", 0) <= 4,
        }
        commands = [op for op in operations if op["tool_name"] == "run_command"]
        actual_command = commands[0]["parameters"].get("command") if len(commands) == 1 else None
        if isinstance(actual_command, str):
            actual_command = shlex.split(actual_command)
        checks["exact_assertion_command"] = actual_command == COMMAND
        checks["actual_snapshot_assertions_passed"] = bool(
            len(commands) == 1
            and commands[0]["result"]
            and commands[0]["result"].get("exit_code") == 0
            and commands[0]["result"].get("stdout_preview") == EXPECTED
        )
        for artifact in artifacts:
            response = http.get(f"/api/v1/artifacts/{artifact['id']}/content", params={"limit": 65536})
            response.raise_for_status()
            assert int(response.headers["X-Artifact-Size"]) <= 65536
            (OUTPUT / (artifact["id"] + ".txt")).write_bytes(response.content)
        state.update(
            stage="verified" if all(checks.values()) else "failed",
            checks=checks,
            run=run,
            budget=budget,
            tools=operations,
            artifacts=artifacts,
            approvals=approvals,
            original_file=actual.decode("utf-8") if actual is not None else None,
        )
        save(state)
        if not all(checks.values()):
            raise RuntimeError("Production smoke failed; inspect retained evidence, do not replay")
    print(
        json.dumps(
            {
                "stage": state["stage"],
                "run_id": state["run_id"],
                "checks": checks,
                "model_calls": budget["model_calls"],
                "tool_calls": budget["tool_calls"],
            }
        )
    )


def screenshot(state):
    report = {"page_errors": [], "blocked_mutations": [], "screenshots": []}
    with sync_playwright() as play:
        browser = play.chromium.launch(headless=True, executable_path=str(CHROME))
        context = browser.new_context(viewport={"width": 1560, "height": 1040})
        context.add_cookies(
            [
                {
                    "name": key,
                    "value": value,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
                for key, value in json.loads(COOKIE.read_text(encoding="utf-8")).items()
            ]
        )
        context.add_init_script(
            "localStorage.setItem('omni.details','true'); localStorage.setItem('omni.conversation',"
            + json.dumps(state["conversation_id"])
            + ");"
        )
        page = context.new_page()
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))

        def read_only(route):
            if route.request.method in {"GET", "HEAD", "OPTIONS"}:
                route.continue_()
            else:
                report["blocked_mutations"].append(route.request.url)
                route.abort()

        page.route("**/api/**", read_only)
        page.goto(BASE, wait_until="networkidle")
        activity = page.locator(".tool-activity")
        activity.wait_for()
        page.wait_for_function("document.querySelectorAll('.tool-activity .tool-record').length >= 2")
        activity.scroll_into_view_if_needed()
        for record in activity.locator(".tool-record").all():
            record.locator("summary").click()
        filename = OUTPUT / "production-tools.png"
        page.screenshot(path=str(filename), full_page=True, animations="disabled")
        report["screenshots"].append(str(filename))
        stdout = activity.locator(".artifact-list>button").filter(has_text="stdout.log")
        stdout.first.click()
        activity.locator(".artifact-preview").wait_for()
        activity.locator(".artifact-preview").scroll_into_view_if_needed()
        filename = OUTPUT / "production-stdout.png"
        page.screenshot(path=str(filename), full_page=True, animations="disabled")
        report["screenshots"].append(str(filename))
        assert not report["page_errors"] and not report["blocked_mutations"]
        browser.close()
    state["ui"] = report
    save(state)
    print(json.dumps(report))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "verify", "screenshot"))
    action = parser.parse_args().action
    if action == "start":
        start()
    else:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        {"verify": verify, "screenshot": screenshot}[action](state)


if __name__ == "__main__":
    main()
