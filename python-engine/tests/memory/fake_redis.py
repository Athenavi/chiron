"""最小 Redis 替身（in-memory double）—— 覆盖 ``ConflictManager`` 的命令集 + TTL。

为什么自带一份而不是引 ``fakeredis``：它不在依赖清单里。而这里的测试只关心四件事
——「登记/裁决/删除都经 Redis」「key 带 tenant」「TTL 到点自动消失」「多副本共享同一份」
—— 都用不上真实 Redis 的其余能力。一个时钟可显式推进（``advance``）的替身，比一个新
依赖更可控：过期是确定性的，不靠 sleep。

语义边界：只实现 ``ConflictManager`` 实际调用的命令，并让过期表现为「读到即不存在」
（Redis 真实行为是惰性 + 定期淘汰，测试只需要前者）。
"""

from __future__ import annotations

from typing import Any


class FakeRedis:
    """带 TTL 的最小内存 Redis。"""

    def __init__(self) -> None:
        self._kv: dict[str, bytes] = {}
        self._sets: dict[str, set[bytes]] = {}
        self._expiry: dict[str, float] = {}
        self._now: float = 0.0

    # ── 测试工具 ──────────────────────────────────────────────────────

    def advance(self, seconds: float) -> None:
        """把虚拟时钟向前拨，用于触发 TTL 过期。"""
        self._now += seconds

    @staticmethod
    def _key(key: Any) -> str:
        return key.decode("utf-8") if isinstance(key, bytes) else str(key)

    @staticmethod
    def _val(value: Any) -> bytes:
        return value if isinstance(value, bytes) else str(value).encode("utf-8")

    def _purge(self, key: str) -> bool:
        """惰性过期：读到过期键就顺手删掉，返回它是否已过期。"""
        exp = self._expiry.get(key)
        if exp is not None and self._now >= exp:
            self._kv.pop(key, None)
            self._sets.pop(key, None)
            self._expiry.pop(key, None)
            return True
        return False

    # ── ConflictManager 用到的命令 ────────────────────────────────────

    async def setex(self, key: Any, ttl: int, value: Any) -> bool:
        k = self._key(key)
        self._kv[k] = self._val(value)
        self._expiry[k] = self._now + ttl
        return True

    async def get(self, key: Any) -> bytes | None:
        k = self._key(key)
        self._purge(k)
        return self._kv.get(k)

    async def delete(self, *keys: Any) -> int:
        removed = 0
        for key in keys:
            k = self._key(key)
            if k in self._kv or k in self._sets:
                removed += 1
            self._kv.pop(k, None)
            self._sets.pop(k, None)
            self._expiry.pop(k, None)
        return removed

    async def sadd(self, key: Any, *members: Any) -> int:
        k = self._key(key)
        self._purge(k)
        s = self._sets.setdefault(k, set())
        added = 0
        for m in members:
            b = self._val(m)
            if b not in s:
                s.add(b)
                added += 1
        return added

    async def srem(self, key: Any, *members: Any) -> int:
        k = self._key(key)
        self._purge(k)
        s = self._sets.get(k, set())
        removed = 0
        for m in members:
            b = self._val(m)
            if b in s:
                s.discard(b)
                removed += 1
        return removed

    async def smembers(self, key: Any) -> set[bytes]:
        k = self._key(key)
        self._purge(k)
        return set(self._sets.get(k, set()))

    async def expire(self, key: Any, ttl: int) -> bool:
        k = self._key(key)
        self._purge(k)
        if k in self._kv or k in self._sets:
            self._expiry[k] = self._now + ttl
            return True
        return False

    async def incr(self, key: Any) -> int:
        k = self._key(key)
        self._purge(k)
        current = int(self._kv.get(k, b"0")) + 1
        self._kv[k] = str(current).encode("utf-8")
        return current
