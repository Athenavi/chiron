"""Browser RPA tools 注册到本地工具注册表。

实现对标 Go `internal/tools/rpa_browser.go` 注册的 11 个浏览器工具。
默认使用内存 StubHub；生产环境可注入真实 Hub（Chrome Extension WebSocket 等）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.tools.registry import registry


# ── Hub 协议 ──────────────────────────────────────────────────
class BrowserHub(Protocol):
    def connected_client_ids(self) -> list[str]: ...
    def exec_command(
        self, client_id: str, method: str, params: dict[str, Any]
    ) -> Any: ...


@dataclass
class StubHub:
    """内存 Hub，仅作测试夹具。生产不得使用（见 _hub 说明）。"""

    _clients: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {"stub-client": {"url": "about:blank"}}
    )

    def connected_client_ids(self) -> list[str]:
        return list(self._clients.keys())

    def exec_command(self, client_id: str, method: str, params: dict[str, Any]) -> Any:
        return {
            "status": "ok",
            "method": method,
            "client_id": client_id,
            "params": params,
            "note": "stub hub",
        }


# 生产默认为 None：未绑定真实 Hub 时浏览器工具 fail-loud，绝不返回假的默认结果
# （原来默认 StubHub 会让 LLM 以为浏览器 RPA 成功，实则什么都没做 — S 修复）。
_hub: BrowserHub | None = None


def bind_hub(hub: BrowserHub) -> None:
    global _hub
    _hub = hub


class GatewayBrowserHub:
    """真实 Hub：经 Go 网关 /v1/rpa/exec 把命令发给已连接浏览器插件(Chrome Extension /ws/rpa)。

    通过环境变量 RPA_GATEWAY_URL(=http://gateway:8080) 开启；未配置则浏览器工具不可用。
    请求携带共享 X-Internal-Token，由 Go 网关校验（与网关→引擎互信同源）。
    使用共享 httpx.AsyncClient 连接池，避免每次调用新建连接（R6 修复）。
    """

    def __init__(self, base_url: str):
        import httpx

        from app.tools.ssrf import assert_safe_url
        assert_safe_url(base_url)
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            follow_redirects=False,
        )

    async def connected_client_ids(self) -> list[str]:
        from app.config import settings

        import os

        url = f"{self._base_url}/v1/rpa/clients"
        resp = await self._client.get(
            url,
            headers={
                "X-Internal-Token": settings.internal_token
                or os.getenv("INTERNAL_TOKEN", "")
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("client_ids") or []

    async def exec_command(self, client_id: str, method: str, params: dict[str, Any]) -> Any:
        import os

        from app.config import settings

        url = f"{self._base_url}/v1/rpa/exec"
        resp = await self._client.post(
            url,
            json={"client_id": client_id, "method": method, "params": params},
            headers={
                "X-Internal-Token": settings.internal_token
                or os.getenv("INTERNAL_TOKEN", "")
            },
        )
        resp.raise_for_status()
        return resp.json()


def _init_default_hub() -> None:
    """依据配置绑定真实网关 Hub；无配置则保持 None（工具 fail-loud）。"""
    import os

    global _hub
    addr = os.getenv("RPA_GATEWAY_URL", "").strip()
    if addr:
        _hub = GatewayBrowserHub(addr)
        from app.observability.logging import get_logger

        get_logger(__name__).info("Browser RPA hub bound to Go gateway: %s", addr)


_init_default_hub()


def _resolve_client(tab_id: int | None = None) -> str:
    if _hub is None:
        raise RuntimeError(
            "browser RPA not available: no hub bound (set RPA_GATEWAY_URL to the Go gateway)"
        )
    ids = _hub.connected_client_ids()
    if not ids:
        raise RuntimeError("no connected browser clients")
    return ids[0]


def _exec(
    method: str, params: dict[str, Any], tab_id: int | None = None
) -> dict[str, Any]:
    client_id = _resolve_client(tab_id)
    if tab_id and tab_id > 0:
        params = {**params, "tabId": tab_id}
    result = _hub.exec_command(client_id, method, params)
    return result if isinstance(result, dict) else {"result": result}


# ── 工具实现 ──────────────────────────────────────────────────
async def browser_navigate(url: str, tab_id: int | None = None) -> dict[str, Any]:
    if not url:
        return {"error": "url is required"}
    return _exec("browser_navigate", {"url": url}, tab_id)


async def browser_click(selector: str, tab_id: int | None = None) -> dict[str, Any]:
    if not selector:
        return {"error": "selector is required"}
    return _exec("browser_click", {"selector": selector}, tab_id)


async def browser_type(
    selector: str, text: str, tab_id: int | None = None
) -> dict[str, Any]:
    if not selector or not text:
        return {"error": "selector and text are required"}
    return _exec("browser_type", {"selector": selector, "text": text}, tab_id)


async def browser_read(selector: str, tab_id: int | None = None) -> dict[str, Any]:
    if not selector:
        return {"error": "selector is required"}
    return _exec("browser_read", {"selector": selector}, tab_id)


async def browser_screenshot(
    tab_id: int | None = None, full_page: bool = False
) -> dict[str, Any]:
    return _exec("browser_screenshot", {"fullPage": full_page}, tab_id)


async def browser_scroll(
    direction: str = "down", amount: int = 500, tab_id: int | None = None
) -> dict[str, Any]:
    return _exec("browser_scroll", {"direction": direction, "amount": amount}, tab_id)


async def browser_get_state(tab_id: int | None = None) -> dict[str, Any]:
    return _exec("browser_get_state", {}, tab_id)


async def browser_tab_list() -> dict[str, Any]:
    return _exec("browser_tab_list", {})


async def browser_tab_create(
    url: str = "", tab_id: int | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if url:
        params["url"] = url
    return _exec("browser_tab_create", params)


async def browser_tab_switch(tab_id: int) -> dict[str, Any]:
    if not tab_id or tab_id <= 0:
        return {"error": "tabId is required"}
    return _exec("browser_tab_switch", {"tabId": tab_id})


async def browser_tab_close(tab_id: int) -> dict[str, Any]:
    if not tab_id or tab_id <= 0:
        return {"error": "tabId is required"}
    return _exec("browser_tab_close", {"tabId": tab_id})


# ── 注册 ──────────────────────────────────────────────────────
_BROWSER_TOOLS = [
    (
        "browser_navigate",
        "Navigate to a URL",
        {"url": {"type": "string"}, "tab_id": {"type": "integer"}},
        ["url"],
        browser_navigate,
    ),
    (
        "browser_click",
        "Click an element by selector",
        {"selector": {"type": "string"}, "tab_id": {"type": "integer"}},
        ["selector"],
        browser_click,
    ),
    (
        "browser_type",
        "Type text into an element",
        {
            "selector": {"type": "string"},
            "text": {"type": "string"},
            "tab_id": {"type": "integer"},
        },
        ["selector", "text"],
        browser_type,
    ),
    (
        "browser_read",
        "Read element text/attributes",
        {"selector": {"type": "string"}, "tab_id": {"type": "integer"}},
        ["selector"],
        browser_read,
    ),
    (
        "browser_screenshot",
        "Take a page screenshot",
        {
            "tab_id": {"type": "integer"},
            "full_page": {"type": "boolean", "default": False},
        },
        [],
        browser_screenshot,
    ),
    (
        "browser_scroll",
        "Scroll the page",
        {
            "direction": {"type": "string", "default": "down"},
            "amount": {"type": "integer", "default": 500},
            "tab_id": {"type": "integer"},
        },
        [],
        browser_scroll,
    ),
    (
        "browser_get_state",
        "Get current page state",
        {"tab_id": {"type": "integer"}},
        [],
        browser_get_state,
    ),
    ("browser_tab_list", "List open tabs", {}, [], browser_tab_list),
    (
        "browser_tab_create",
        "Create a new tab",
        {"url": {"type": "string"}},
        [],
        browser_tab_create,
    ),
    (
        "browser_tab_switch",
        "Switch to a tab",
        {"tab_id": {"type": "integer"}},
        ["tab_id"],
        browser_tab_switch,
    ),
    (
        "browser_tab_close",
        "Close a tab",
        {"tab_id": {"type": "integer"}},
        ["tab_id"],
        browser_tab_close,
    ),
]

for name, desc, props, required, handler in _BROWSER_TOOLS:
    registry.register(
        name=name,
        description=desc,
        parameters={"type": "object", "properties": props, "required": required},
        handler=handler,
    )
