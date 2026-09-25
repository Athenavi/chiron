"""RunRegistry — session 归属映射（引擎 run owner lease，Redis 镜像）。

背景：agent 进行中 run 的现场状态（AgentRuntime、工具审批、PersistentTerminal）仍在
本进程内，网关只能用「session 一致性哈希」尽力路由；哈希在扩缩容/实例上下线后会漂移，
审批与取消可能落到错误实例，用户只看到含糊的 "no active agent for this session"。

本模块把「哪个实例持有该 session 的 run」写进 Redis（TTL 续期 + run_token），供两侧使用：

- 网关：把该 session 的请求优先路由到映射中的实例（哈希仅作回退，见
  internal/engine/discovery.go 的 applyRunAffinity）；
- 引擎：审批/取消时校验归属与 run_token——映射指向别的实例时返回明确错误
  （而非静默失败）；实例故障后映射随 TTL 过期，用户重试即在新实例重建 run。

键：``{REDIS_KEY_PREFIX}engine:run:{session_id}``
值：``{"instance_id","run_token","owner_uid","url","started_at","last_seen"}``
TTL 300s（与 Go 侧 session run 锁 5min 对齐），心跳 100s 续期。

注意：这里只登记「归属」，不做 run 现场状态的持久化/迁移——实例故障时该 run 仍会中断
（用户可重试），本模块消除的是「路由到错误实例 + 静默失败」这一类问题。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

RUN_TTL_SECONDS = 300
RUN_HEARTBEAT_SECONDS = 100


def run_key(session_id: str) -> str:
    from app.redis_keys import rkey

    return rkey("engine:run:") + session_id


def _payload(
    instance_id: str, run_token: str, owner_uid: str, url: str, started_at: str
) -> str:
    return json.dumps(
        {
            "instance_id": instance_id,
            "run_token": run_token,
            "owner_uid": owner_uid,
            "url": url,
            "started_at": started_at,
            "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )


async def owner_of(redis: Any, session_id: str) -> dict[str, Any] | None:
    """读取 session 的 run 归属；无映射/解析失败/Redis 不可用均返回 None。"""
    if redis is None or not session_id:
        return None
    try:
        raw = await redis.get(run_key(session_id))
    except Exception as exc:  # noqa: BLE001 - 归属查询失败不阻断审批
        logger.debug("run registry read failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


class RunLease:
    """一次 run 的归属租约：启动即登记，周期续期，结束（仅 token 匹配时）删除。"""

    def __init__(
        self,
        redis: Any,
        session_id: str,
        instance_id: str,
        run_token: str,
        owner_uid: str = "",
        url: str = "",
    ) -> None:
        self._redis = redis
        self._session_id = session_id
        self._instance_id = instance_id
        self._run_token = run_token
        self._owner_uid = owner_uid
        self._url = url
        self._started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._task: asyncio.Task[Any] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._redis and self._session_id)

    async def start(self) -> None:
        if not self.enabled:
            return
        await self._publish()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止续期并注销归属（仅当键仍属于本次 run_token，避免误删后继 run）。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if not self.enabled:
            return
        try:
            await self._redis.eval(
                _RELEASE_LUA,
                1,
                run_key(self._session_id),
                self._run_token,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("run registry release failed: %s", exc)

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(RUN_HEARTBEAT_SECONDS)
                await self._publish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 续期失败不阻断 run
            logger.warning("run registry heartbeat failed: %s", exc)

    async def _publish(self) -> None:
        try:
            await self._redis.set(
                run_key(self._session_id),
                _payload(
                    self._instance_id,
                    self._run_token,
                    self._owner_uid,
                    self._url,
                    self._started_at,
                ),
                ex=RUN_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("run registry publish failed: %s", exc)


# 仅当键仍属于本次 run_token 时删除，避免旧 run 的清理删掉新 run 的登记。
_RELEASE_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then
  return 0
end
local ok, data = pcall(cjson.decode, cur)
if ok and data['run_token'] == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
