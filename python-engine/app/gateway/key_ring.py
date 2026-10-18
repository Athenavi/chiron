"""KeyRing —— 引擎侧 LLM 密钥环（集中派 DR 的引擎运行时组件，V2：Redis Lua 原子取 key）。

权威分层（与 docs/llm-provider-key-management-dr.md 一致）：
- **DB 是权威**：网关 `syncProviderKeyset` 把 `llm:keys:{provider}` 这个 HASH 整体替换成
  库里当前 key，字段值只带 `{"k": 明文, "s": 权威状态}`；`s` 只有管理员能改（active /
  rate_limited / circuit_open）。
- **Redis 镜像是每次取 key 的唯一仲裁**：本类不再维护进程内的"本地环 + freshness 缓存"，
  `get_key`/`report_failure` 各自跑一段**只碰这一个 HASH 键**的 Lua 脚本，读写都在服务端原子完成。

为什么要 Redis 侧原子（V1 的三个真实缺陷，DR §5.1/§5.2 的 A1/A2/B1）：
- **A1 公平性**：V1 `get_key` 先 `active_keys()`（释放锁）、取队头、再重新拿锁把它挪到队尾 ——
  读与改之间有 await 间隙，同实例并发协程会都看到同一个队头、都打同一把 key；各实例本地环又各转各的。
  V2 把"选哪把 + 记账"收进一段 EVAL：按**最小 last-used（LRU）**在可用 key 里选，选完立刻把它的
  `u` 写成当前时间 —— 跨实例、跨协程都是严格轮转，无竞态。
- **A2 熔断不可自愈 / 冷却不跨实例**：V1 达阈值把 `s` 写成 `circuit_open`，而放行只看 `s=="active"`，
  冷却时间戳 `c` 只用来"额外排除"、从不用来"重新放行" ⇒ 一次限流风暴能把好 key 永久退役，
  直到管理员重新同步；进程内 30s 冷却从不写 Redis，别的实例照样打；共享计数 `incr`+`expire` 两次调用，
  中间崩溃就留下一个没有 TTL、永不复位的计数键。V2：**引擎不再改 `s`**（`s` 归管理员），自动停用
  只在 active key 上盖一个**到期时间戳 `c`**（软 30s、达阈值 60s），取 key 把 `c<=now` 当已恢复 ⇒ 自愈；
  冷却/失败窗口都写进同一个共享 HASH ⇒ 跨实例立即可见；失败计数 `f`/`ft` 也并进字段 payload，
  在一段 EVAL 里"读-改-写" ⇒ 原子、且不再有无 TTL 的孤儿计数键。
- **B1 单一状态源**：选 key 的路径不再有本地镜像与 freshness 缓存，四状态源收敛为
  "DB 权威 `s` + 引擎瞬态 `c/u/f/ft`"，且管理员整体同步会顺带清掉瞬态（罕见、且能自愈）。

Cluster 约束：引擎虽是单连接 `redis.asyncio.Redis`，但网关按多 master 路由这些键
（见 internal/api/model_discovery.go 的 "各 master" 注释）。为对任何拓扑都安全，
**每段 EVAL 只碰一个键**（该 provider 的 HASH）——所以计数器/LRU 游标只能写进 HASH 的字段 payload，
不能用独立的 `llm:rr:` / `llm:fail:` 键（那会与 HASH 落不同 slot 触发 CROSSSLOT）。

Redis 不可用 / 镜像为空：退回 env 种子（`settings.xxx_api_key`，进程内 LRU 轮转 + 软冷却），
与 Redis 故障降级语义一致（DR §9）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.redis_keys import rkey

logger = logging.getLogger(__name__)

LLM_KEY_HASH_PREFIX = rkey("llm:keys:")

# 共享失败计数窗口（秒）与停用阈值（窗口用字段里的 ft 时间戳模拟，不依赖键 TTL）。
FAIL_WINDOW = 60
FAIL_THRESHOLD = 5
# 自动冷却时间窗（秒）：单次失败软冷却 / 达阈值升级冷却。都写成到期时间戳 c，到期即自愈。
COOLDOWN_SOFT = 30
COOLDOWN_HARD = 60


def _key_id(provider: str, key: str) -> str:
    import hashlib

    return hashlib.sha256(f"{provider}:{key}".encode()).hexdigest()[:12]


# 单键原子取 key：HGETALL 该 provider 的 HASH，筛出可用（s==active 且冷却已到期），
# 按最小 last-used（u）选一把（LRU ⇒ 严格轮转），把它的 u 写成当前时间后返回 {digest, 明文}。
# 无可用 key 返回 nil。只碰 KEYS[1] 一个键 ⇒ Cluster 安全。
_PICK_SCRIPT = """
local now = tonumber(ARGV[1])
local flat = redis.call('HGETALL', KEYS[1])
local bf, bk, bu = nil, nil, nil
for i = 1, #flat, 2 do
  local ok, o = pcall(cjson.decode, flat[i + 1])
  if ok and type(o) == 'table' and (o.s or 'active') == 'active' then
    local k = o.k
    local c = tonumber(o.c) or 0
    if k and k ~= '' and (c == 0 or c <= now) then
      local u = tonumber(o.u) or 0
      if bf == nil or u < bu then bf = flat[i]; bk = k; bu = u end
    end
  end
end
if bf == nil then return nil end
local ok2, o2 = pcall(cjson.decode, redis.call('HGET', KEYS[1], bf))
if ok2 and type(o2) == 'table' then
  o2.u = now
  redis.call('HSET', KEYS[1], bf, cjson.encode(o2))
end
return {bf, bk}
"""

# 单键原子失败上报：把 digest 字段的窗口化失败计数 +1，盖软冷却 c=now+soft；
# 达阈值则升级为 hard 冷却并复位窗口。绝不缩短一个已存在的更长冷却。
# 返回值：-1 = 该 digest 不在本 HASH（例如 -anthropic 变体把上游 key 存在基础 provider 下，
# 调用方换源重试）；0 = 记了一次软冷却；1 = 本次触达阈值、升级为硬冷却。
_FAIL_SCRIPT = """
local field = ARGV[1]
local now = tonumber(ARGV[2])
local soft = tonumber(ARGV[3])
local hard = tonumber(ARGV[4])
local win = tonumber(ARGV[5])
local thr = tonumber(ARGV[6])
local raw = redis.call('HGET', KEYS[1], field)
if not raw then return -1 end
local ok, o = pcall(cjson.decode, raw)
if not ok or type(o) ~= 'table' then return -1 end
local f = tonumber(o.f) or 0
local ft = tonumber(o.ft) or 0
if ft == 0 or (now - ft) > win then f = 0; ft = now end
f = f + 1
local cd = soft
local escalated = 0
if f >= thr then cd = hard; f = 0; ft = 0; escalated = 1 end
o.f = f
o.ft = ft
local until_ts = now + cd
local cur = tonumber(o.c) or 0
if until_ts > cur then o.c = until_ts end
redis.call('HSET', KEYS[1], field, cjson.encode(o))
return escalated
"""


class KeyRing:
    """LLM 密钥环（V2：Redis 为每次取的原子仲裁；env 种子仅作降级兜底）。"""

    def __init__(
        self, redis: Any = None, env_seeds: dict[str, list[str]] | None = None
    ) -> None:
        self._redis = redis
        self._env = env_seeds or {}
        # env 兜底路径（无 Redis）用的进程内 LRU 游标与软冷却：digest -> epoch。
        self._env_used: dict[str, float] = {}
        self._env_cool: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _sources(provider: str) -> list[str]:
        """同产品协议变体共享一把上游 key：`X-anthropic` 取不到时回退读 `X`。

        `_resolve_client` 拿不到 key 会静默回退到 placeholder ⇒ 用假 key 打上游得 401，
        而真 key 其实挂在基础 provider 下。按命名约定补一次回退源。
        """
        sources = [provider]
        if provider.endswith("-anthropic"):
            sources.append(provider[: -len("-anthropic")])
        return sources

    @staticmethod
    def _usable(obj: Any, now: float) -> tuple[str, str] | None:
        """从一条字段 payload 判可用：s 必须 active（引擎不改 s），且冷却时间戳到期。"""
        if not isinstance(obj, dict):
            return None
        if str(obj.get("s") or "active") != "active":
            return None
        key = str(obj.get("k") or "")
        if not key:
            return None
        try:
            cooldown = float(obj.get("c") or 0.0)
        except (TypeError, ValueError):
            cooldown = 0.0
        if cooldown and now < cooldown:
            return None
        return key, cooldown

    def _env_digests(self, provider: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key in self._env.get(provider, []):
            out[_key_id(provider, key)] = {"key": key}
        return out

    async def _usable_from_hash(self, provider: str, now: float) -> list[dict[str, Any]]:
        """读取该 provider 的 HASH，返回可用 {key,id} 列表（仅用于健康检查/兜底列举，不做记账）。"""
        if self._redis is None:
            return []
        raw = await self._redis.hgetall(f"{LLM_KEY_HASH_PREFIX}{provider}")
        out: list[dict[str, Any]] = []
        for digest, payload in (raw or {}).items():
            digest = digest.decode() if isinstance(digest, bytes) else digest
            try:
                obj = json.loads(payload.decode() if isinstance(payload, bytes) else payload)
            except (ValueError, TypeError):
                continue
            usable = self._usable(obj, now)
            if usable:
                out.append({"key": usable[0], "id": digest})
        return out

    async def active_keys(self, provider: str) -> list[dict[str, Any]]:
        """该 provider 当前可用 key 列表；Redis 镜像优先，取不到再退 env 种子。"""
        now = time.time()
        if self._redis is not None:
            for src in self._sources(provider):
                try:
                    keys = await self._usable_from_hash(src, now)
                except Exception as exc:  # noqa: BLE001 - Redis 抖动退到 env，行为与降级文档一致
                    logger.warning("KeyRing active_keys refresh failed: %s", exc)
                    keys = []
                if keys:
                    return keys
        return self._env_active(provider, now)

    def _env_active(self, provider: str, now: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for src in self._sources(provider):
            for digest, item in self._env_digests(src).items():
                if now < self._env_cool.get(digest, 0.0):
                    continue
                out.append({"key": item["key"], "id": digest})
        return out

    async def get_key(self, provider: str) -> dict[str, Any] | None:
        """取一把可用 key：Redis 侧单键 EVAL 原子选（LRU 轮转），Redis 不可用/为空退 env。"""
        now = time.time()
        if self._redis is not None:
            for src in self._sources(provider):
                try:
                    picked = await self._redis.eval(
                        _PICK_SCRIPT, 1, f"{LLM_KEY_HASH_PREFIX}{src}", str(now)
                    )
                except Exception as exc:  # noqa: BLE001 - EVAL 失败（含 Redis 断连）退 env 兜底
                    logger.warning("KeyRing pick EVAL failed (redis unavailable?): %s", exc)
                    continue
                if picked:
                    digest, key = picked[0], picked[1]
                    digest = digest.decode() if isinstance(digest, bytes) else digest
                    key = key.decode() if isinstance(key, bytes) else key
                    return {"key": str(key), "id": str(digest)}
        return self._env_pick(provider, now)

    def _env_pick(self, provider: str, now: float) -> dict[str, Any] | None:
        """无 Redis：在 env 种子里按进程内 LRU 选一把，跳过软冷却中的。"""
        candidates: list[tuple[float, str, str]] = []
        for src in self._sources(provider):
            for digest, item in self._env_digests(src).items():
                if now < self._env_cool.get(digest, 0.0):
                    continue
                candidates.append((self._env_used.get(digest, 0.0), digest, item["key"]))
        if not candidates:
            return None
        candidates.sort(key=lambda c: (c[0], c[1]))
        _, digest, key = candidates[0]
        self._env_used[digest] = now
        return {"key": key, "id": digest}

    async def report_failure(self, provider: str, key: str) -> None:
        """失败上报：Redis 侧单键 EVAL 原子盖冷却/计阈值；无 Redis 退进程内软冷却。"""
        now = time.time()
        if self._redis is not None:
            for src in self._sources(provider):
                field = _key_id(src, key)
                try:
                    hit = await self._redis.eval(
                        _FAIL_SCRIPT,
                        1,
                        f"{LLM_KEY_HASH_PREFIX}{src}",
                        field,
                        str(now),
                        str(COOLDOWN_SOFT),
                        str(COOLDOWN_HARD),
                        str(FAIL_WINDOW),
                        str(FAIL_THRESHOLD),
                    )
                except Exception as exc:  # noqa: BLE001 - 记账失败不应打断调用方
                    logger.warning("KeyRing report_failure EVAL failed: %s", exc)
                    continue
                # -1 = 该 digest 不在此源 HASH，继续下一源；0/1 = 命中并记账（1 表示已达阈值升级）。
                if hit is not None and int(hit) >= 0:
                    if int(hit) == 1:
                        logger.warning(
                            "KeyRing: key auto-cooled to hard window (fail threshold hit) "
                            "provider=%s digest=%s",
                            src,
                            field,
                        )
                    return
        async with self._lock:
            for src in self._sources(provider):
                digest = _key_id(src, key)
                if self._env.get(src):
                    until = now + COOLDOWN_SOFT
                    if until > self._env_cool.get(digest, 0.0):
                        self._env_cool[digest] = until
