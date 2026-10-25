"""租户/用户级 harness 配置的读取（P1-④）—— **deepagents 没有这一层**。

deepagents 的 harness profile 是**部署级**注册（`register_harness_profile`，进程启动时决定）。
Chiron 把它下沉到租户级 / 用户级：同一条稳定前缀，租户可以追加自己的行为塑形，
用户级还能覆盖租户级。

**四条硬约束**

1. **默认关**：只有 `enabled = true` 的记录才生效；表空 / 无记录 ⇒ 返回空串 ⇒
   `_apply_system_prefix` 逐字不变（引入本机制不该改变任何现有会话行为）。
2. **有上限**：suffix 会拼进 system prompt ⇒ 长度封顶（`MAX_SUFFIX_CHARS`，超出截断）。
   否则一个租户能用配置吃掉所有会话的上下文预算。
3. **按租户隔离**：查询以 `tenant_id` 为第一键 —— 不支持跨租户读取。
4. **失败不阻断**：查库失败只告警并返回空串（配置读不到 = 不生效，而不是让对话失败）。

**性能**：每轮都要读，但配置是"低频变更、高频读取" ⇒ 进程内 TTL 缓存，与
`app/agent/model_window.py` 同一取向。TTL 内变更最多滞后 `CACHE_TTL_SECONDS` 秒。

**可写通道**：本模块**只读**。写入由管理面负责（表结构见
`migrations/versions/0014_harness_profiles.py`）。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

#: suffix 长度上限（字符）—— 超过即截断并告警
MAX_SUFFIX_CHARS = 2000
#: 进程内缓存 TTL（秒）：配置低频变更，每次请求查库不值当
CACHE_TTL_SECONDS = 60.0

#: (tenant_id, user_id, model) -> (expire_at_monotonic, suffix)
_cache: dict[tuple[str, str, str], tuple[float, str]] = {}


def _clamp(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_SUFFIX_CHARS:
        return text
    logger.warning(
        "harness suffix truncated (%d -> %d chars): 租户配置超出上限",
        len(text),
        MAX_SUFFIX_CHARS,
    )
    return text[:MAX_SUFFIX_CHARS]


async def _query(tenant_id: int, user_id: str, model: str) -> str:
    """取最匹配的一条：用户级优先于租户级；模型前缀更长的优先于更短的。"""
    try:
        from app.db import get_pool

        pool = get_pool()
        row = await pool.fetchrow(
            """
            SELECT enabled, system_prompt_suffix
            FROM harness_profiles
            WHERE tenant_id = $1
              AND (user_id = $2 OR user_id = '')
              AND ($3 = '' OR model_prefix = '' OR lower($3) LIKE model_prefix || '%')
            ORDER BY (user_id = $2) DESC, length(model_prefix) DESC
            LIMIT 1
            """,
            tenant_id,
            user_id,
            model,
        )
    except Exception as exc:  # noqa: BLE001 - 读不到配置 = 不生效，不该让对话失败
        logger.warning("harness profile lookup failed: %s", exc)
        return ""
    if not row:
        return ""
    # `enabled = false` 是**显式禁用**，不是"未配置"：它占位并**阻止**回退到租户级 ——
    # 否则用户没法关掉租户给他加上的 suffix。所以这里不能把 `enabled = true` 写进 WHERE
    # （那会让禁用记录被跳过、悄悄回退到租户级），只能取到最近的一条再判断。
    if not row["enabled"]:
        return ""
    return _clamp(str(row["system_prompt_suffix"] or ""))


async def resolve_suffix(tenant_id: int, user_id: str, model: str) -> str:
    """返回该租户/用户/模型适用的 harness suffix；没有则空串（= 不生效）。"""
    if not tenant_id:
        return ""
    key = (tenant_id, user_id or "", (model or "").lower())
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    value = await _query(tenant_id, user_id or "", (model or "").lower())
    _cache[key] = (now + CACHE_TTL_SECONDS, value)
    return value


def clear_cache() -> None:
    """清空进程内缓存（配置变更后立即生效 / 测试用）。"""
    _cache.clear()


#: 变更广播频道。
#:
#: ⚠️ **必须与 Go 侧 `internal/api/harness_profiles.go` 的 `HarnessProfileChannel` 逐字相同**
#: （语言不同，无法共享常量；`scripts/check_tool_policy_parity.py` 会断言这一对）。
CHANGE_CHANNEL = "chiron:harness_profile_changed"

#: 订阅断开后的重连退避（秒）
RECONNECT_DELAY_SECONDS = 5.0


async def listen_for_changes() -> None:
    """订阅变更广播，收到即清缓存 —— 把"最多滞后一个 TTL"变成**即时生效**。

    **失败不致命**：Redis 不可用或连接断开时记 warning 并退避重试；调用方（lifespan）
    把它当周期任务取消即可（与 reconciler / retention 同一生命周期范式）。取消会让
    `asyncio.CancelledError` 冒泡出去，因此停机不会卡在这里。
    """
    import asyncio

    while True:
        pubsub = None
        try:
            from app.redis_client import aclose_pubsub, get_redis

            redis = await get_redis()
            if redis is None:
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                continue
            pubsub = redis.pubsub()
            await pubsub.subscribe(CHANGE_CHANNEL)
            logger.info("harness profile listener subscribed: %s", CHANGE_CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    clear_cache()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 订阅断了要能自己回来
            logger.warning("harness profile listener error, retrying: %s", exc)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
        finally:
            if pubsub is not None:
                try:
                    await aclose_pubsub(pubsub)
                except Exception:  # noqa: BLE001 - 关连接失败不该盖住上面的异常
                    logger.debug("harness pubsub close failed", exc_info=True)
