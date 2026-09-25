"""MCP owner 租约与跨实例桥（B1a）。

问题：MCP 连接规模 ≈ **引擎实例数 × 活跃用户数 × 每用户 server 数**，扩容会线性放大
对第三方 MCP server 的连接（000.md 第 6 条）。

方案：把每个活跃用户**固定到一个 owner 实例**持有 MCP 连接，其它实例不建连接，只注册
「代理工具」并在调用时经 Redis 通道转发给 owner。连接数因此从 N×M×S 降为 M×S。

组成：
- **租约**：``mcp:owner:{user_id}`` = instance_id（TTL + 续期）
  - 抢租约：``SET ... NX EX``（原子，只有一方成功）
  - 续期/释放：Lua CAS（仅当值仍是自己），避免误删后继 owner
- **工具清单**：owner 把 ``[{name, description, schema}]`` 写入 ``mcp:tools:{user_id}``（TTL 同租约），
  非 owner 读取后注册代理工具——否则 LLM 在非 owner 实例上看不到这些工具。
- **调用桥**：pub/sub 请求-响应
  - 请求 → ``mcp:invoke:{owner_instance_id}``（含 reply_to / req_id / tool / args）
  - 响应 → ``mcp:reply:{caller_instance_id}``（含 req_id）
  - 超时/owner 不可达**明确报错**（不返回空结果，避免 LLM 基于错误结果继续）

失败语义与边界：
- 无 Redis 或未开启 ``MCP_OWNER_LEASE_ENABLED`` 时完全不启用（单实例行为不变）；
- pub/sub 不保证投递：超时即失败并报错，**不自动重试**（MCP 工具可能有副作用）；
- 本模块只做路由，不改变工具语义；owner 故障后租约 TTL 到期，其它实例会在下一轮轮询接管。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# 抢租约：仅当键不存在时写入（原子）
_ACQUIRE_LUA = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
  return 1
end
return 0
"""

# 续期/释放：仅当值仍是自己（避免覆盖/删除后继 owner）
_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def _owner_key(user_id: str) -> str:
    from app.redis_keys import rkey

    return rkey("mcp:owner:") + user_id


def _tools_key(user_id: str) -> str:
    from app.redis_keys import rkey

    return rkey("mcp:tools:") + user_id


def _invoke_channel(instance_id: str) -> str:
    from app.redis_keys import rkey

    return rkey("mcp:invoke:") + instance_id


def _reply_channel(instance_id: str) -> str:
    from app.redis_keys import rkey

    return rkey("mcp:reply:") + instance_id


class MCPOwnerLease:
    """活跃用户 → owner 实例 的租约与工具清单（Redis）。"""

    def __init__(self, redis: Any, instance_id: str, ttl: int) -> None:
        self._redis = redis
        self._instance_id = instance_id
        self._ttl = max(30, int(ttl))

    @property
    def enabled(self) -> bool:
        return self._redis is not None

    async def acquire(self, user_id: str) -> bool:
        """抢占 owner（返回是否抢到）。抢不到说明其它实例持有。"""
        if not self.enabled or not user_id:
            return True  # 未启用 → 视作自己持有（保持单实例语义）
        try:
            res = await self._redis.eval(
                _ACQUIRE_LUA, 1, _owner_key(user_id), self._instance_id, self._ttl
            )
            return int(res) == 1
        except Exception as exc:  # noqa: BLE001 - Redis 抖动时退化为自己持有
            logger.warning("mcp owner acquire failed for %s: %s", user_id, exc)
            return True

    async def renew(self, user_id: str) -> bool:
        """续期（仅当自己仍是 owner）。"""
        if not self.enabled or not user_id:
            return True
        try:
            res = await self._redis.eval(
                _RENEW_LUA, 1, _owner_key(user_id), self._instance_id, self._ttl
            )
            return int(res) == 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("mcp owner renew failed for %s: %s", user_id, exc)
            return False

    async def release(self, user_id: str) -> None:
        """释放（仅当自己仍是 owner）。"""
        if not self.enabled or not user_id:
            return
        try:
            await self._redis.eval(
                _RELEASE_LUA, 1, _owner_key(user_id), self._instance_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("mcp owner release failed for %s: %s", user_id, exc)

    async def owner_of(self, user_id: str) -> str | None:
        """返回当前 owner 实例 ID（无租约/失败返回 None）。"""
        if not self.enabled or not user_id:
            return None
        try:
            raw = await self._redis.get(_owner_key(user_id))
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    async def publish_tools(self, user_id: str, tools: list[dict[str, Any]]) -> None:
        """owner 公布该用户可见的工具清单（名称/描述/参数 schema），供非 owner 注册代理。"""
        if not self.enabled or not user_id:
            return
        try:
            await self._redis.set(
                _tools_key(user_id), json.dumps(tools, ensure_ascii=False), ex=self._ttl
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("mcp tools publish failed for %s: %s", user_id, exc)

    async def read_tools(self, user_id: str) -> list[dict[str, Any]]:
        """读取 owner 公布的清单（无/失败返回空列表）。"""
        if not self.enabled or not user_id:
            return []
        try:
            raw = await self._redis.get(_tools_key(user_id))
        except Exception:  # noqa: BLE001
            return []
        if not raw:
            return []
        try:
            data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (ValueError, AttributeError):
            return []
        return data if isinstance(data, list) else []


class MCPBridge:
    """跨实例 MCP 工具调用桥（pub/sub 请求-响应）。

    handler 由 owner 侧提供：(tool_name, args) -> awaitable result。
    """

    def __init__(
        self,
        redis: Any,
        instance_id: str,
        handler: Callable[[str, dict[str, Any]], Awaitable[Any]],
        timeout: float = 30.0,
    ) -> None:
        self._redis = redis
        self._instance_id = instance_id
        self._handler = handler
        self._timeout = max(1.0, float(timeout))
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._tasks: list[asyncio.Task[Any]] = []

    async def start(self) -> None:
        if self._redis is None:
            return
        self._tasks = [
            asyncio.create_task(self._serve_requests()),
            asyncio.create_task(self._serve_replies()),
        ]
        logger.info("mcp bridge started (instance=%s)", self._instance_id)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks = []
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def invoke(self, owner_instance_id: str, tool_name: str, args: dict[str, Any]) -> Any:
        """在 owner 实例上执行工具并返回结果；超时/失败抛 RuntimeError（不静默）。"""
        if self._redis is None or not owner_instance_id:
            raise RuntimeError("MCP bridge unavailable (no Redis or missing owner)")
        req_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        payload = json.dumps(
            {
                "req_id": req_id,
                "reply_to": self._instance_id,
                "tool": tool_name,
                "args": args or {},
            },
            ensure_ascii=False,
        )
        try:
            await self._redis.publish(_invoke_channel(owner_instance_id), payload)
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except TimeoutError as e:
            raise RuntimeError(
                f"MCP 跨实例调用超时（owner={owner_instance_id}, tool={tool_name}, "
                f"timeout={self._timeout}s）——owner 可能已下线"
            ) from e
        finally:
            self._pending.pop(req_id, None)

    async def _serve_requests(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(_invoke_channel(self._instance_id))
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    req = json.loads(msg["data"])
                except (ValueError, TypeError):
                    continue
                reply_to = req.get("reply_to") or ""
                req_id = req.get("req_id") or ""
                if not reply_to or not req_id:
                    continue
                try:
                    result = await self._handler(req.get("tool", ""), req.get("args") or {})
                    resp = {"req_id": req_id, "ok": True, "result": result}
                except Exception as e:  # noqa: BLE001 - 远端失败要如实回传
                    resp = {"req_id": req_id, "ok": False, "error": str(e)}
                try:
                    await self._redis.publish(
                        _reply_channel(reply_to), json.dumps(resp, ensure_ascii=False)
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("mcp bridge reply failed: %s", exc)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await pubsub.unsubscribe(_invoke_channel(self._instance_id))
                await pubsub.close()
            except Exception:  # noqa: BLE001
                pass

    async def _serve_replies(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(_reply_channel(self._instance_id))
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    resp = json.loads(msg["data"])
                except (ValueError, TypeError):
                    continue
                fut = self._pending.get(resp.get("req_id") or "")
                if fut is None or fut.done():
                    continue
                if resp.get("ok"):
                    fut.set_result(resp.get("result"))
                else:
                    fut.set_exception(
                        RuntimeError(resp.get("error") or "remote MCP call failed")
                    )
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await pubsub.unsubscribe(_reply_channel(self._instance_id))
                await pubsub.close()
            except Exception:  # noqa: BLE001
                pass
