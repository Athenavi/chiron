"""HTTP + SSE 客户端（ACP 适配层用）—— 与 headless CLI（批 F）走同一条路。

**为什么单独一层**：它的输入输出是"事件字典的异步迭代"，与 ACP / SDK 完全无关 —— 因此能用
httpx 的 `MockTransport` 完整单测（不需要引擎、不需要 SDK）。`agent.py` 只负责把这些事件交给 SDK。

三条与批 F 一致的口径：

* 提交走 `POST /submit`，收事件走 `GET /events`（SSE）—— **真实用户路径**，不 import 引擎内部类；
* 身份走 `X-API-Key`（一个连接一个身份）；
* 取消走 `POST /v1/agent/interrupt`（**真取消**：引擎在轮次边界停下）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

#: 视为"本轮结束"的事件类型（与评测侧同一口径；`cancelled` 也是终态）
TERMINAL_EVENTS = frozenset({"done", "error", "cancelled"})

DEFAULT_TIMEOUT = 300.0


def parse_sse_line(line: str) -> dict[str, Any] | None:
    """解析一行 SSE；非 `data:` 行或非法 JSON 返回 None。

    刻意**宽容**：解析不了的行跳过而不是抛错 —— 一行噪音不该让整轮失败。
    """
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


class ChironClient:
    """网关的薄客户端（只用到三个端点）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Any = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        #: 可注入的 httpx transport —— 生产为 None（走默认），测试用 `MockTransport`。
        #: 没有这个口子，`events()` 的 SSE 解析就只能靠真实引擎来验证。
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._api_key} if self._api_key else {}

    def _httpx(self) -> Any:
        # 延迟 import：与评测侧同一取舍（只有真实 HTTP 路径需要 httpx）
        import httpx

        return httpx

    async def submit(self, session_id: str, content: str) -> None:
        """提交一轮输入。"""
        httpx = self._httpx()
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers=self._headers(),
            transport=self._transport,
        ) as client:
            resp = await client.post(
                "/submit", json={"session_id": session_id, "content": content}
            )
            resp.raise_for_status()

    async def events(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        """读事件流，直到终态事件为止。"""
        httpx = self._httpx()
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers=self._headers(),
            transport=self._transport,
        ) as client:
            async with client.stream(
                "GET", "/events", params={"session_id": session_id}
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    event = parse_sse_line(line)
                    if event is None:
                        continue
                    yield event
                    if str(event.get("type", "")) in TERMINAL_EVENTS:
                        return

    async def interrupt(self, session_id: str) -> bool:
        """请求中断（真取消）。返回网关是否接受。"""
        httpx = self._httpx()
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers=self._headers(),
            transport=self._transport,
        ) as client:
            resp = await client.post("/v1/agent/interrupt", json={"session_id": session_id})
            if resp.status_code != 200:
                logger.warning(
                    "interrupt rejected: status=%s body=%s", resp.status_code, resp.text[:200]
                )
                return False
            try:
                payload = resp.json()
            except ValueError:
                return False
            data = payload.get("data") if isinstance(payload, dict) else None
            return bool((data or payload).get("ok")) if isinstance(payload, dict) else False


async def stream_run(
    client: ChironClient, session_id: str, content: str
) -> AsyncIterator[dict[str, Any]]:
    """提交一轮并产出事件（到终态为止）。

    提交失败不抛给调用方：产出一个 `error` 事件 —— 调用方（ACP 适配层）据此收尾并向用户说明，
    而不是让异常穿到 SDK 的 dispatcher 里变成一个没有上下文的崩溃。
    """
    try:
        await client.submit(session_id, content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("submit failed: %s", exc)
        yield {"type": "error", "error": f"submit_failed: {exc}"}
        return

    try:
        async for event in client.events(session_id):
            yield event
    except Exception as exc:  # noqa: BLE001
        logger.warning("event stream failed: %s", exc)
        yield {"type": "error", "error": f"stream_failed: {exc}"}


async def interrupt(client: ChironClient, session_id: str) -> bool:
    """请求中断；失败返回 False（不抛）。"""
    try:
        return await client.interrupt(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("interrupt failed: %s", exc)
        return False
