"""Opt-in browser acceptance for persisted task/resource/cache observations."""
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from test_m4_api import workspace  # noqa: F401
from test_m4_reconciliation import unknown  # noqa: F401

from omniharness.api import create_app
from omniharness.models import Run, ToolOperation
from omniharness.security import create_pairing_code
from omniharness.services import add_event


@pytest.mark.skipif(os.environ.get("OMNI_BROWSER_E2E") != "1", reason="Explicit browser opt-in required")
def test_unknown_task_resources_cache_and_stale_observation(environment, unknown):  # noqa: F811
    playwright = pytest.importorskip("playwright.sync_api")
    chrome = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright/chromium-1243/chrome-win64/chrome.exe"
    assert chrome.is_file(), "The existing acceptance Chromium must be available"
    settings, engine, factory, _ = environment
    run_id, operation_id, _, _ = unknown
    settings.static_dir = Path("web/dist").resolve()
    assert (settings.static_dir / "index.html").is_file()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings.allowed_origins = f"http://127.0.0.1:{port},http://testserver"
    with factory.begin() as db:
        run = db.get(Run, run_id)
        add_event(db, run, "agent.model_completed", {"usage_details": {
            "cache_read_input_tokens": 8, "cache_creation_input_tokens": 0}})
        add_event(db, run, "agent.model_completed", {"usage_details": {"cache_creation_input_tokens": 0}})
    server = uvicorn.Server(uvicorn.Config(create_app(settings, engine), host="127.0.0.1", port=port,
                                         log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    shots = Path(".omniharness/qa/creation-sol-observation")
    shots.mkdir(parents=True, exist_ok=True)
    errors, mutations = [], []
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=str(chrome))
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: mutations.append(request.url)
                    if request.method == "POST" and "/reconcile" in request.url else None)
            page.add_init_script("localStorage.setItem('omni.details','true')")
            page.goto(f"http://127.0.0.1:{port}", wait_until="domcontentloaded")
            page.locator("#pair-code").fill(create_pairing_code(factory))
            page.get_by_role("button", name="连接工作台").click()
            page.get_by_role("navigation", name="最近对话").get_by_role("button", name="Explain a checkpoint", exact=True).click()
            panel = page.locator("section").filter(has=page.get_by_role("heading", name="任务与资源状态", exact=True))
            playwright.expect(panel).to_contain_text("结果或资源需要核对")
            playwright.expect(panel).to_contain_text("占用工具槽 1")
            playwright.expect(panel).to_contain_text("工作区租约仍持有")
            panel.locator("summary").filter(has_text="上下文处理与缓存").click()
            playwright.expect(panel).to_contain_text("总量未知")
            playwright.expect(panel).to_contain_text("0 tokens（供应商返回）")
            page.screenshot(path=str(shots / "desktop.png"))

            def unavailable(route):
                route.fulfill(status=503, content_type="application/json", body=json.dumps({
                    "detail": {"code": "TEST_UNAVAILABLE", "message": "隔离状态服务暂不可用"}}, ensure_ascii=False))

            pattern = f"**/runs/{run_id}/observation"
            page.route(pattern, unavailable)
            playwright.expect(panel.get_by_role("alert")).to_contain_text("可能已过期", timeout=12000)
            playwright.expect(panel).to_contain_text("占用工具槽 1")
            page.screenshot(path=str(shots / "stale.png"))
            page.unroute(pattern, unavailable)
            panel.get_by_role("button", name="刷新", exact=True).click()
            playwright.expect(panel.get_by_role("alert")).to_have_count(0)
            playwright.expect(panel).to_contain_text("结果或资源需要核对")
            page.set_viewport_size({"width": 390, "height": 844})
            # Wait for the responsive sidebar's exit transition; otherwise a
            # screenshot can capture it covering half the detail drawer.
            playwright.expect(page.locator(".sidebar")).not_to_be_in_viewport()
            panel.scroll_into_view_if_needed()
            playwright.expect(panel).to_be_visible()
            bounds = panel.bounding_box()
            assert bounds is not None and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(shots / "mobile.png"))
            assert not errors and not mutations, {"page_errors": errors, "unexpected_mutations": mutations}
            browser.close()
        with factory() as db:
            operation = db.get(ToolOperation, operation_id)
            assert operation.status == "uncertain" and operation.slot_claimed
        (shots / "result.json").write_text(json.dumps({"passed": True, "unknown_resources_visible": True,
            "partial_cache_distinct_from_zero": True, "stale_read_visible": True, "refresh_recovers": True,
            "mobile_no_horizontal_overflow": True, "page_errors": errors, "reconciliation_requests": mutations}),
            encoding="utf-8")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
