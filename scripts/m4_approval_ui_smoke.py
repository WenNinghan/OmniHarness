"""Explicit staged acceptance test: one harmless command, real Flash, real approval.

Run only after the comparison batch has ended. Every stage preserves its evidence.
This script never restarts production, changes models, or approves another command.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import time
from pathlib import Path
from uuid import uuid4

import httpx
from m4_preview_deploy import PRIVATE, PROJECT, wsl
from m4_preview_ui import BASE, CHROME
from playwright.sync_api import sync_playwright

OUTPUT = PRIVATE / "approval-ui"
STATE = OUTPUT / "state.json"
COOKIE_FILE = PRIVATE / "secrets/approval-ui-session.json"
COMMAND = ["python", "-c", "print('M4_APPROVAL_OK')"]
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def save(value):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def client():
    return httpx.Client(
        base_url=BASE,
        trust_env=False,
        timeout=30,
        headers={"Origin": BASE, "X-Omni-Request": "1"},
        cookies=json.loads(COOKIE_FILE.read_text(encoding="utf-8")),
    )


def get(http, path):
    response = http.get(path)
    response.raise_for_status()
    return response.json()


def post(http, path, body):
    response = http.post(path, json=body)
    response.raise_for_status()
    return response.json()


def no_other_active(http, own_id=None):
    others = [
        run
        for run in get(http, "/api/v1/runs")
        if run["id"] != own_id and run["status"] not in TERMINAL | {"pending_review"}
    ]
    if others:
        raise RuntimeError("Another candidate Run is active; this test must wait")


def start():
    if STATE.exists():
        raise RuntimeError("An approval acceptance test already exists; resume its next stage")
    code = wsl("docker", "exec", PROJECT + "-api-1", "omniharness", "auth-code").decode().splitlines()[-1]
    with httpx.Client(
        base_url=BASE, trust_env=False, timeout=30, headers={"Origin": BASE, "X-Omni-Request": "1"}
    ) as http:
        post(http, "/api/v1/auth/pair", {"code": code})
        no_other_active(http)
        COOKIE_FILE.write_text(json.dumps(dict(http.cookies)), encoding="utf-8")
        models = get(http, "/api/v1/model-configs")
        model = next(row for row in models if row["model_name"] == "deepseek-flash" and row["enabled"])
        if model.get("capabilities", {}).get("tools", {}).get("status") != "supported":
            raise RuntimeError("Flash has not passed the actual tool capability probe")
        fixture = Path("G:/OmniHarness-M4-Fixtures") / ("approval-ui-" + uuid4().hex[:8])
        fixture.mkdir(parents=True)
        (fixture / "README.md").write_text(
            "M4 approval smoke: no project changes permitted.\n", encoding="utf-8"
        )
        workspace = post(
            http,
            "/api/v1/workspaces",
            {
                "name": "M4 真实审批验收",
                "host_path": str(fixture),
                "read_allow": ["."],
                "write_allow": [],
                "command_approval": True,
            },
        )
        conversation = post(http, "/api/v1/conversations", {"title": "M4 真实审批与重启恢复验收"})
        state = {
            "workspace_id": workspace["id"],
            "conversation_id": conversation["id"],
            "fixture": str(fixture),
            "model": model["model_name"],
            "model_id": model["id"],
            "requested_command": COMMAND,
            "stage": "registered",
        }
        save(state)
        result = post(
            http,
            f"/api/v1/conversations/{conversation['id']}/messages",
            {
                "content": "这是一个权限审批验收。请只调用一次 run_command，command 必须原样使用数组 "
                + json.dumps(COMMAND, ensure_ascii=False)
                + "，cwd 为 .，timeout_seconds 为 30。等待人工批准后执行。不要修改文件，"
                "不要运行其他命令或使用其他工具，不要重复这条命令。完成后只根据实际 stdout 汇报。",
                "model_config_id": model["id"],
                "client_request_id": "approval-ui-" + uuid4().hex,
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
        state.update(run_id=result["run_id"], stage="started")
        save(state)
    print(json.dumps({"stage": state["stage"], "run_id": state["run_id"]}))


def exact_pending(http, state):
    decisions = get(http, f"/api/v1/runs/{state['run_id']}/approvals")
    pending = [item for item in decisions if item["status"] == "pending"]
    if len(pending) != 1:
        raise RuntimeError("Expected exactly one pending command; no decision was sent")
    approval = pending[0]
    operation = approval["operation"]
    parameters = operation["parameters"]
    command = parameters.get("command")
    if isinstance(command, str):
        command = shlex.split(command)
    if operation["tool_name"] != "run_command" or command != COMMAND or parameters.get("cwd", ".") != ".":
        raise RuntimeError("The proposed operation differs from the harmless acceptance command")
    if (
        operation["status"] != "awaiting_approval"
        or operation.get("command_id")
        or operation.get("sandbox_id")
    ):
        raise RuntimeError("Command was dispatched before the explicit approval")
    tools = get(http, f"/api/v1/runs/{state['run_id']}/tools")
    if len(tools) != 1:
        raise RuntimeError("Unexpected additional tool requests; stop this acceptance test")
    return approval


def inspect_ui(state, name, *, approve=False):
    report = {"page_errors": [], "blocked_mutations": []}
    with sync_playwright() as play:
        browser = play.chromium.launch(headless=True, executable_path=str(CHROME))
        context = browser.new_context(viewport={"width": 1560, "height": 1040})
        cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
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
                for key, value in cookies.items()
            ]
        )
        context.add_init_script(
            "localStorage.setItem('omni.details','true'); localStorage.setItem('omni.conversation',"
            + json.dumps(state["conversation_id"])
            + ");"
        )
        page = context.new_page()
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))

        def boundary(route):
            request = route.request
            if request.method in {"GET", "HEAD", "OPTIONS"}:
                return route.continue_()
            if (
                approve
                and request.method == "POST"
                and request.url == BASE + f"/api/v1/approvals/{state['approval_id']}/decision"
            ):
                body = request.post_data_json
                if body["decision"] == "approve" and body["fingerprint"] == state["fingerprint"]:
                    state["decision_body"] = body
                    save(state)
                    return route.continue_()
            report["blocked_mutations"].append(request.url)
            route.abort()

        page.route("**/api/**", boundary)
        page.goto(BASE, wait_until="networkidle")
        activity = page.locator(".tool-activity")
        activity.wait_for()
        if approve or state["stage"] in {"pending", "restarted"}:
            card = activity.locator(".approval-card")
            card.wait_for()
            card.scroll_into_view_if_needed()
        else:
            activity.scroll_into_view_if_needed()
        page.screenshot(path=str(OUTPUT / (name + ".png")), full_page=True, animations="disabled")
        if approve:
            with page.expect_response(
                lambda response: (
                    response.url.endswith(f"/approvals/{state['approval_id']}/decision")
                    and response.request.method == "POST"
                )
            ) as decision_response:
                activity.get_by_role("button", name="批准此操作", exact=True).click()
            assert decision_response.value.status == 202
        elif state["stage"] == "completed":
            records = activity.locator(".tool-record")
            if records.count():
                records.first.locator("summary").click()
                page.screenshot(
                    path=str(OUTPUT / "completed-command.png"), full_page=True, animations="disabled"
                )
            files = activity.locator(".artifact-list>button")
            if files.count():
                files.first.click()
                activity.locator(".artifact-preview").wait_for()
                activity.locator(".artifact-preview").scroll_into_view_if_needed()
                page.screenshot(
                    path=str(OUTPUT / "completed-artifact.png"), full_page=True, animations="disabled"
                )
        assert not report["page_errors"] and not report["blocked_mutations"]
        browser.close()
    state["ui_" + name] = report
    save(state)


def pending(state):
    with client() as http:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            run = get(http, f"/api/v1/runs/{state['run_id']}")
            if run["status"] == "awaiting_approval":
                approval = exact_pending(http, state)
                state.update(
                    stage="pending",
                    approval_id=approval["id"],
                    fingerprint=approval["fingerprint"],
                    operation_id=approval["operation_id"],
                )
                save(state)
                inspect_ui(state, "pending-before-restart")
                print("Waiting for explicit approval; zero command dispatches.")
                return
            if run["status"] in TERMINAL | {"pending_review"}:
                raise RuntimeError("Run ended before reaching the intended approval")
            time.sleep(1)
    raise TimeoutError("Approval was not reached; do not create another paid Run")


def restart(state):
    if state["stage"] != "pending":
        raise RuntimeError("Restart test requires the verified approval checkpoint")
    with client() as http:
        no_other_active(http, state["run_id"])
        approval = exact_pending(http, state)
        assert approval["id"] == state["approval_id"]
        container = PROJECT + "-worker-1"
        label = (
            wsl(
                "docker",
                "inspect",
                container,
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}',
            )
            .decode()
            .strip()
        )
        assert label == PROJECT
        before = wsl("docker", "inspect", container, "--format", "{{.State.StartedAt}}").decode().strip()
        wsl("docker", "restart", container)
        time.sleep(5)
        after = wsl("docker", "inspect", container, "--format", "{{.State.StartedAt}}").decode().strip()
        assert after != before
        approval = exact_pending(http, state)
        assert approval["id"] == state["approval_id"] and approval["fingerprint"] == state["fingerprint"]
        assert get(http, f"/api/v1/runs/{state['run_id']}")["status"] == "awaiting_approval"
        state.update(stage="restarted", worker_before=before, worker_after=after, preserved_approval=True)
        save(state)
    inspect_ui(state, "pending-after-restart")
    print("Candidate worker restarted; the same approval remains pending and undispatched.")


def approve(state):
    if state["stage"] != "restarted":
        raise RuntimeError("Verify restart recovery before approving")
    with client() as http:
        exact_pending(http, state)
    inspect_ui(state, "ui-approval-click", approve=True)
    state["stage"] = "approved"
    save(state)
    print("The exact harmless command was approved through the real UI.")


def finish(state):
    if state["stage"] != "approved":
        raise RuntimeError("No approval has been sent")
    with client() as http:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            run = get(http, f"/api/v1/runs/{state['run_id']}")
            if run["status"] in TERMINAL | {"pending_review"}:
                break
            if run["status"] == "awaiting_approval":
                approvals = get(http, f"/api/v1/runs/{state['run_id']}/approvals")
                if any(item["status"] == "pending" for item in approvals):
                    raise RuntimeError("A second approval was requested; do not approve extra work")
            time.sleep(1)
        assert run["status"] == "completed", "Acceptance Run did not complete; no retry is permitted"
        tools = get(http, f"/api/v1/runs/{state['run_id']}/tools")
        assert (
            len(tools) == 1 and tools[0]["id"] == state["operation_id"] and tools[0]["status"] == "completed"
        )
        assert "M4_APPROVAL_OK" in json.dumps(tools[0]["result"])
        evidence = get(http, f"/api/v1/runs/{state['run_id']}/artifacts")
        assert evidence
        post(http, f"/api/v1/approvals/{state['approval_id']}/decision", state["decision_body"])
        time.sleep(2)
        duplicate = get(http, f"/api/v1/runs/{state['run_id']}/tools")
        assert duplicate == tools
        budget = get(http, f"/api/v1/runs/{state['run_id']}/budget")
        state.update(
            stage="completed",
            final_tools=tools,
            artifacts=evidence,
            budget=budget,
            duplicate_approval_no_reexecution=True,
        )
        save(state)
    inspect_ui(state, "completed-overview")
    print(
        json.dumps(
            {
                "stage": "completed",
                "run_id": state["run_id"],
                "command_executions": 1,
                "artifact_count": len(evidence),
                "duplicate_approval_no_reexecution": True,
            }
        )
    )


def main():
    global OUTPUT, STATE, COOKIE_FILE
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "pending", "restart", "approve", "finish"))
    parser.add_argument("--attempt", default="approval-ui")
    args = parser.parse_args()
    if not re.fullmatch(r"approval-ui(?:-[a-z0-9-]+)?", args.attempt):
        parser.error("Use an approval-ui-prefixed local evidence directory name")
    OUTPUT = PRIVATE / args.attempt
    STATE = OUTPUT / "state.json"
    COOKIE_FILE = PRIVATE / "secrets" / (args.attempt + "-session.json")
    action = args.action
    if action == "start":
        start()
    else:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        {"pending": pending, "restart": restart, "approve": approve, "finish": finish}[action](state)


if __name__ == "__main__":
    main()
