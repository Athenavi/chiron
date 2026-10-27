"""Redis 适配层的实测。

这里只验证"探测流程本身是通的"，**支持/不支持**的结论由 ``scripts/verify_p0.py`` 输出，
避免把某版本 fakeredis 的能力写死在断言里。

注意：**不要**在模块级 ``importorskip("fakeredis")``。后端顺序是
``server → bundled → fakeredis``，没装 fakeredis 时前两档仍然可以跑；模块级跳过会让
bundled（本地模式最常用的那一档）完全不测，等于把最重要的路径藏起来。
需要 fakeredis 的确切断言放到具体用例里按条件跳过。
"""

from __future__ import annotations

import importlib.util

import pytest

from app.config import Settings
from app.storage.redis import PROBE_OPS, RedisServer

HAS_FAKEREDIS = importlib.util.find_spec("fakeredis") is not None


async def test_probe_covers_every_contract_command(tmp_path) -> None:
    # data_dir 指到 tmp_path：内置 redis 会把 dump.rdb 写在 <data-dir>/redis 下，
    # 用真实用户目录会污染本机数据（也违反"测试只碰临时目录"的约定）。
    settings = Settings(pg_enabled=False, redis_enabled=True, data_dir=tmp_path / "data")
    redis = RedisServer(settings)
    await redis.start()
    try:
        available = {"server", "bundled"} | ({"fakeredis"} if HAS_FAKEREDIS else set())
        assert redis.backend in available, redis.error
        results = await redis.probe()
    finally:
        await redis.stop()

    assert [result.op for result in results] == list(PROBE_OPS)
    assert all(result.detail for result in results), "每条结论都必须带实测明细（含失败原因）"
    # XADD 是 Streams 的基础，fakeredis / redis 都应支持；不成立说明环境异常
    xadd = next(result for result in results if result.op == "XADD")
    assert xadd.ok, xadd.detail


async def test_eval_verdict_matches_backend(tmp_path) -> None:
    """EVAL 的结论必须和 backend 对得上：bundled/server 真支持 Lua，fakeredis 不支持。

    为什么要单独断言：``fakeredis`` 前七条命令全 PASS，只有 EVAL 报
    ``unknown command 'eval'``。若某次改动让 ``probe()`` 漏掉 EVAL，这里会立刻发现
    （PROBE_OPS 与结果列表长度不等），而不是在上线后才由引擎的 Lua 脚本炸出来。
    """
    settings = Settings(pg_enabled=False, redis_enabled=True, data_dir=tmp_path / "data")
    redis = RedisServer(settings)
    await redis.start()
    try:
        results = await redis.probe()
        backend = redis.backend
    finally:
        await redis.stop()

    eval_result = next(result for result in results if result.op == "EVAL")
    assert redis.lua_supported == eval_result.ok
    if backend in {"bundled", "server"}:
        assert eval_result.ok, eval_result.detail
    if backend == "fakeredis":
        # 桩的解释器缺失是**已知事实**，不是环境异常；这里只锁住"结论如实上报"
        assert not eval_result.ok
        assert "eval" in eval_result.detail.lower()


async def test_probe_marks_everything_failed_without_client() -> None:
    redis = RedisServer(Settings(pg_enabled=False, redis_enabled=False))
    await redis.start()  # 关掉的存储 → backend=disabled，客户端为空
    results = await redis.probe()

    assert len(results) == len(PROBE_OPS)
    assert not any(result.ok for result in results)
