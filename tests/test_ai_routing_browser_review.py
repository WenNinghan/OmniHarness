"""Opt-in isolated browser integration; no external provider traffic."""

import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import select
from test_postgres import postgres_environment  # noqa: F401

import omniharness.ai_routing as routing
from omniharness.api import create_app
from omniharness.checkpoint_fencing import initialize_checkpoint_schema
from omniharness.models import Conversation, Run
from omniharness.security import create_pairing_code
from omniharness.stage_contract import STAGE_KEYS


@pytest.mark.skipif(os.environ.get("OMNI_BROWSER_E2E") != "1", reason="Explicit browser test opt-in required")
def test_browser_planner_and_run_scope(request, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    chrome = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright/chromium-1243/chrome-win64/chrome.exe"
    if not chrome.is_file():
        pytest.skip("Dedicated local Chromium is unavailable")
    static_dir = Path(os.environ["OMNI_UI_BUILD_DIR"])
    assert (static_dir / "index.html").is_file()
    settings, engine, factory, _ = request.getfixturevalue("postgres_environment")
    settings.enable_agent = True
    settings.static_dir = static_dir
    settings.checkpoint_schema = f"routing_browser_{uuid4().hex}"
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
        runner.run(initialize_checkpoint_schema(os.environ["OMNI_TEST_POSTGRES_URL"], schema=settings.checkpoint_schema))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings.allowed_origins = f"http://127.0.0.1:{port},http://testserver"
    app = create_app(settings, engine)
    with TestClient(app, headers={"X-Omni-Request": "1"}) as client:
        assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
        model_ids = []
        for name in ("Browser Strong", "Browser Cheap"):
            response = client.post("/api/v1/model-configs", json={"name": name,
                "adapter_type": "openai-compatible", "model_name": name.lower().replace(" ", "-"),
                "base_url": "http://127.0.0.1:11434/v1"})
            assert response.status_code == 201
            model_ids.append(response.json()["id"])
        mock = client.post("/api/v1/model-configs", json={"name": "Browser Demo",
                           "adapter_type": "mock", "model_name": "browser-demo"})
        assert mock.status_code == 201
        mock_id = mock.json()["id"]
        original_bindings = client.get("/api/v1/stage-bindings").json()
    calls = []
    stale_started, release_stale = threading.Event(), threading.Event()

    async def fake_planner(config, messages):
        payload = json.loads(messages[-1].content)
        assert config.base_url == "http://127.0.0.1:11434/v1"
        assert mock_id not in {item["id"] for item in payload["candidates"]}
        calls.append(payload)
        if payload["task"] == "新会话不继承草案":
            stale_started.set()
            await asyncio.to_thread(release_stale.wait, 10)
        return AIMessage(content=json.dumps({"stages": [
            {"stage": stage, "model_config_id": model_ids[0], "reason": "synthetic UI recommendation"}
            for stage in STAGE_KEYS
        ]}))

    monkeypatch.setattr(routing, "invoke_planner", fake_planner)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.05)
    assert server.started
    try:
        with sync_api.sync_playwright() as browser_api:
            browser = browser_api.chromium.launch(headless=True, executable_path=str(chrome))
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script(f"localStorage.setItem('omni.model', {json.dumps(model_ids[0])})")
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.locator("#pair-code").fill(create_pairing_code(factory))
            page.get_by_role("button", name="连接工作台").click()
            page.get_by_text("当前默认模型", exact=True).wait_for()
            page.get_by_role("textbox", name="消息内容").fill("浏览器测试：计算 6 乘 7")
            page.get_by_role("button", name="AI 规划模型").click()
            panel = page.get_by_label("AI 模型规划")
            panel.get_by_text("http://127.0.0.1:11434/v1/chat/completions", exact=True).wait_for()
            assert len(calls) == 0
            assert "Browser Demo" not in panel.locator("#routing-planner-model").inner_text()
            panel.get_by_role("button", name="生成规划", exact=True).click()
            panel.get_by_text("synthetic UI recommendation").first.wait_for()
            assert len(calls) == 1
            page.screenshot(path=".omniharness/routing-browser-review-desktop.png")
            panel.get_by_label("审查阶段模型").click()
            page.get_by_role("listbox", name="可用模型").get_by_role("option", name="Browser Cheap", exact=False).click()
            panel.get_by_role("button", name="用于本次任务").click()
            panel.get_by_role("button", name="关闭模型规划").click()
            assert page.get_by_label("运行模式").input_value() == "agent"
            page.get_by_role("button", name="发送消息").click()
            deadline = time.monotonic() + 10
            created = None
            while time.monotonic() < deadline:
                with factory() as db:
                    conversation = db.scalar(select(Conversation).where(Conversation.title == "浏览器测试：计算 6 乘 7"))
                    created = db.scalar(select(Run).where(Run.conversation_id == conversation.id)) if conversation else None
                    if created:
                        roles = created.config_snapshot["agent"]["roles"]
                        assert roles["review"]["model"]["id"] == model_ids[1]
                        assert roles["think"]["model"]["id"] == model_ids[0]
                        break
                time.sleep(.1)
            assert created is not None
            assert len(calls) == 1
            page.get_by_role("button", name="停止生成").wait_for(timeout=10000)
            # Reload preserves model selections per existing conversation, but
            # not user text or automatic acceptance of a later task.
            page.wait_for_function("sessionStorage.getItem('omni.routing.plans.v1')?.includes('" + created.conversation_id + "')")
            cached = page.evaluate("sessionStorage.getItem('omni.routing.plans.v1')")
            assert "浏览器测试" not in cached and "synthetic UI recommendation" not in cached
            page.reload(wait_until="networkidle")
            page.get_by_role("textbox", name="消息内容").fill("后续任务继续使用这个模型分配")
            page.get_by_role("button", name="AI 规划模型").click()
            restored = page.get_by_label("AI 模型规划")
            assert "Browser Cheap" in restored.get_by_label("审查阶段模型").inner_text()
            restored.get_by_role("button", name="沿用方案到当前问题").click()
            assert len(calls) == 1
            with factory() as db:
                unchanged = db.get(Run, created.id)
                assert unchanged.config_snapshot["agent"]["roles"]["review"]["model"]["id"] == model_ids[1]
            restored.get_by_role("button", name="关闭模型规划").click()
            page.get_by_role("button", name="新建对话", exact=False).click()
            page.get_by_role("textbox", name="消息内容").fill("新会话不继承草案")
            page.get_by_role("button", name="AI 规划模型").click()
            page.get_by_label("AI 模型规划").get_by_text("方案会显示在这里").wait_for()
            assert len(calls) == 1
            # Closing the panel and changing tasks while a paid request is in
            # flight must not apply its eventual result to the new task.
            new_panel = page.get_by_label("AI 模型规划")
            new_panel.get_by_role("button", name="生成规划", exact=True).click()
            assert stale_started.wait(10)
            new_panel.get_by_role("button", name="关闭模型规划").click()
            page.get_by_role("textbox", name="消息内容").fill("问题已改变")
            page.get_by_role("button", name="AI 规划模型").click()
            release_stale.set()
            page.wait_for_timeout(400)
            page.get_by_label("AI 模型规划").get_by_text("方案会显示在这里").wait_for()
            assert len(calls) == 2
            page.set_viewport_size({"width": 390, "height": 844})
            assert not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
            page.screenshot(path=".omniharness/routing-browser-review-mobile.png")
            with factory() as db:
                stored = db.get(Run, created.id)
                assert stored.config_snapshot["agent"]["roles"]["review"]["model"]["id"] == model_ids[1]
            with TestClient(app, headers={"X-Omni-Request": "1"}) as client:
                assert client.post("/api/v1/auth/pair", json={"code": create_pairing_code(factory)}).status_code == 200
                assert client.get("/api/v1/stage-bindings").json() == original_bindings
            browser.close()
    finally:
        release_stale.set()
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
