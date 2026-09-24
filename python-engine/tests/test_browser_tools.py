"""browser.py RPA 工具注册与 StubHub 行为测试。

核心意图:
- 11 个浏览器工具必须全部注册到本地 registry（对标 Go rpa_browser.go）
- StubHub 命令透传（method/params/client_id 完整到达 Hub）
- bind_hub 注入点可替换真实 Hub（Chrome Extension WebSocket）
- 无已连接客户端时 fail-loud（RuntimeError，不返回假结果）
- 参数校验: 缺 url/selector/text/tabId 时返回明确 error
"""
from __future__ import annotations

import pytest

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.tools import browser
from app.tools.registry import registry

# ── 注册完整性 ──────────────────────────────────────────────

EXPECTED_TOOLS = {
    "browser_navigate", "browser_click", "browser_type", "browser_read",
    "browser_screenshot", "browser_scroll", "browser_get_state",
    "browser_tab_list", "browser_tab_create", "browser_tab_switch",
    "browser_tab_close",
}


def test_all_11_browser_tools_registered():
    """对标 Go rpa_browser.go: 11 个工具一个不少。"""
    registered = {n for n in registry.list_names() if n.startswith("browser_")}
    assert EXPECTED_TOOLS <= registered, f"缺失: {EXPECTED_TOOLS - registered}"
    assert len(registered) == len(EXPECTED_TOOLS), f"多余: {registered - EXPECTED_TOOLS}"


def test_browser_tool_schemas_have_required_params():
    """必填参数（url/selector/text/tabId）必须在 schema.required 中声明。"""
    navigate = registry.get("browser_navigate")
    assert navigate is not None
    assert "url" in navigate.parameters.get("required", [])

    click = registry.get("browser_click")
    assert click is not None
    assert "selector" in click.parameters.get("required", [])

    switch = registry.get("browser_tab_switch")
    assert switch is not None
    # 参数名必须是 **snake_case**：handler 签名为 `browser_tab_switch(tab_id)`，
    # schema 若写 camelCase（曾经的 "tabId"）会让模型传了却收不到 ⇒ 调用必 TypeError。
    assert "tab_id" in switch.parameters.get("required", [])


# ── StubHub 命令透传 ────────────────────────────────────────


@pytest.fixture
def stub_hub(monkeypatch):
    """显式绑定 StubHub，验证命令透传（仅测试夹具；生产默认无 hub → fail-loud）。"""
    monkeypatch.setattr(browser, "_hub", browser.StubHub())


async def test_navigate_forwards_url_to_hub(stub_hub):
    result = await browser.browser_navigate("https://example.com")
    assert result["method"] == "browser_navigate"
    assert result["params"]["url"] == "https://example.com"
    assert result["status"] == "ok"


async def test_click_forwards_selector(stub_hub):
    result = await browser.browser_click("#submit-btn")
    assert result["method"] == "browser_click"
    assert result["params"]["selector"] == "#submit-btn"


async def test_type_forwards_selector_and_text(stub_hub):
    result = await browser.browser_type("#search", "hello world")
    assert result["params"]["selector"] == "#search"
    assert result["params"]["text"] == "hello world"


async def test_screenshot_defaults(stub_hub):
    result = await browser.browser_screenshot(full_page=True)
    assert result["method"] == "browser_screenshot"
    assert result["params"]["fullPage"] is True


async def test_scroll_params(stub_hub):
    result = await browser.browser_scroll(direction="up", amount=300)
    assert result["params"]["direction"] == "up"
    assert result["params"]["amount"] == 300


async def test_tab_id_injected_into_params(stub_hub):
    result = await browser.browser_navigate("https://example.com", tab_id=7)
    assert result["params"]["tabId"] == 7


# ── 参数校验 (fail-loud 语义) ───────────────────────────────

async def test_navigate_empty_url_rejected():
    result = await browser.browser_navigate("")
    assert "error" in result


async def test_click_empty_selector_rejected():
    result = await browser.browser_click("")
    assert "error" in result


async def test_type_empty_text_rejected():
    result = await browser.browser_type("#sel", "")
    assert "error" in result


async def test_tab_switch_invalid_id_rejected():
    assert "error" in await browser.browser_tab_switch(0)
    assert "error" in await browser.browser_tab_switch(-1)


# ── bind_hub 注入点 ─────────────────────────────────────────

class RecordingHub:
    """记录命令的真实 Hub 替身（模拟 Chrome Extension WebSocket）。"""

    def __init__(self):
        self.commands: list[tuple[str, str, dict]] = []

    def connected_client_ids(self) -> list[str]:
        return ["ext-client-1"]

    def exec_command(self, client_id: str, method: str, params: dict) -> dict:
        self.commands.append((client_id, method, dict(params)))
        return {"status": "ok", "real": True}


@pytest.fixture
def recording_hub(monkeypatch):
    hub = RecordingHub()
    monkeypatch.setattr(browser, "_hub", hub)
    return hub


async def test_bind_hub_replaces_stub(recording_hub):
    """bind_hub/_hub 替换后命令走真实 Hub。"""
    result = await browser.browser_navigate("https://real.example.com")
    assert result == {"status": "ok", "real": True}
    client_id, method, params = recording_hub.commands[0]
    assert client_id == "ext-client-1"
    assert method == "browser_navigate"
    assert params["url"] == "https://real.example.com"


async def test_no_connected_clients_fails_loud(monkeypatch):
    """Hub 无客户端连接必须抛错，不返回假结果。"""

    class EmptyHub:
        def connected_client_ids(self) -> list[str]:
            return []

        def exec_command(self, client_id, method, params):
            raise AssertionError("should not be called")

    monkeypatch.setattr(browser, "_hub", EmptyHub())
    with pytest.raises(RuntimeError, match="no connected browser clients"):
        await browser.browser_navigate("https://example.com")


async def test_stubhub_connected_client_ids():
    """默认 StubHub 提供 stub-client 占位。"""
    ids = browser.StubHub().connected_client_ids()
    assert "stub-client" in ids


# ── S 修复:生产无真实 hub 绝不返回假成功 ───────────────────────

async def test_no_hub_fails_loud(monkeypatch):
    """未绑定任何 hub 时浏览器工具必须明确报错,不得返回假成功。"""
    monkeypatch.setattr(browser, "_hub", None)
    with pytest.raises(RuntimeError, match="browser RPA not available"):
        await browser.browser_navigate("https://example.com")


def test_default_hub_is_none():
    """默认 _hub 必须为 None(生产不落 StubHub 假实现)。"""
    assert browser._hub is None or not isinstance(browser._hub, browser.StubHub)
