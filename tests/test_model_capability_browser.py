"""Opt-in browser regression for model capability freshness; all API calls are mocked."""

import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNI_BROWSER_E2E") != "1", reason="Explicit browser opt-in required"
)


@pytest.fixture
def capability_workbench():
    playwright = pytest.importorskip("playwright.sync_api")
    chrome = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright/chromium-1243/chrome-win64/chrome.exe"
    assert chrome.is_file(), "The existing acceptance Chromium must be available"
    static = Path("web/dist").resolve()
    assert (static / "index.html").is_file(), "Build the current frontend before browser tests"

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(static)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {
        "model": {
            "id": "capability-fixture", "name": "Browser fixture", "adapter_type": "openai-compatible",
            "model_name": "before-edit", "base_url": "http://localhost:7777/v1", "enabled": True,
            "key_configured": False, "version": 1, "provider_version": 1, "parameters": {},
            "timeout_seconds": 120, "max_output_tokens": 100000, "capabilities": {}, "tested_at": None,
            "created_at": "2026-11-15T00:00:00Z",
        },
        "defer": None, "pending": None, "defer_next_models": False, "pending_models": None,
        "test_requests": [], "unexpected": [],
    }

    def route_api(route):
        path, method = urlparse(route.request.url).path, route.request.method
        if path == "/api/v1/bootstrap":
            data = {"version": "browser-fixture", "authenticated": True, "pairing_required": False,
                    "demo_enabled": False, "stage": "M4", "agent_available": False}
        elif path == "/api/v1/model-configs/capability-fixture/test" and method == "POST":
            probes = route.request.post_data_json["probes"]
            state["test_requests"].append(probes)
            results = {probe: {"status": "supported", "message": f"fixture-v1-{probe}"} for probe in probes}
            data = {"model_config_id": "capability-fixture", "results": results,
                    "tested_at": "2026-11-15T00:01:00Z"}
            # Successful test writes occur before its response reaches the browser.
            # A later edit can therefore clear these results before delivery.
            if state["defer"] != "conflict":
                state["model"]["capabilities"] = {**state["model"]["capabilities"], **results}
                state["model"]["tested_at"] = data["tested_at"]
            if state["defer"]:
                state["pending"] = (route, data)
                return
        elif path == "/api/v1/model-configs/capability-fixture" and method == "PUT":
            # Match save_model: preserve the ID, increment versions, clear probe evidence.
            state["model"] = {**state["model"], **route.request.post_data_json,
                              "version": 2, "provider_version": 2, "capabilities": {}, "tested_at": None}
            data = state["model"]
        elif path == "/api/v1/model-configs" and method == "GET":
            data = [state["model"]]
            if state["defer_next_models"]:
                state["defer_next_models"] = False
                # Freeze the response as it existed when the request was made.
                state["pending_models"] = (route, json.loads(json.dumps(data)))
                return
        elif path in {"/api/v1/conversations", "/api/v1/workspaces"} and method == "GET":
            data = []
        elif path == "/api/v1/tools/status" and method == "GET":
            data = {"enabled": False, "broker_ready": False}
        else:
            state["unexpected"].append((method, path))
            route.fulfill(status=404, content_type="application/json", body="{}")
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=str(chrome))
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("**/api/v1/**", route_api)
                page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="networkidle")
                page.locator(".nav-link").filter(has_text="模型连接").click()
                page.get_by_role("button", name="测试连接", exact=True).wait_for()
                yield page, state, playwright.expect
                assert not errors and not state["unexpected"], {"page_errors": errors,
                                                               "unexpected_api": state["unexpected"]}
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def edit_model(page):
    page.get_by_role("button", name="编辑 Browser fixture", exact=True).click()
    page.locator('input[name="model_name"]').fill("after-edit")
    page.get_by_role("button", name="保存修改", exact=True).click()
    page.locator(".model-identity code").filter(has_text="after-edit").wait_for()


def assert_untested(page, state, expect):
    assert state["model"]["version"] == 2
    assert state["model"]["capabilities"] == {} and state["model"]["tested_at"] is None
    expect(page.locator(".probe-success")).to_have_count(0)
    expect(page.locator(".probe-item").filter(has_text="未测试")).to_have_count(4)
    expect(page.get_by_text("fixture-v1-chat", exact=True)).to_have_count(0)


def test_edit_clears_previously_tested_capabilities(capability_workbench):
    page, state, expect = capability_workbench
    page.get_by_role("button", name="测试连接", exact=True).click()
    expect(page.locator(".probe-success")).to_have_count(4)
    edit_model(page)
    assert_untested(page, state, expect)


def test_delayed_success_cannot_restore_capabilities_after_edit(capability_workbench):
    page, state, expect = capability_workbench
    state["defer"] = "success"
    page.get_by_role("button", name="测试连接", exact=True).click()
    edit_model(page)
    route, data = state["pending"]
    route.fulfill(status=200, content_type="application/json", body=json.dumps(data))
    expect(page.get_by_role("button", name="测试连接", exact=True)).to_be_enabled()
    assert_untested(page, state, expect)


def test_configuration_conflict_does_not_fill_probe_results(capability_workbench):
    page, state, expect = capability_workbench
    state["defer"] = "conflict"
    page.get_by_role("button", name="测试连接", exact=True).click()
    edit_model(page)
    route, _ = state["pending"]
    route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": {
        "code": "CONFIG_CHANGED", "message": "测试期间配置已改变，请重新测试。"}}, ensure_ascii=False))
    expect(page.get_by_role("alert")).to_contain_text("测试期间配置已改变")
    expect(page.get_by_role("button", name="测试连接", exact=True)).to_be_enabled()
    assert_untested(page, state, expect)


def test_delayed_model_refresh_cannot_restore_pre_edit_configuration(capability_workbench):
    page, state, expect = capability_workbench
    state["defer_next_models"] = True
    with page.expect_request(lambda request: request.method == "GET"
                             and urlparse(request.url).path == "/api/v1/model-configs"):
        page.get_by_role("button", name="测试连接", exact=True).click()
    edit_model(page)
    assert state["pending_models"] is not None
    route, captured = state["pending_models"]
    assert captured[0]["version"] == 1 and len(captured[0]["capabilities"]) == 4
    route.fulfill(status=200, content_type="application/json", body=json.dumps(captured))
    expect(page.get_by_role("button", name="测试连接", exact=True)).to_be_enabled()
    expect(page.locator(".model-identity code")).to_have_text("after-edit")
    assert_untested(page, state, expect)


def test_partial_probe_keeps_other_current_capabilities(capability_workbench):
    page, state, expect = capability_workbench
    page.get_by_role("button", name="测试连接", exact=True).click()
    expect(page.locator(".probe-success")).to_have_count(4)
    page.get_by_role("button", name="测试摘要模式", exact=True).click()
    expect(page.get_by_text("fixture-v1-summary_non_thinking", exact=True)).to_be_visible()
    expect(page.locator(".probe-success")).to_have_count(4)
    assert state["test_requests"] == [["chat", "streaming", "tools", "structured"], ["summary_non_thinking"]]
    assert len(state["model"]["capabilities"]) == 5
