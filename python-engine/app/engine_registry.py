"""EngineRegistry — 引擎实例 Redis 自注册（批 E1）。

多实例部署下，网关通过 Redis 注册表动态发现引擎副本，替代静态 PYTHON_ENGINE_ADDRESS
的人工同步。引擎启动后周期性写 {REDIS_KEY_PREFIX}engine:instance:{instance_id} =
{"url","version","last_seen"}，TTL 60s；网关侧每 15s 扫描存活实例并动态更新地址表。

- advertise_url 由环境变量 ENGINE_ADVERTISE_URL 提供（引擎所在网络可达地址，
  如 http://engine-0:8000；K8s 按 pod/statefulset 注入）。
- 同一 compose 服务名（--scale）下各副本广播同一 URL：网关去重后等价现有 DNS 轮询。
- 未配置 advertise_url 或 Redis 不可用时跳过注册（网关回退静态 PYTHON_ENGINE_ADDRESS）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

REG_TTL_SECONDS = 60  # 注册 key 存活期；网关 15s 扫描，留 4 倍余量
REG_INTERVAL_SECONDS = 20  # 心跳写间隔（< TTL，保证网关始终可见）


class EngineRegistry:
    """引擎实例心跳注册器（每进程一个，随 lifespan 启停）。"""

    def __init__(
        self,
        redis: Any,
        instance_id: str,
        advertise_url: str,
        version: str = "3.0.0",
    ) -> None:
        self._redis = redis
        self._instance_id = instance_id
        self._advertise_url = advertise_url.strip().rstrip("/")
        self._version = version
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._redis and self._advertise_url)

    async def start(self) -> None:
        if not self.enabled:
            logger.info(
                "engine registry disabled: advertise_url=%r redis=%r",
                self._advertise_url,
                self._redis is not None,
            )
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("engine registry started: %s", self._advertise_url)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        from app.redis_keys import rkey

        key = rkey("engine:instance:") + self._instance_id
        try:
            while True:
                await self._publish(key)
                await asyncio.sleep(REG_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            # 优雅退出：主动注销，避免残留到 TTL 过期
            try:
                await self._redis.delete(key)
            except Exception as exc:  # noqa: BLE001
                logger.debug("engine registry deregister failed: %s", exc)
            raise
        except Exception as exc:  # noqa: BLE001 - 注册失败不阻断引擎运行
            logger.warning("engine registry loop failed: %s", exc)

    async def _publish(self, key: str) -> None:
        payload = {
            "url": self._advertise_url,
            "version": self._version,
            "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            await self._redis.set(key, json.dumps(payload), ex=REG_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("engine registry heartbeat failed: %s", exc)
