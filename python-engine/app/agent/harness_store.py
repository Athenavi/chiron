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


async def _query(tenant_id: str, user_id: str, model: str) -> str:
    """取最匹配的一条：用户级优先于租户级；模型前缀更长的优先于更短的。"""
    try:
        from app.db import get_pool

        pool = get_pool()
        row = await pool.fetchrow(
            """
            SELECT system_prompt_suffix
            FROM harness_profiles
            WHERE tenant_id = $1
              AND enabled = true
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
    return _clamp(str(row["system_prompt_suffix"] or ""))


async def resolve_suffix(tenant_id: str, user_id: str, model: str) -> str:
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
