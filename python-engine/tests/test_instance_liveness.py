"""M17：实例存活心跳（``app/instance_liveness.py``）—— 线账死预留清扫的判据来源。

这里判的是**协议本身**：什么时候算"参与了协议"、存活键写在哪、探测的形状（一次一个键）、
自我豁免、正向缓存的方向。清扫怎么用这些判据在 ``tests/test_line_ledger.py`` 里判。

方向性约定贯穿全部用例：**问不出来就算活着**。这套机制唯一能做的事是"提前收回已死实例的
预留"，它一旦在证据不足时删东西，制造的是**少算**（上限静默失效）—— 线账最坏的一类错误，
比"多等一个 TTL"严重得多。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app import instance_liveness as il


async def _reset_module_state() -> None:
    active = il._ACTIVE
    il._ACTIVE = None
    il._PROBE_CACHE.clear()
    if active is not None and active._task is not None:
        active._task.cancel()
        try:
            await active._task
        except asyncio.CancelledError:
            pass
        active._task = None
        active._started = False


@pytest.fixture(autouse=True)
async def _module_state_is_not_shared():
    """心跳登记与探测缓存都是模块级状态：用例之间必须归零。

    setup 也清一次：`monkeypatch.setitem` 的还原时机与本 fixture 的先后**不受控制**
    （实测只在 teardown 清时，被还原的那条"活着"的缓存会活到下一个用例，把它的答案改掉）。
    """
    await _reset_module_state()
    yield
    await _reset_module_state()


class _FakeAlive:
    """只记账、不解释语义的 Redis 替身（``set`` / ``delete`` / ``exists``）。"""

    def __init__(
        self, *, existing: set[str] | None = None, error: bool = False, probe_error: bool = False
    ) -> None:
        self.existing = existing or set()
        self.error = error
        self.probe_error = probe_error
        self.sets: list[tuple[str, int]] = []
        self.deletes: list[str] = []
        self.exists_calls: list[tuple[str, ...]] = []

    async def set(self, key: str, value: str, ex: int = 0) -> None:
        if self.error:
            raise RuntimeError("redis down")
        self.sets.append((key, ex))
        self.existing.add(key)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.existing:
                self.existing.discard(key)
                removed += 1
            self.deletes.append(key)
        return removed

    async def exists(self, key: str) -> int:
        # 记录**整条参数元组**：Cluster 下多键 EXISTS 会 CROSSSLOT，这里要能看出只传了一个键
        self.exists_calls.append((key,))
        if self.error or self.probe_error:
            raise RuntimeError("redis down")
        return 1 if key in self.existing else 0


def _hb(redis: Any, instance_id: str = "engine-A") -> il.InstanceLiveness:
    return il.InstanceLiveness(redis=redis, instance_id=instance_id)


# ── 协议的加入与退出 ────────────────────────────────────────────────────


class TestParticipation:
    async def test_start_writes_the_key_before_the_first_tick(self) -> None:
        """心跳循环第一拍要等一个周期，而别的实例随时可能来问 —— 所以启动时**同步写一次**。"""
        redis = _FakeAlive()
        hb = _hb(redis)
        assert await hb.start() is True
        try:
            assert redis.sets == [(il.alive_key("engine-A"), il.ALIVE_TTL_SECONDS)]
            assert il.participates() is True
        finally:
            await hb.stop()

    async def test_no_redis_means_no_participation(self, monkeypatch) -> None:
        """没配 Redis：不写键、不宣称参与 —— 线账的清扫随之禁用。"""

        # 生产里的 `_connect` 把任何失败都收敛成 None（没配 Redis 是正常部署形态），
        # 这里必须照这个形状来 —— 直接抛会让 start() 把异常漏给调用方。
        async def _none() -> Any:
            return None

        monkeypatch.setattr(il, "_connect", _none)
        hb = il.InstanceLiveness(redis=None, instance_id="engine-A")
        assert await hb.start() is False
        assert il.participates() is False
        assert await il.holders_alive(["engine-B"]) == {}

    async def test_no_instance_id_means_no_participation(self) -> None:
        """没有身份就没有归属：宁可不开清扫，也不写一个空键名（``engine:alive:``）。"""
        hb = _hb(_FakeAlive(), instance_id="")
        assert await hb.start() is False
        assert il.participates() is False

    async def test_write_failure_does_not_claim_participation(self) -> None:
        """键没写成就算没参与 —— 否则本进程会拿着"读不到别人的键"这条**没有依据的证据**去删预留。"""
        redis = _FakeAlive(error=True)
        hb = _hb(redis)
        assert await hb.start() is False
        assert il.participates() is False
        assert redis.sets == []

    async def test_stop_deletes_the_key_and_ends_participation(self) -> None:
        """优雅退出要**主动告别**：留着键，别的实例会在整个 TTL 窗口里以为这个实例还在跑。"""
        redis = _FakeAlive()
        hb = _hb(redis)
        assert await hb.start() is True
        await hb.stop()
        assert redis.deletes == [il.alive_key("engine-A")]
        assert il.participates() is False
        assert hb._task is None

    async def test_restart_after_stop_works(self) -> None:
        """``stop()`` 之后同一个对象还能再起（lifespan 之外没有别的管理者，测一下别锁死）。"""
        redis = _FakeAlive()
        hb = _hb(redis)
        await hb.start()
        await hb.stop()
        assert await hb.start() is True
        assert il.participates() is True
        await hb.stop()

    async def test_a_stale_stop_does_not_unregister_the_live_one(self) -> None:
        """旧实例的 ``stop()`` 不许把新心跳的登记抹掉 —— 抹掉等于本进程突然"退出协议"，
        而它下一秒还在写键。"""
        redis = _FakeAlive()
        old = _hb(redis, "engine-OLD")
        new = _hb(redis, "engine-NEW")
        await old.start()
        await new.start()
        await old.stop()
        assert il.participates() is True
        assert il.self_id() == "engine-NEW"
        await new.stop()

    def test_ttl_covers_the_refresh_interval_and_the_probe_cache(self) -> None:
        """常量之间的**关系**才是契约：TTL 要给抖动留余量，探测缓存必须短于 TTL，
        否则"缓存说它活着"会活过"它的键其实已经过期"。"""
        assert il.ALIVE_TTL_SECONDS >= 3 * il.ALIVE_INTERVAL_SECONDS
        assert il.ALIVE_PROBE_CACHE_SECONDS < il.ALIVE_TTL_SECONDS
        assert il.alive_key("a").endswith("engine:alive:a")


# ── 探测 ────────────────────────────────────────────────────────────────


class TestProbing:
    async def test_absent_alive_key_reads_as_dead(self) -> None:
        redis = _FakeAlive(existing={il.alive_key("engine-B")})
        hb = _hb(redis)
        await hb.start()
        try:
            got = await il.holders_alive(["engine-B", "engine-C"])
            assert got == {"engine-B": True, "engine-C": False}
        finally:
            await hb.stop()

    async def test_self_is_exempt_without_a_redis_call(self) -> None:
        """自我豁免：本进程的心跳键还没落（刚启动、抖了一下）时，一次核对会把自己正在跑的
        run 的预留删掉 —— 那是自己把自己的闸门拆了。"""
        redis = _FakeAlive()  # existing 里**没有** engine-A：只有豁免能救它
        hb = _hb(redis, "engine-A")
        await hb.start()
        try:
            assert await il.holders_alive(["engine-A"]) == {"engine-A": True}
            assert redis.exists_calls == []
        finally:
            await hb.stop()

    async def test_probe_failure_reads_as_alive(self) -> None:
        """问不出的方向是**不删**：Redis 抖动不该让一条线的预留被全站收回。

        只让 `exists` 失败：`set` 必须还能写，否则心跳起不来，本用例就退化成"没参与 ⇒
        返回空"，跟"探测失败 ⇒ 判活"是两件事（后者才是这里要钉的）。
        """
        redis = _FakeAlive(probe_error=True)
        hb = _hb(redis)
        await hb.start()
        try:
            assert await il.holders_alive(["engine-B"]) == {"engine-B": True}
        finally:
            await hb.stop()

    async def test_a_live_holder_is_answered_from_cache(self) -> None:
        """正向缓存就是清扫的成本上限：30s 冷却 × 每实例一次 EXISTS，缓存后连这个都省掉。"""
        redis = _FakeAlive(existing={il.alive_key("engine-B")})
        hb = _hb(redis)
        await hb.start()
        try:
            await il.holders_alive(["engine-B"])
            await il.holders_alive(["engine-B"])
            assert len(redis.exists_calls) == 1
        finally:
            await hb.stop()

    async def test_a_dead_holder_is_never_cached(self) -> None:
        """负向结论不缓存：读到死就当场删，缓存它只会让同一批成员被反复判死、反复删。"""
        redis = _FakeAlive()
        hb = _hb(redis)
        await hb.start()
        try:
            await il.holders_alive(["engine-B"])
            await il.holders_alive(["engine-B"])
            assert len(redis.exists_calls) == 2
        finally:
            await hb.stop()

    async def test_the_cache_ages_out(self, monkeypatch) -> None:
        """缓存是**有时限**的：过了 ``ALIVE_PROBE_CACHE_SECONDS`` 必须重新问 ——
        实例被杀而它的键还在的窗口，就是这段。"""
        redis = _FakeAlive(existing={il.alive_key("engine-B")})
        hb = _hb(redis)
        await hb.start()
        try:
            await il.holders_alive(["engine-B"])
            monkeypatch.setitem(il._PROBE_CACHE, "engine-B", time.monotonic() - 1)
            await il.holders_alive(["engine-B"])
            assert len(redis.exists_calls) == 2
        finally:
            await hb.stop()

    async def test_every_probe_touches_exactly_one_key(self) -> None:
        """Cluster 下多键命令会 CROSSSLOT（``engine:alive:B`` 与 ``engine:alive:C`` 不同槽）——
        线账的脚本早就因此规定"一个 EVAL 只碰一个键"，探测同规矩。"""
        redis = _FakeAlive()
        hb = _hb(redis)
        await hb.start()
        try:
            await il.holders_alive(["engine-B", "engine-C", "engine-D"])
            assert [len(call) for call in redis.exists_calls] == [1, 1, 1]
        finally:
            await hb.stop()

    async def test_empty_holders_and_blank_holders_are_skipped(self) -> None:
        redis = _FakeAlive()
        hb = _hb(redis)
        await hb.start()
        try:
            assert await il.holders_alive([]) == {}
            assert await il.holders_alive([""]) == {}
            assert redis.exists_calls == []
        finally:
            await hb.stop()

    async def test_holders_alive_without_participation_asks_nothing(self) -> None:
        """没参与就不探测：返回 ``{}``，调用方据此**什么都不删**。"""
        assert il.participates() is False
        assert await il.holders_alive(["engine-B"]) == {}


# ── 心跳循环本身 ────────────────────────────────────────────────────────


class TestHeartbeatLoop:
    async def test_the_loop_renews_the_key(self, monkeypatch) -> None:
        redis = _FakeAlive()
        monkeypatch.setattr(il, "ALIVE_INTERVAL_SECONDS", 0)
        hb = _hb(redis)
        await hb.start()
        try:
            await asyncio.sleep(0.05)
            assert len(redis.sets) >= 2, "续期没跑起来：键会在 TTL 到点后被判成已死"
        finally:
            await hb.stop()

    async def test_a_flaky_write_does_not_kill_the_loop(self, monkeypatch) -> None:
        """``set`` 抛一次不该让心跳从此停摆：那样本进程会一路"已死"到重启。"""
        redis = _FakeAlive()
        monkeypatch.setattr(il, "ALIVE_INTERVAL_SECONDS", 0)
        hb = _hb(redis)
        await hb.start()
        try:
            real_set = redis.set

            async def _flaky(key: str, value: str, ex: int = 0) -> None:
                redis.set = real_set  # 只失败一次
                raise RuntimeError("redis down")

            redis.set = _flaky  # type: ignore[method-assign]
            await asyncio.sleep(0.05)
            assert len(redis.sets) >= 2
            assert il.participates() is True
        finally:
            redis.set = real_set  # type: ignore[assignment]
            await hb.stop()

    async def test_a_lost_connection_ends_participation(self, monkeypatch) -> None:
        """循环退出 = 再没人续期这个键 ⇒ 立刻退出协议，别再依据它删别人的预留。"""

        async def _no_redis() -> Any:
            return None

        redis = _FakeAlive()
        monkeypatch.setattr(il, "ALIVE_INTERVAL_SECONDS", 0)
        hb = _hb(redis)
        await hb.start()
        assert il.participates() is True
        monkeypatch.setattr(il, "_connect", _no_redis)
        hb._redis = None  # 模拟连接没了（下一拍补不回来）
        await asyncio.wait_for(hb._task, timeout=1)
        assert il.participates() is False
        assert hb.started is False
        await hb.stop()
