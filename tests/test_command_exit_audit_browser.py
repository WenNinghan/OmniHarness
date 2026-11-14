"""Opt-in browser check of persisted and replayed command-result warnings."""
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from conftest import create_chat
from sqlalchemy import select

from omniharness.api import create_app
from omniharness.models import Message, Run
from omniharness.security import create_pairing_code
from omniharness.services import add_event


@pytest.mark.skipif(os.environ.get("OMNI_BROWSER_E2E") != "1", reason="Explicit browser opt-in required")
@pytest.mark.parametrize("audit_kind", ["conflict", "incomplete"])
def test_command_audit_notice_survives_reload_and_mobile(environment, audit_kind):
    playwright = pytest.importorskip("playwright.sync_api")
    chrome = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright/chromium-1243/chrome-win64/chrome.exe"
    assert chrome.is_file()
    settings, engine, factory, client = environment
    _, _, created = create_chat(client)
    run_id = created["run_id"]
    artifact = "61b78660-6971-4d7f-99d4-c3d7c975d0b3"
    if audit_kind == "conflict":
        audit = {"status": "conflict", "truncated": False,
                 "conflicts": [{"artifact_id": artifact, "operation_id": "a" * 64,
                                "claimed_exit_code": 0, "actual_exit_code": 1, "line": 4}]}
    else:
        audit = {"status": "incomplete", "truncated": True, "conflicts": []}
    answer = "模型原始答复：测试退出码 0。"
    metadata = {"output_status": "complete", "command_result_audit": audit}
    with factory.begin() as db:
        run = db.get(Run, run_id)
        message = db.scalar(select(Message).where(Message.run_id == run_id, Message.role == "assistant"))
        run.status = message.status = "completed"
        message.content = answer
        message.output_metadata = metadata
        add_event(db, run, "run.completed", {"message_id": message.id, "output_metadata": metadata})
    settings.static_dir = Path("web/dist").resolve()
    assert (settings.static_dir / "index.html").is_file()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings.allowed_origins = f"http://127.0.0.1:{port},http://testserver"
    server = uvicorn.Server(uvicorn.Config(create_app(settings, engine), host="127.0.0.1", port=port,
                                          log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.05)
    assert server.started
    shots = Path(".omniharness/qa/command-exit-audit")
    shots.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with playwright.sync_playwright() as browser_runtime:
            browser = browser_runtime.chromium.launch(executable_path=str(chrome), headless=True)
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            # Capture writes in this isolated page; never access the host clipboard.
            context.add_init_script("""Object.defineProperty(navigator, 'clipboard', {
                configurable: true, value: {writeText: async text => {window.__copiedAnswer = text}}
            })""")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}", wait_until="domcontentloaded")
            page.locator("#pair-code").fill(create_pairing_code(factory))
            page.get_by_role("button", name="连接工作台").click()
            page.get_by_role("navigation", name="最近对话").get_by_role("button", name="Explain a checkpoint", exact=True).click()
            heading = "命令退出码存在冲突" if audit_kind == "conflict" else "命令退出码核对未完成"
            warning = page.get_by_role("alert" if audit_kind == "conflict" else "status").filter(has_text=heading)
            playwright.expect(warning).to_be_visible()
            if audit_kind == "conflict":
                playwright.expect(warning).to_contain_text(artifact)
                playwright.expect(warning).to_contain_text("0")
                playwright.expect(warning).to_contain_text("1")
            else:
                playwright.expect(warning).to_contain_text("核对范围")
                playwright.expect(warning).not_to_contain_text("存在冲突")
                playwright.expect(page.get_by_role("alert").filter(has_text="命令退出码存在冲突")).to_have_count(0)
            playwright.expect(page.get_by_text(answer)).to_be_visible()
            page.get_by_role("button", name="复制回答", exact=True).click()
            copied = page.evaluate("window.__copiedAnswer")
            assert isinstance(copied, str) and copied.endswith("\n\n" + answer)
            if audit_kind == "conflict":
                assert artifact in copied and "不一致" in copied
            else:
                assert "核对未完成" in copied and "存在冲突" not in copied
            page.reload(wait_until="domcontentloaded")
            page.get_by_role("navigation", name="最近对话").get_by_role("button", name="Explain a checkpoint", exact=True).click()
            playwright.expect(warning).to_be_visible()
            page.screenshot(path=str(shots / f"desktop-{audit_kind}-t1.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            playwright.expect(page.locator(".sidebar")).not_to_be_in_viewport()
            toggle = page.get_by_role("button", name="运行详情", exact=True)
            if toggle.get_attribute("aria-expanded") == "true":
                page.get_by_role("button", name="收起运行详情").click()
            playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
            warning.scroll_into_view_if_needed()
            playwright.expect(warning).to_be_visible()
            assert warning.evaluate("el => { const r=el.getBoundingClientRect(); "
                "const p=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2); return p && el.contains(p) }")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(shots / f"mobile-{audit_kind}-t1.png"))
            assert not errors
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
