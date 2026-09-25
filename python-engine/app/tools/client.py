"""
系统工具客户端 — 调用 Go 内部 API
"""

from __future__ import annotations

import logging
from typing import Any, cast

import httpx

logger = logging.getLogger(__name__)


class SystemToolClient:
    """
    系统工具客户端 — 调用 Go 系统工具 API

    Go 侧执行系统工具（邮件、工单、数据库等），带鉴权+计费+审计
    """

    def __init__(
        self,
        go_url: str,
        internal_token: str,
        timeout: int = 30,
        max_retries: int = 3,
    ):
        self._go_url = go_url.rstrip("/")
        self._token = internal_token
        self._timeout = timeout
        self._max_retries = max_retries
        self._http: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端"""
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"X-Internal-Token": self._token},
            )
        return self._http

    async def execute(
        self,
        tool_name: str,
        params: dict[str, Any],
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """
        执行系统工具

        Args:
            tool_name: 工具名称
            params: 工具参数
            tenant_id: 租户 ID
            user_id: 用户 ID

        Returns:
            工具执行结果
        """
        client = await self._get_client()

        for attempt in range(self._max_retries):
            try:
                resp = await client.post(
                    f"{self._go_url}/v1/tools/execute",
                    json={
                        "name": tool_name,
                        "input": params,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                    },
                )

                if resp.status_code == 402:
                    # 余额不足
                    return {"error": "insufficient_balance", "message": resp.text}

                if resp.status_code == 401:
                    # 认证失败
                    return {
                        "error": "unauthorized",
                        "message": "Internal token invalid",
                    }

                resp.raise_for_status()
                return cast("dict[str, Any]", resp.json())

            except httpx.TimeoutException:
                logger.warning(
                    "Tool execution timeout (attempt %d/%d): %s",
                    attempt + 1,
                    self._max_retries,
                    tool_name,
                )
                if attempt == self._max_retries - 1:
                    return {
                        "error": "timeout",
                        "message": f"Tool '{tool_name}' execution timeout",
                    }

            except httpx.HTTPStatusError as e:
                logger.error("Tool execution HTTP error: %s", e)
                if attempt == self._max_retries - 1:
                    return {"error": "http_error", "message": str(e)}

            except Exception as e:
                logger.error("Tool execution error: %s", e)
                if attempt == self._max_retries - 1:
                    return {"error": "execution_error", "message": str(e)}

        return {
            "error": "max_retries_exceeded",
            "message": f"Tool '{tool_name}' failed after {self._max_retries} attempts",
        }

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        if self._http:
            await self._http.aclose()
            self._http = None
