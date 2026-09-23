"""Redis 只允许一条连接路径（推进 X3 时删掉 unified wrapper 的回归保护）。

背景见 ``app/redis_client.py`` 的模块说明：那个适配层只实现了 get/set/delete/ping，
``xadd`` 等命令缺失 —— 于是 ``USE_UNIFIED_REDIS_CLIENT=true`` 时工作流入队**永远失败**，
而失败又被上层当成"队列暂不可达"，故障伪装成了环境问题。

这几条断言的价值不在"证明当前代码正确"，而在于**让缺口无法再悄悄出现**：
包装类少实现几个方法，类型注解和 code review 都看不出来，只有断言会拦。
"""

from __future__ import annotations

import inspect

import pytest


def test_unified_wrapper_is_gone():
    """``_UnifiedRedisWrapper`` / ``USE_UNIFIED`` / ``_get_unified_client`` 必须已删除。"""
    from app import redis_client

    assert not hasattr(redis_client, "_UnifiedRedisWrapper")
    assert not hasattr(redis_client, "USE_UNIFIED")
    assert not hasattr(redis_client, "_get_unified_client")


def test_get_redis_has_single_path():
    """``get_redis()`` 里不能再出现 unified 分支。"""
    from app.redis_client import get_redis

    source = inspect.getsource(get_redis)
    assert "Unified" not in source
    assert "USE_UNIFIED" not in source


@pytest.mark.parametrize(
    "command",
    ["xadd", "xreadgroup", "xack", "xlen", "xdel", "exists", "expire", "incr", "zadd", "eval"],
)
def test_native_client_exposes_commands_used_by_callers(command):
    """真客户端必须带着调用方实际用到的命令（wrapper 当初缺的就是这些）。

    ``xadd`` → 工作流入队；``eval`` → 租户限流的 Lua 滑动窗口；``zadd`` → 同上；
    ``incr``/``expire`` → 计数与 TTL。任何一条缺失，都会让对应链路在运行时才炸。
    """
    import redis.asyncio as aioredis

    assert hasattr(aioredis.Redis, command), f"aioredis.Redis 缺少 {command}"
