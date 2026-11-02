"""分布式子 Agent 配额（设计 §9.1）。

为什么必须放 Redis
------------------
`subagent.py` 用 `asyncio.create_task` 起子 Agent，作用范围只有**本副本**。
3 副本 × 模型一轮并发 10 个调用 = 30 个并发子 agent —— 进程内的
`asyncio.Semaphore` 对此完全无感。配额只有落在共享介质上才有意义。

**超限拒绝，不排队**
--------------------
排队会消耗父 agent 的 wall budget，而父模型无法区分"慢"与"死了"：它只会
觉得工具卡住，然后重复调用。所以这里返回明确的拒绝理由，让父模型改
策略（拆小、串行、或干脆自己做）。

三层
----
| 层 | 键 | 默认 | 超限 |
| --- | --- | --- | --- |
| 租户 | `subq:t:{tenant}` | 24 | 拒绝 |
| 会话 | `subq:s:{session}` | 8 | 拒绝 |
| 全局 | `subq:g` | 96 | 拒绝 |

租约语义
--------
槽位带 TTL（ZSET score = 过期时刻）。进程崩溃时 `finally` 跑不到，
槽位靠 TTL 自然过期，不会永久泄漏。这是"宁可短暂超发，也不永久堵死"的取舍：
漏掉的释放会让该会话此后再也起不了子 agent，而超发的上限是多跑一两个。
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

#: 会话级默认上限。单个会话并发 8 个子 agent 已经远超正常用法
#: （每次委派都在烧 token，且都与父 agent 共享工作区）。
DEFAULT_SESSION_LIMIT = 8
#: 租户级默认上限：一个租户通常只跑少数几个会话。
DEFAULT_TENANT_LIMIT = 24
#: 全局默认上限：保护整个引擎进程与下游模型配额。
DEFAULT_GLOBAL_LIMIT = 96
#: 租约长度。必须大于子 agent 的最长运行时间，否则还在跑就被判过期释放。
DEFAULT_LEASE_SECONDS = 3600

#: 三层一次性原子判定。**必须在一段 Lua 里**：分三次 `ZCARD` 再 `ZADD` 的话，
#: 两个请求可能同时看到"还有 1 个空位"然后一起占掉 —— 配额就形同虚设。
_ACQUIRE_LUA = """
local now = tonumber(ARGV[1])
local lease_ms = tonumber(ARGV[2])
local member = ARGV[3]
local lim = {tonumber(ARGV[4]), tonumber(ARGV[5]), tonumber(ARGV[6])}
local names = {'tenant', 'session', 'global'}

-- 先清掉已过期的租约，否则崩溃留下的僵尸槽位会永久占用配额。
for i = 1, 3 do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now)
end

for i = 1, 3 do
  if lim[i] > 0 and redis.call('ZCARD', KEYS[i]) >= lim[i] then
    return names[i]
  end
end

for i = 1, 3 do
  redis.call('ZADD', KEYS[i], now + lease_ms, member)
  redis.call('PEXPIRE', KEYS[i], lease_ms)
end
return 'ok'
"""

_RELEASE_LUA = """
for i = 1, 3 do
  redis.call('ZREM', KEYS[i], ARGV[1])
end
return 1
"""


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def limits() -> tuple[int, int, int]:
    """返回 `(租户, 会话, 全局)` 上限。`0` 表示该层不限。"""
    return (
        max(0, _env_int("SUBAGENT_QUOTA_PER_TENANT", DEFAULT_TENANT_LIMIT)),
        max(0, _env_int("SUBAGENT_QUOTA_PER_SESSION", DEFAULT_SESSION_LIMIT)),
        max(0, _env_int("SUBAGENT_QUOTA_GLOBAL", DEFAULT_GLOBAL_LIMIT)),
    )


def lease_seconds() -> int:
    return max(1, _env_int("SUBAGENT_QUOTA_LEASE_SECONDS", DEFAULT_LEASE_SECONDS))


def new_owner_id() -> str:
    """租约持有者标识。每次 `subagent()` 调用一份，release 时用来精确回收。"""
    return f"sq_{uuid.uuid4().hex[:12]}"


def _keys(tenant_id: int, session_id: str) -> list[str]:
    return [
        f"subq:t:{int(tenant_id or 0)}",
        f"subq:s:{session_id}",
        "subq:g",
    ]


def _as_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def acquire(*, owner: str, tenant_id: int, session_id: str) -> str | None:
    """尝试占一个槽位。成功返回 `None`，超限返回被触发的那一层名。

    Redis 不可用时**放行**（告警后返回 None）。这与项目里"Redis 是硬依赖、
    fail-closed"的取向看似相反，但理由不同：那一条针对的是**状态存储**
    （丢了就错），而配额是限流保护。让 Redis 抖动演变成"所有委派一律失败"，
    比短暂超发严重得多。
    """
    if not session_id:
        # 没有会话就没有可归属的配额对象。上行调用永远带 session，走到这里
        # 说明是异常路径 —— 不为它编一个桶，直接放行。
        return None

    t_lim, s_lim, g_lim = limits()
    if t_lim == 0 and s_lim == 0 and g_lim == 0:
        return None

    from app.redis_client import get_redis

    now_ms = int(time.time() * 1000)
    try:
        redis = await get_redis()
        verdict = await redis.eval(
            _ACQUIRE_LUA,
            3,
            *_keys(tenant_id, session_id),
            now_ms,
            lease_seconds() * 1000,
            owner,
            t_lim,
            s_lim,
            g_lim,
        )
    except Exception as exc:  # noqa: BLE001 — 见 docstring：配额不可用不该阻断委派
        logger.warning("subagent quota unavailable, allowing delegation: %s", exc)
        return None

    text = _as_text(verdict)
    if text == "ok":
        return None
    logger.info("subagent quota exceeded (layer=%s tenant=%s session=%s)", text, tenant_id, session_id)
    return text


async def release(*, owner: str, tenant_id: int, session_id: str) -> None:
    """回收槽位。子 Agent 结束（成功/失败/取消）都必须走到这里。

    失败只告警：槽位最终会被租约 TTL 清掉，不该因为回收失败再抛一次异常
    把已经收尾的对话打挂。
    """
    if not owner or not session_id:
        return

    from app.redis_client import get_redis

    try:
        redis = await get_redis()
        await redis.eval(_RELEASE_LUA, 3, *_keys(tenant_id, session_id), owner)
    except Exception as exc:  # noqa: BLE001 — 租约 TTL 会兜底
        logger.warning("subagent quota release failed (owner=%s): %s", owner, exc)


def rejection_payload(layer: str, *, tenant_id: int, session_id: str) -> dict[str, Any]:
    """给父模型看的拒绝结果。

    带上 `retryable` 与明确的下一步：父模型拿到一句"超限"只会原样重试，
    而重试必然再被拒 —— 那等于制造一次死循环。
    """
    limit = {"tenant": limits()[0], "session": limits()[1], "global": limits()[2]}.get(layer, 0)
    scope = {
        "tenant": "this tenant",
        "session": "this conversation",
        "global": "this deployment",
    }.get(layer, "the system")
    return {
        "status": "rejected",
        "reason": "subagent_quota_exceeded",
        "layer": layer,
        "limit": limit,
        "error": (
            f"Too many concurrent subagents for {scope} (limit={limit}). "
            "Do the work yourself instead, or wait for a running subagent to finish "
            "before delegating again. Retrying immediately will be rejected again."
        ),
        "retryable": False,
    }
