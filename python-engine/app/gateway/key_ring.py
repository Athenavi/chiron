"""KeyRing — 引擎侧 LLM 密钥环(集中派 DR 的引擎运行时组件)。

V1(LocalKeyRing 语义):以 Redis keyset(llm:keys:{provider})为权威镜像,
辅以 env 种子(settings.xxx_api_key)兜底;访问时按 freshness 刷新(默认 3s),
返回 active 的明文 key。失败上报:本地冷却 + Redis 共享失败计数(窗口 TTL),
达阈值把 keyset 字段置 circuit_open 并附冷却到期时间戳。

V2 预留:把本类替换为"Redis Lua 原子取 key"实现,保持 active_keys/report_failure
同接口即可平滑切换(见 docs/llm-provider-key-management-dr.md)。

Redis 不可用/为空时:仅返回 env 种子(单机降级),与 Redis 故障语义文档一致。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

logger = logging.getLogger(__name__)

from app.redis_keys import rkey

LLM_KEY_HASH_PREFIX = rkey("llm:keys:")
LLM_FAIL_PREFIX = rkey("llm:fail:")
# 共享失败计数窗口(秒)与停用阈值
FAIL_WINDOW = 60
FAIL_THRESHOLD = 5
# 本地冷却(秒):单实例侧软停用,避免反复打同一个坏 key
LOCAL_COOLDOWN = 30
# 本地镜像 freshness(秒):管理端变更在此延迟内收敛到各引擎实例
FRESHNESS = 3


def _key_id(provider: str, key: str) -> str:
    import hashlib

    return hashlib.sha256(f"{provider}:{key}".encode()).hexdigest()[:12]


class KeyRing:
    """LLM 密钥环(V1 本地环;同接口预留 V2 Redis 原子实现)。"""

    def __init__(self, redis=None, env_seeds: dict[str, list[str]] | None = None):
        self._redis = redis
        self._env = env_seeds or {}
        # provider -> digest12 -> {"key": 明文, "status": s, "cooldown": epoch, "manual": bool}
        self._cache: dict[str, dict[str, dict]] = {}
        self._updated: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _env_digests(self, provider: str):
        out = {}
        for key in self._env.get(provider, []):
            out[_key_id(provider, key)] = {"key": key, "status": "active", "cooldown": 0.0, "manual": False}
        return out

    async def _refresh(self, provider: str) -> None:
        now = time.time()
        cached = provider in self._updated and now - self._updated[provider] < FRESHNESS
        if cached:
            return
        # 依次尝试「本 provider」与「同产品基础 provider」的 keyset。
        #
        # 为什么需要：同产品的多协议变体（如 opencode-go / opencode-go-anthropic）在 keyset 里
        # 按 **preset id** 分开存，但共享同一个上游 key。只配了一处时，另一条协议线取不到 key，
        # 而 _resolve_client 取不到会**静默回退到 placeholder key** ⇒ 拿假 key 打上游 ⇒
        # 上游返回 "401 Invalid API key"（而那个 key 其实是好的，直连实测 200）。
        # 这里按命名约定回退：`X-anthropic` 取不到就读 `X`。
        sources = [provider]
        for _suf in ("-anthropic",):
            if provider.endswith(_suf):
                sources.append(provider[: -len(_suf)])
                break

        ring: dict[str, dict] = {}
        for _src in sources:
            ring.update(self._env_digests(_src))
            if ring:
                break
        if self._redis is not None:
            for _src in sources:
                if ring:
                    break
                try:
                    raw = await self._redis.hgetall(f"{LLM_KEY_HASH_PREFIX}{_src}")
                    for digest, payload in (raw or {}).items():
                        digest = digest.decode() if isinstance(digest, bytes) else digest
                        try:
                            item = json.loads(payload.decode() if isinstance(payload, bytes) else payload)
                        except (ValueError, TypeError):
                            continue
                        key = str(item.get("k") or "")
                        if not key:
                            continue
                        status = str(item.get("s") or "active")
                        cooldown = float(item.get("c") or 0.0)
                        ring[digest] = {"key": key, "status": status, "cooldown": cooldown, "manual": True}
                except Exception as exc:
                    logger.warning("KeyRing refresh failed (redis unavailable?): %s", exc)
                    # 保留 env 兜底;镜像不刷新
        self._cache[provider] = ring
        self._updated[provider] = time.time()

    async def active_keys(self, provider: str) -> list[dict]:
        """返回该 provider 当前可用(key 明文+digest)列表;过滤 manual 停用/熔断/本地冷却。"""
        async with self._lock:
            await self._refresh(provider)
        now = time.time()
        ring = self._cache.get(provider, {})
        out = []
        for digest, item in ring.items():
            if item["status"] != "active":
                continue
            if item["cooldown"] and now < item["cooldown"]:
                continue
            out.append({"key": item["key"], "id": digest})
        return out

    async def get_key(self, provider: str) -> dict | None:
        """取一个可用 key(V1 轮询第一个;V2 替换为 Redis 原子加权选择)。"""
        keys = await self.active_keys(provider)
        if not keys:
            return None
        # 简单轮询:把已取过的移到队尾
        picked = keys[0]
        async with self._lock:
            ring = self._cache.get(provider)
            if ring and picked["id"] in ring:
                ring[picked["id"]] = ring.pop(picked["id"])
        return picked

    async def report_failure(self, provider: str, key: str) -> None:
        """失败上报:本地冷却 + Redis 共享计数(达阈值全局停用并冷却 60s)。"""
        digest = _key_id(provider, key)
        async with self._lock:
            ring = self._cache.setdefault(provider, {})
            item = ring.get(digest)
            if item is None:
                item = {"key": key, "status": "active", "cooldown": 0.0, "manual": False}
                ring[digest] = item
            item["cooldown"] = time.time() + LOCAL_COOLDOWN
            self._updated[provider] = time.time()

        if self._redis is None:
            return
        try:
            cnt = await self._redis.incr(f"{LLM_FAIL_PREFIX}{provider}:{digest}")
            await self._redis.expire(f"{LLM_FAIL_PREFIX}{provider}:{digest}", FAIL_WINDOW)
            if cnt >= FAIL_THRESHOLD:
                # 共享停用:写回 keyset 字段 status=circuit_open + 冷却到期时间
                cooldown_until = time.time() + FAIL_WINDOW
                async with self._lock:
                    cur = self._cache.get(provider, {}).get(digest, {})
                payload = json.dumps({"k": key, "s": "circuit_open", "c": cooldown_until})
                await self._redis.hset(f"{LLM_KEY_HASH_PREFIX}{provider}", digest, payload)
                logger.warning(
                    "KeyRing: key auto-disabled (shared fail count=%d) provider=%s digest=%s",
                    cnt, provider, digest,
                )
        except Exception as exc:
            logger.warning("KeyRing report_failure redis op failed: %s", exc)
