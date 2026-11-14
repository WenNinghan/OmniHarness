"""Inspect an existing real Run without sending messages or deciding approvals."""

from __future__ import annotations

import argparse
import json

from m4_preview_deploy import PRIVATE, PROJECT, wsl
from m4_preview_ui import BASE, CHROME
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    args = parser.parse_args()
    code = wsl("docker", "exec", PROJECT + "-api-1", "omniharness", "auth-code").decode().splitlines()[-1]
    output = PRIVATE / "ui-runs" / args.run_id
    output.mkdir(parents=True, exist_ok=True)
    report = {"run_id": args.run_id, "page_errors": [], "blocked_mutations": [], "screenshots": []}
    with sync_playwright() as play:
        browser = play.chromium.launch(headless=True, executable_path=str(CHROME))
        context = browser.new_context(viewport={"width": 1560, "height": 1040}, device_scale_factor=1)
        paired = context.request.post(
            BASE + "/api/v1/auth/pair", data={"code": code}, headers={"X-Omni-Request": "1", "Origin": BASE}
        )
        assert paired.status == 200
        response = context.request.get(BASE + "/api/v1/runs/" + args.run_id)
        assert response.status == 200
        run = response.json()
        report["run_status"] = run["status"]
        report["conversation_id"] = run["conversation_id"]
        context.add_init_script(
            "localStorage.setItem('omni.details', 'true'); localStorage.setItem('omni.conversation', "
            + json.dumps(run["conversation_id"])
            + ");"
        )
        page = context.new_page()
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))

        def read_only(route):
            if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                report["blocked_mutations"].append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.route("**/api/**", read_only)
        page.goto(BASE, wait_until="networkidle")
        activity = page.locator(".tool-activity")
        activity.wait_for()
        current_tools = context.request.get(BASE + "/api/v1/runs/" + args.run_id + "/tools")
        assert current_tools.status == 200
        if current_tools.json():
            page.wait_for_function(
                "document.querySelector('.tool-activity .tool-record,.tool-activity .approval-card') !== null"
            )

        def shot(name):
            page.screenshot(path=str(output / name), full_page=True, animations="disabled")
            report["screenshots"].append(str(output / name))

        shot("01-run-desktop.png")
        activity.scroll_into_view_if_needed()
        records = activity.locator(".tool-record")
        report["tool_record_count"] = records.count()
        report["pending_approval_count"] = activity.locator(".approval-card").count()
        report["artifact_count"] = activity.locator(".artifact-list>button").count()
        shot("02-tools-desktop.png")
        if records.count():
            records.first.locator("summary").click()
            shot("03-tool-detail.png")
        if report["artifact_count"]:
            activity.locator(".artifact-list>button").first.click()
            activity.locator(".artifact-preview").wait_for()
            activity.locator(".artifact-preview").scroll_into_view_if_needed()
            shot("04-artifact.png")
        page.set_viewport_size({"width": 390, "height": 844})
        activity.scroll_into_view_if_needed()
        shot("05-tools-mobile.png")
        report["mobile_overflow"] = page.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert not report["page_errors"] and not report["blocked_mutations"]
        browser.close()
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
