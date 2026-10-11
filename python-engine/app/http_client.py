"""进程级共享 httpx.AsyncClient —— 连接池复用（P1 修复）。

此前多处 `async with httpx.AsyncClient(...) as client:` 每次调用都新建连接池，
每个请求都要重做 TCP + TLS 握手。`shared_client()` 是一个**上下文管理器**：
它 yield 一个按 (timeout, follow_redirects) 缓存的进程级 client，退出时**不**关闭它，
从而让 keep-alive 连接真正跨请求复用。

用法（把 `httpx.AsyncClient(...)` 换成 `shared_client(...)`，块体与缩进不变）：

    from app.http_client import shared_client

    async with shared_client(timeout=settings.http_timeout_web, follow_redirects=True) as client:
        resp = await client.get(url)

注意：本模块只管**短生命周期调用**的复用。长期持有的 client（如 `self._client = ...`）
保持原样。进程退出时由 `aclose_all()` 统一关闭（见 app.main 的 lifespan shutdown）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

#: httpx 的默认超时（httpx.Timeout(5.0)），用于保持 `httpx.AsyncClient()` 无参调用的原语义。
_DEFAULT_TIMEOUT = 5.0

#: 按 (timeout 秒 | None, follow_redirects) 缓存的共享 client。
_clients: dict[tuple[float | None, bool], httpx.AsyncClient] = {}


def get_shared_client(
    timeout: float | None = _DEFAULT_TIMEOUT, *, follow_redirects: bool = False
) -> httpx.AsyncClient:
    """返回（并按需创建）共享 client。调用方**不得**关闭它。"""
    key: tuple[float | None, bool] = (timeout, bool(follow_redirects))
    client = _clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=follow_redirects,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        _clients[key] = client
    return client


#: 模块加载时的原始 httpx.AsyncClient，用于检测"被测试替换成 mock"。
_ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient


@asynccontextmanager
async def shared_client(
    timeout: float | None = _DEFAULT_TIMEOUT, *, follow_redirects: bool = False
) -> AsyncIterator[httpx.AsyncClient]:
    """上下文管理器形式：yield 共享 client，退出时**不**关闭它。

    若检测到 `httpx.AsyncClient` 已被替换（测试 monkeypatch 打桩），则**不走缓存**、
    每次直接构造并交给替换实现 —— 否则缓存命中会让 monkeypatch 失效（跨测试污染：
    前一个测试建立的 client 被后一个测试复用，打桩形同虚设）。
    """
    if httpx.AsyncClient is not _ORIGINAL_ASYNC_CLIENT:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=follow_redirects
        ) as client:
            yield client
        return
    yield get_shared_client(timeout, follow_redirects=follow_redirects)


async def aclose_all() -> None:
    """关闭全部共享 client（应用关闭时调用）。"""
    for client in list(_clients.values()):
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 — 关闭失败不应阻塞进程退出
            pass
    _clients.clear()
