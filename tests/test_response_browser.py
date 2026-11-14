"""Real browser + local API/Worker checks; all model output is synthetic, no external calls."""

import asyncio
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import select

from omniharness.api import create_app
from omniharness.models import Run
from omniharness.providers import ModelDelta, ProviderError
from omniharness.security import create_pairing_code
from omniharness.worker import Worker


@pytest.mark.skipif(os.environ.get("OMNI_BROWSER_E2E") != "1", reason="Explicit browser opt-in required")
def test_response_channels_history_and_model_picker(environment):
    playwright = pytest.importorskip("playwright.sync_api")
    chrome = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright/chromium-1243/chrome-win64/chrome.exe"
    if not chrome.is_file():
        pytest.skip("Local Chromium unavailable")
    settings, engine, factory, client = environment
    settings.static_dir = Path(os.environ["OMNI_UI_BUILD_DIR"])
    assert (settings.static_dir / "index.html").is_file()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings.allowed_origins = f"http://127.0.0.1:{port},http://testserver"
    names = [
        "deepseek-flash", "deepseek-v4-pro", "glm-5", "glm-5.1", "glm-5.2",
        "glm-5.2-fast-preview", "glm-5.3", "glm-5.3-flash", "glm-5.3-prime",
        "hy3", "hy4", "kimi-k2.6", "kimi-k2.7", "kimi-k2.7-code", "kimi-k2.8-preview",
        "kimi-k3", "kimi-k3.1-pro", "mimo-v2.5-pro", "MiniMax-M2.5", "qwen3.5-plus",
        "qwen3.6-plus", "qwen3.7-max", "qwen3.8-flash", "qwen3.8-max", "qwen3.8-max-0902",
    ]
    models = []
    for name in names:
        response = client.post("/api/v1/model-configs", json={"name": name, "adapter_type": "openai-compatible",
                              "model_name": name, "base_url": "https://e-flowcode.cc/v1"})
        assert response.status_code == 201
        assert response.json()["max_output_tokens"] == 100000
        models.append(response.json())
    server = uvicorn.Server(uvicorn.Config(create_app(settings, engine), host="127.0.0.1", port=port,
                                         log_level="error", access_log=False))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    deadline = time.monotonic() + 15
    while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.05)
    assert server.started
    calls = []

    async def stream(config, history):
        calls.append(history)
        yield ModelDelta(reasoning="这是一段合成的思考测试内容，与正式回答分开保存。")
        await asyncio.sleep(.15)
        if len(calls) == 1:
            yield ModelDelta(finish_reason="length", output_status="truncated")
            raise ProviderError("OUTPUT_TRUNCATED", "达到输出上限，尚未给出正式回答。")
        yield ModelDelta(text="正式回答：通道分离验证通过。")
        yield ModelDelta(finish_reason="stop", output_status="complete")

    worker = Worker(settings, engine, stream_fn=stream)
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=str(chrome))
            context = browser.new_context(viewport={"width": 1440, "height": 960}, permissions=["clipboard-read", "clipboard-write"])
            page = context.new_page()
            # Price snapshots have an explicit seven-day validity window.
            page.clock.set_fixed_time("2026-11-08T14:00:00Z")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script(f"localStorage.setItem('omni.model', {json.dumps(models[0]['id'])}); localStorage.setItem('omni.details','false')")
            page.goto(f"http://127.0.0.1:{port}", wait_until="networkidle")
            page.locator("#pair-code").fill(create_pairing_code(factory))
            page.get_by_role("button", name="连接工作台").click()
            page.get_by_label("选择模型", exact=True).wait_for()
            playwright.expect(page.get_by_label("选择模型", exact=True)).to_contain_text("100K")
            playwright.expect(page.get_by_label("选择模型", exact=True).locator("small")).to_be_visible()
            page.get_by_label("选择模型", exact=True).click()
            assert page.get_by_role("listbox", name="可用模型").get_by_role("option").count() == 25
            assert page.locator(".model-picker-price.is-unknown").count() == 0
            qwen_price = page.get_by_role("button", name="查看 Qwen 3.8 Max 0902 的价格依据", exact=True)
            assert qwen_price.inner_text() == "≈4×"
            qwen_price.click()
            assert "最高" in page.locator(".model-picker-price-fields").inner_text()
            assert "e-flowcode.cc/api/pricing" in page.locator(".model-picker-price-fields").inner_text()
            search = page.get_by_role("combobox", name="搜索模型")
            search.fill("kimi")
            option = page.get_by_role("listbox", name="可用模型").get_by_role("option", name="Kimi K3.1 Pro", exact=False)
            option.wait_for()
            assert page.get_by_role("listbox", name="可用模型").get_by_role("option").count() == 6
            search.fill("kimi k3.1")
            assert page.get_by_role("listbox", name="可用模型").get_by_role("option").count() == 1
            search.press("ArrowDown")
            search.press("Enter")
            assert "Kimi K3.1 Pro" in page.get_by_label("选择模型", exact=True).inner_text()
            page.get_by_label("选择模型", exact=True).click()
            page.get_by_role("combobox", name="搜索模型").fill("")
            shots = Path(".omniharness/output-limits-ui")
            shots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shots / "model-picker-desktop.png"))
            page.keyboard.press("Escape")
            assert page.get_by_label("选择模型", exact=True).evaluate("el => document.activeElement === el")
            for i in range(2):
                page.get_by_role("textbox", name="消息内容").fill(f"合成测试 {i + 1}")
                page.get_by_role("button", name="发送消息", exact=True).click()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    with factory() as db:
                        if db.scalar(select(Run.id).where(Run.status == "queued")):
                            break
                    page.wait_for_timeout(50)
                # Playwright's sync bridge has an active event loop on this thread.
                with ThreadPoolExecutor(max_workers=1) as executor:
                    assert executor.submit(asyncio.run, worker.run_once()).result(timeout=20)
                page.get_by_role("button", name="发送消息", exact=True).wait_for()
                if i == 0:
                    page.get_by_text("达到输出上限，尚未给出正式回答。", exact=True).first.wait_for()
                    assert page.locator(".reasoning-disclosure").count() == 1
                    assert not page.locator(".reasoning-disclosure").evaluate("el => el.open")
            page.get_by_text("正式回答：通道分离验证通过。", exact=True).wait_for()
            page.reload(wait_until="networkidle")
            page.get_by_text("正式回答：通道分离验证通过。", exact=True).wait_for()
            assert page.locator(".message-output-notice").count() == 1
            assert page.locator(".reasoning-disclosure").count() == 2
            assert not page.locator(".reasoning-disclosure").last.evaluate("el => el.open")
            page.locator(".reasoning-disclosure").last.locator("summary").click()
            assert page.locator(".reasoning-body").last.is_visible()
            page.get_by_role("button", name="复制回答", exact=True).click()
            assert page.evaluate("navigator.clipboard.readText()") == "正式回答：通道分离验证通过。"
            page.screenshot(path=str(shots / "message-channels-desktop.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_label("选择模型", exact=True).click()
            page.screenshot(path=str(shots / "model-picker-mobile.png"))
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.get_by_role("button", name="关闭模型选择", exact=True).click()
            page.set_viewport_size({"width": 1440, "height": 960})
            page.get_by_role("button", name="模型连接", exact=False).click()
            page.get_by_role("button", name="添加模型", exact=True).click()
            page.get_by_role("button", name="高级参数", exact=False).click()
            limit = page.locator('input[name="max_output_tokens"]')
            assert limit.input_value() == "100000"
            assert limit.get_attribute("min") == "16"
            assert limit.get_attribute("max") == "131072"
            page.screenshot(path=str(shots / "new-model-default.png"))
            assert not errors
            assert all(item["role"] == "user" for item in calls[1])
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
