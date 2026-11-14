"""Read-only candidate UI inspection. Auth is the only permitted mutation."""

from __future__ import annotations

import json
import os
from pathlib import Path

from m4_preview_deploy import PRIVATE, PROJECT, wsl
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:18792"
CHROME = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright/chromium-1243/chrome-win64/chrome.exe"


def main():
    output = PRIVATE / "ui"
    output.mkdir(exist_ok=True)
    code = wsl("docker", "exec", PROJECT + "-api-1", "omniharness", "auth-code").decode().splitlines()[-1]
    report = {"screenshots": [], "page_errors": [], "blocked_mutations": [], "model_calls": 0}
    with sync_playwright() as play:
        browser = play.chromium.launch(headless=True, executable_path=str(CHROME))
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        response = context.request.post(
            BASE + "/api/v1/auth/pair", data={"code": code}, headers={"X-Omni-Request": "1", "Origin": BASE}
        )
        assert response.status == 200
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
        page.locator(".workspace-trigger").wait_for()

        def shot(name):
            page.screenshot(path=str(output / name), full_page=True, animations="disabled")
            report["screenshots"].append(str(output / name))

        shot("01-desktop.png")
        page.locator(".workspace-trigger").click()
        page.get_by_role("dialog", name="选择工作项目").wait_for()
        page.get_by_role("button", name="登记新项目").wait_for(state="visible")
        assert page.locator(".workspace-options button").count() >= 2
        shot("02-workspaces.png")
        page.get_by_role("button", name="登记新项目").click()
        page.locator(".workspace-form").wait_for()
        assert page.get_by_role("checkbox", name="允许修改原项目").is_checked() is False
        assert page.get_by_role("checkbox", name="每条命令由我审批").is_checked() is False
        shot("03-register-desktop.png")
        page.locator(".workspace-manager").evaluate("el => { el.scrollTop = el.scrollHeight }")
        shot("04-permissions-desktop.png")
        page.set_viewport_size({"width": 390, "height": 844})
        shot("05-permissions-mobile.png")
        report["mobile_document_overflow"] = page.evaluate(
            "document.documentElement.scrollWidth > innerWidth"
        )
        report["mobile_layers"] = page.evaluate(
            """() => ['.modal-backdrop','.modal','.run-panel','.workspace-manager'].map(selector => {const el=document.querySelector(selector), s=getComputedStyle(el); return {selector,zIndex:s.zIndex,padding:s.padding,position:s.position,rect:el.getBoundingClientRect().toJSON()}})"""
        )
        try:
            page.get_by_role("button", name="关闭", exact=True).click(timeout=2000)
            report["mobile_modal_close"] = "PASS"
        except PlaywrightTimeout:
            report["mobile_modal_close"] = "FAIL: run-panel intercepts the modal close button"
            page.keyboard.press("Escape")
        shot("06-mobile.png")
        report["workspace_controls_visible"] = page.locator(".workspace-trigger").is_visible()
        report["tool_activity"] = "UNVERIFIED: no real workspace Run in this fresh preview database"
        assert not report["page_errors"]
        assert not report["blocked_mutations"]
        browser.close()
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
