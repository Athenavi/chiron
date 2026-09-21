"""P2 单元测试：子 Agent 运行期缓存（Redis key 约定、TTL、事件流、降级）。

key 约定必须与 Go 侧 ``internal/api/subagent_handler.go`` 一致，因此这里逐字断言 key 串，
避免两侧漂移导致"查询不到子 Agent 进度"这类难以定位的问题。
"""
from __future__ import annotations

import pytest

from app.redis_keys import rkey
from app.subagent.runtime_cache import SubagentRuntimeCache


class _FakePipeline:
    def __init__(self, parent):
        self._parent = parent
        self._buffered: list[tuple] = []

    def hset(self, key, field=None, value=None, mapping=None, **kw):
        self._buffered.append(("hset", key, _normalize_hset(field, value, mapping, kw)))
        return self

    def expire(self, key, ttl):
        self._buffered.append(("expire", key, ttl))
        return self

    def sadd(self, key, *vals):
        self._buffered.append(("sadd", key, vals))
        return self

    async def execute(self):
        if self._parent.fail:
            raise RuntimeError("redis down")
        self._parent.calls.extend(self._buffered)
        return [1] * len(self._buffered)


def _normalize_hset(field, value, mapping, extra) -> dict:
    """把 redis-py 的两种 hset 形式（mapping= / field+value）归一成 dict 便于断言。"""
    if mapping is not None:
        return dict(mapping)
    if field is not None:
        return {field: value}
    return dict(extra)


class _FakeRedis:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail

    def pipeline(self):
        return _FakePipeline(self)

    async def hset(self, key, field=None, value=None, mapping=None, **kw):
        if self.fail:
            raise RuntimeError("redis down")
        self.calls.append(("hset", key, _normalize_hset(field, value, mapping, kw)))

    async def expire(self, key, ttl):
        if self.fail:
            raise RuntimeError("redis down")
        self.calls.append(("expire", key, ttl))

    async def xadd(self, key, fields, maxlen=None, approximate=None):
        if self.fail:
            raise RuntimeError("redis down")
        self.calls.append(("xadd", key, fields, maxlen))


def test_key_convention_matches_go_side():
    """key 串必须逐字稳定（Go 侧用同一约定读）。"""
    assert SubagentRuntimeCache.key_run("t1", "rs_1") == rkey("subagent:t1:run:rs_1")
    assert SubagentRuntimeCache.key_events("t1", "rs_1") == rkey("subagent:t1:ev:rs_1")
    assert SubagentRuntimeCache.key_children("t1", "rs_0") == rkey("subagent:t1:children:rs_0")
    assert SubagentRuntimeCache.key_tree("t1", "s1") == rkey("subagent:t1:tree:s1")


@pytest.mark.asyncio
async def test_start_run_writes_status_tree_and_children():
    redis = _FakeRedis()
    cache = SubagentRuntimeCache(redis, ttl=1800)
    await cache.start_run(run_id="rs_1", tenant="t1", root_session_id="s1",
                          parent_run_id="", depth=1, profile="reviewer", task="看代码")

    kinds = [c[0] for c in redis.calls]
    assert kinds.count("hset") == 2        # 状态 Hash + 树骨架
    assert kinds.count("expire") == 3      # 三个 key 都设 TTL
    assert kinds.count("sadd") == 1        # 父节点索引（顶层 → 根会话）
    keys = [c[1] for c in redis.calls]
    assert rkey("subagent:t1:run:rs_1") in keys
    assert rkey("subagent:t1:tree:s1") in keys
    assert rkey("subagent:t1:children:s1") in keys   # 顶层委派挂在根会话下
    ttls = {c[2] for c in redis.calls if c[0] == "expire"}
    assert ttls == {1800}


@pytest.mark.asyncio
async def test_start_run_registers_under_parent_run():
    redis = _FakeRedis()
    cache = SubagentRuntimeCache(redis)
    await cache.start_run(run_id="rs_2", tenant="t1", root_session_id="s1",
                          parent_run_id="rs_1", depth=2)
    keys = [c[1] for c in redis.calls]
    assert rkey("subagent:t1:children:rs_1") in keys   # 孙节点挂在父 run 下


@pytest.mark.asyncio
async def test_push_event_appends_to_stream_with_maxlen():
    redis = _FakeRedis()
    cache = SubagentRuntimeCache(redis, max_events=123)
    await cache.push_event(run_id="rs_1", tenant="t1", payload={"type": "subagent.text", "content": "hi"})
    call = next(c for c in redis.calls if c[0] == "xadd")
    assert call[1] == rkey("subagent:t1:ev:rs_1")
    assert call[3] == 123                 # maxlen 生效（防无界增长）
    assert '"subagent.text"' in call[2]["data"]


@pytest.mark.asyncio
async def test_update_status_and_tree_summary():
    redis = _FakeRedis()
    cache = SubagentRuntimeCache(redis)
    await cache.update_status(run_id="rs_1", tenant="t1", status="completed",
                              summary="结论", usage={"steps": 3}, result_ref="rs_1")
    await cache.update_tree_summary(tenant="t1", root_session_id="s1", run_id="rs_1",
                                    summary="结论", status="completed", depth=1, profile="reviewer")
    status_call = next(c for c in redis.calls if c[0] == "hset" and c[1].endswith("run:rs_1"))
    assert status_call[2]["status"] == "completed"
    assert status_call[2]["result_ref"] == "rs_1"
    tree_call = next(c for c in redis.calls if c[0] == "hset" and c[1].endswith("tree:s1"))
    assert "结论" in tree_call[2]["rs_1"]


@pytest.mark.asyncio
async def test_degrades_silently_and_stops_retrying():
    redis = _FakeRedis(fail=True)
    cache = SubagentRuntimeCache(redis)
    await cache.start_run(run_id="rs_1", tenant="t1", root_session_id="s1")   # 不抛
    assert cache.available is False
    before = len(redis.calls)
    await cache.push_event(run_id="rs_1", tenant="t1", payload={"type": "x"})  # 不再尝试
    assert len(redis.calls) == before


@pytest.mark.asyncio
async def test_no_redis_is_noop():
    cache = SubagentRuntimeCache(None)
    assert cache.available is False
    await cache.start_run(run_id="rs_1", tenant="t1", root_session_id="s1")
    await cache.push_event(run_id="rs_1", tenant="t1", payload={})
    await cache.update_status(run_id="rs_1", tenant="t1", status="completed")
