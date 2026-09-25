"""
工具发现 — 从 Go 拉取工具清单
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ToolDiscovery:
    """
    工具发现 — 从 Go 拉取工具清单，本地缓存

    Python 启动时从 Go 拉取工具清单（含权限、费率），缓存到本地
    """

    def __init__(
        self,
        go_url: str,
        internal_token: str,
        refresh_interval: int = 300,  # 5 分钟
    ):
        self._go_url = go_url.rstrip("/")
        self._token = internal_token
        self._refresh_interval = refresh_interval
        self._cache: dict[str, dict[str, Any]] = {}
        self._last_refresh: float = 0
        self._http: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端"""
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=10,
                headers={"X-Internal-Token": self._token},
            )
        return self._http

    async def get_tools(self) -> list[dict[str, Any]]:
        """
        获取工具清单（带缓存）

        Returns:
            工具清单列表
        """
        if time.time() - self._last_refresh > self._refresh_interval:
            await self._refresh()
        return list(self._cache.values())

    async def get_tool(self, name: str) -> dict[str, Any] | None:
        """
        获取单个工具信息

        Args:
            name: 工具名称

        Returns:
            工具信息，不存在返回 None
        """
        if time.time() - self._last_refresh > self._refresh_interval:
            await self._refresh()
        return self._cache.get(name)

    async def _refresh(self) -> None:
        """刷新工具清单"""
        try:
            client = await self._get_client()
            resp = await client.get(f"{self._go_url}/v1/tools")
            resp.raise_for_status()

            data = resp.json()
            tools = data.get("tools", [])
            self._cache = {t["name"]: t for t in tools}
            self._last_refresh = time.time()

            logger.info("Tool list refreshed: %d tools", len(self._cache))

        except Exception as e:
            logger.warning("Failed to refresh tool list: %s", e)
            # 如果缓存为空，设置一个较短的重试间隔
            if not self._cache:
                self._last_refresh = time.time() - self._refresh_interval + 30

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        if self._http:
            await self._http.aclose()
            self._http = None
