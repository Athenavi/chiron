"""Redis 适配层的实测（fakeredis 缺失则整体跳过）。

这里只验证"探测流程本身是通的"，**支持/不支持**的结论由 ``scripts/verify_p0.py`` 输出，
避免把某版本 fakeredis 的能力写死在断言里。
"""

from __future__ import annotations

import pytest

pytest.importorskip("fakeredis", reason="未安装 fakeredis（pip install -e .[dev] 之外还需 fakeredis）")

from app.config import Settings  # noqa: E402
from app.storage.redis import PROBE_OPS, RedisServer  # noqa: E402


async def test_probe_covers_every_contract_command() -> None:
    settings = Settings(pg_enabled=False, redis_enabled=True)
    redis = RedisServer(settings)
    await redis.start()
    try:
        assert redis.backend in {"fakeredis", "server"}, redis.error
        results = await redis.probe()
    finally:
        await redis.stop()

    assert [result.op for result in results] == list(PROBE_OPS)
    assert all(result.detail for result in results), "每条结论都必须带实测明细（含失败原因）"
    # XADD 是 Streams 的基础，fakeredis / redis 都应支持；不成立说明环境异常
    xadd = next(result for result in results if result.op == "XADD")
    assert xadd.ok, xadd.detail


async def test_probe_marks_everything_failed_without_client() -> None:
    redis = RedisServer(Settings(pg_enabled=False, redis_enabled=False))
    await redis.start()  # 关掉的存储 → backend=disabled，客户端为空
    results = await redis.probe()

    assert len(results) == len(PROBE_OPS)
    assert not any(result.ok for result in results)
