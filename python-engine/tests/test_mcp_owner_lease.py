"""MCP owner 租约 / 跨实例桥 / 活跃用户连接预算的**行为**验证。

为什么值得单独一套用例
----------------------
DSH（DeepSeek Harness）是本地单用户 harness，架构上**不存在**"多个实例共享同一批用户"
这件事；Chiron 是多副本 SaaS，MCP 连接规模 ≈ 实例数 × 活跃用户数 × server 数，扩容会线性
放大对第三方 MCP server 的连接。owner 租约 + 连接预算是为此而设（见
docs/dsh-gap-analysis.md 切口 #2）。但这套机制此前**一条用例都没有**，
`vendor/规划.md` §3.2 因此长期把它列在"缺实测证据"里。

分两层：
* **不依赖 Redis**（默认套件跑）：未启用租约时必须保持单实例语义（零行为变化）；
  bridge 在无 Redis 时必须**显式失败**而不是返回空结果。
* **依赖真实 Redis**（`-m integration`，由 CI 的 real-stack job 提供 Redis）：
  租约互斥 / CAS 续期与释放 / TTL 下限 / 工具清单往返 / 桥的往返·远端报错·超时。
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import pytest
import redis.asyncio as aioredis

from app.plugins.owner_lease import (
    MCPBridge,
    MCPOwnerLease,
    _invoke_channel,
    _owner_key,
    _reply_channel,
    _tools_key,
)
from app.plugins.store import ActiveTracker

REDIS_URL = (
    os.getenv("CHIRON_TEST_REDIS_URL")
    or os.getenv("REDIS_URL")
    or "redis://127.0.0.1:6379/0"
)


def _uid(tag: str) -> str:
    return f"pytest-mcp-{tag}-{uuid.uuid4().hex[:12]}"


async def _echo_handler(tool: str, args: dict[str, Any]) -> Any:
    return {"tool": tool, "args": args}


async def _boom_handler(tool: str, args: dict[str, Any]) -> Any:
    raise ValueError("远端炸了")


# ── 不依赖 Redis（默认套件）──────────────────────────────────────────────


async def test_disabled_lease_keeps_single_instance_semantics():
    """未启用（无 Redis）时视作"自己持有"，且不产生任何副作用 —— 单实例行为不变。"""
    lease = MCPOwnerLease(None, "inst-a", 60)
    assert lease.enabled is False
    assert await lease.acquire("u1") is True
    assert await lease.renew("u1") is True
    assert await lease.owner_of("u1") is None
    await lease.release("u1")  # 不应抛异常
    await lease.publish_tools("u1", [{"name": "t"}])
    assert await lease.read_tools("u1") == []


async def test_bridge_without_redis_fails_loudly():
    """无 Redis 时调用必须**显式报错** —— 不能返回空结果让 LLM 基于错误继续。"""
    bridge = MCPBridge(None, "inst-a", _echo_handler)
    with pytest.raises(RuntimeError):
        await bridge.invoke("owner-x", "tool", {})
    await bridge.start()  # 无 Redis：start 是空操作
    await bridge.stop()


def test_active_users_sorted_budget_keeps_most_recent():
    """连接预算的用户截断：只服务最近活跃的前 N 个；`limit=0` 表示不限制。

    这是 `pool.reconcile` 里 `active_users_sorted(limit=mcp_max_users_per_instance)` 的
    纯函数部分，不需要 Redis。时间戳直接注入，避免依赖 `sleep` 的调度精度。
    """
    tracker = ActiveTracker()
    now = time.time()
    tracker._last = {f"u{i}": now - (5 - i) for i in range(5)}  # u4 最新

    assert tracker.active_users_sorted(limit=2) == ["u4", "u3"]
    assert tracker.active_users_sorted(limit=0) == ["u4", "u3", "u2", "u1", "u0"]


# ── 依赖真实 Redis（-m integration）──────────────────────────────────────


@pytest.fixture
async def redis_client():
    client = aioredis.from_url(REDIS_URL, decode_responses=False)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        pytest.fail(f"需要可达的 Redis（{REDIS_URL}）才能跑 MCP owner 租约的集成用例：{exc}")
    try:
        yield client
    finally:
        await client.aclose()


async def _wait_subscribers(redis: Any, channel: str, timeout: float = 5.0) -> None:
    """等到该频道真的有订阅者再发消息 —— pub/sub 订阅是异步建立的，直接 publish 会丢。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            entries = await redis.pubsub_numsub(channel)
        except Exception:  # noqa: BLE001
            entries = []
        for ch, count in entries:
            name = ch.decode() if isinstance(ch, bytes) else ch
            if name == channel and count >= 1:
                return
        await asyncio.sleep(0.05)
    raise AssertionError(f"等待订阅者超时：{channel}")


@pytest.mark.integration
async def test_lease_is_exclusive_and_release_frees_it(redis_client):
    uid = _uid("excl")
    a = MCPOwnerLease(redis_client, "inst-a", 60)
    b = MCPOwnerLease(redis_client, "inst-b", 60)
    try:
        assert await a.acquire(uid) is True
        assert await b.acquire(uid) is False, "同一用户不得有两个 owner"
        assert await a.owner_of(uid) == "inst-a"

        await a.release(uid)
        assert await b.acquire(uid) is True, "owner 释放后其它实例必须能接管"
        assert await b.owner_of(uid) == "inst-b"
    finally:
        await a.release(uid)
        await b.release(uid)


@pytest.mark.integration
async def test_non_owner_cannot_renew_or_release(redis_client):
    """续期/释放是 CAS：非 owner 既不能续期，也不能删掉别人的租约。"""
    uid = _uid("cas")
    a = MCPOwnerLease(redis_client, "inst-a", 60)
    b = MCPOwnerLease(redis_client, "inst-b", 60)
    try:
        assert await a.acquire(uid) is True
        assert await b.renew(uid) is False

        await b.release(uid)
        assert await a.owner_of(uid) == "inst-a", "非 owner 的 release 不得误删后继/现任租约"
        assert await a.renew(uid) is True, "owner 自己续期必须成功"
    finally:
        await a.release(uid)


@pytest.mark.integration
async def test_lease_ttl_is_applied_and_clamped(redis_client):
    """租约必须有 TTL（否则 owner 崩溃会永久占位），且不低于 30s 下限。"""
    uid = _uid("ttl")
    low = MCPOwnerLease(redis_client, "inst-low", 1)  # 低于下限 → 夹到 30
    standard = MCPOwnerLease(redis_client, "inst-std", 120)
    try:
        assert await low.acquire(uid) is True
        ttl = await redis_client.ttl(_owner_key(uid))
        assert 0 < ttl <= 30, f"TTL 应被夹到 30s，实际 {ttl}"

        await low.release(uid)
        assert await standard.acquire(uid) is True
        ttl = await redis_client.ttl(_owner_key(uid))
        assert 0 < ttl <= 120, f"TTL 应等于配置值，实际 {ttl}"
    finally:
        await low.release(uid)
        await standard.release(uid)


@pytest.mark.integration
async def test_tools_manifest_roundtrip(redis_client):
    """owner 公布的工具清单必须能被其它实例原样读回（含中文），且带 TTL。"""
    uid = _uid("tools")
    lease = MCPOwnerLease(redis_client, "inst-a", 60)
    tools = [{"name": "echo", "description": "回显（中文）", "schema": {"type": "object"}}]
    try:
        await lease.publish_tools(uid, tools)
        assert await lease.read_tools(uid) == tools
        assert await redis_client.ttl(_tools_key(uid)) > 0
    finally:
        await redis_client.delete(_owner_key(uid), _tools_key(uid))


@pytest.mark.integration
async def test_bridge_roundtrip_between_two_instances(redis_client):
    """跨实例调用：caller 经 Redis 把请求发给 owner，owner 执行后按 req_id 回传。"""
    owner = MCPBridge(redis_client, "inst-owner", _echo_handler, timeout=10)
    caller = MCPBridge(redis_client, "inst-caller", _echo_handler, timeout=10)
    await owner.start()
    await caller.start()
    try:
        await _wait_subscribers(redis_client, _invoke_channel("inst-owner"))
        await _wait_subscribers(redis_client, _reply_channel("inst-caller"))

        result = await caller.invoke("inst-owner", "echo", {"中文": "值", "n": 1})
        assert result == {"tool": "echo", "args": {"中文": "值", "n": 1}}
    finally:
        await caller.stop()
        await owner.stop()


@pytest.mark.integration
async def test_bridge_propagates_remote_error(redis_client):
    """远端 handler 报错必须原样传回调用方 —— 不吞成空结果。"""
    owner = MCPBridge(redis_client, "inst-owner-err", _boom_handler, timeout=10)
    caller = MCPBridge(redis_client, "inst-caller-err", _echo_handler, timeout=10)
    await owner.start()
    await caller.start()
    try:
        await _wait_subscribers(redis_client, _invoke_channel("inst-owner-err"))
        await _wait_subscribers(redis_client, _reply_channel("inst-caller-err"))

        with pytest.raises(RuntimeError, match="远端炸了"):
            await caller.invoke("inst-owner-err", "boom", {})
    finally:
        await caller.stop()
        await owner.stop()


@pytest.mark.integration
async def test_bridge_times_out_loudly_when_owner_absent(redis_client):
    """owner 不可达：超时后抛 RuntimeError（报错里点明原因），而不是静默返回空。"""
    caller = MCPBridge(redis_client, "inst-caller-nobody", _echo_handler, timeout=1)
    await caller.start()
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="超时"):
            await caller.invoke("inst-nobody", "tool", {})
        assert time.monotonic() - started < 5
    finally:
        await caller.stop()
