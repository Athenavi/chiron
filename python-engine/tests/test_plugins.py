"""插件模块测试：PluginStore per-user 隔离、ActiveTracker、registry owner 过滤、连接池去重共享。"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.plugins.pool import MCPClientPool
from app.plugins.store import (
    ActiveTracker,
    PluginStore,
    RedisActiveTracker,
    ServerConfig,
    active_users_key,
)
from app.tools.registry import registry

# ── PluginStore per-user 隔离 ─────────────────────────────────────────────

def test_plugin_store_per_user_isolation(tmp_path):
    store = PluginStore(tmp_path)
    store.save("u1", [ServerConfig(name="git", command="npx", args=["-y", "@modelcontextprotocol/server-git"], status="active")])
    store.save("u2", [ServerConfig(name="web", command="npx", args=["-y", "web-server"], status="inactive")])

    u1 = store.load("u1")
    assert len(u1) == 1 and u1[0].name == "git"
    # u2 的配置不影响 u1
    assert store.load("u1") == u1
    # inactive 服务器不返回
    assert store.active_servers("u2") == []
    # 未知用户返回空
    assert store.load("nobody") == []


def test_plugin_store_signature_changes_on_update(tmp_path):
    store = PluginStore(tmp_path)
    store.save("u1", [ServerConfig(name="git", command="npx", args=["-y", "git-server"])])
    sig1 = store.signature("u1")
    store.save("u1", [ServerConfig(name="git", command="npx", args=["-y", "git-server-v2"])])
    sig2 = store.signature("u1")
    assert sig1 != sig2


# ── ActiveTracker ─────────────────────────────────────────────────────────

def test_active_tracker_window():
    tracker = ActiveTracker()
    tracker.touch("u1")
    assert tracker.is_active("u1", window=3600)
    assert tracker.is_active("u2", window=3600) is False
    assert "u1" in tracker.active_users(window=3600)
    tracker.prune(window=0)  # 立即过期
    assert tracker.is_active("u1", window=0) is False


# ── ActiveTracker（Redis 共享实现） ───────────────────────────────────────


class _FakeActiveRedis:
    """只实现 RedisActiveTracker 用到的四个命令，并记录调用"""

    def __init__(self, remote: dict[str, float] | None = None, *, fail: bool = False):
        self.zadd_calls: list[tuple[str, dict[str, float]]] = []
        self.expire_calls: list[tuple[str, int]] = []
        self.zrem_calls: list[tuple[str, float]] = []
        self._remote = dict(remote or {})
        self._fail = fail

    async def zadd(self, key, mapping):
        if self._fail:
            raise ConnectionError("redis down")
        self.zadd_calls.append((key, {k: float(v) for k, v in mapping.items()}))
        self._remote.update({k: float(v) for k, v in mapping.items()})

    async def expire(self, key, ttl):
        if self._fail:
            raise ConnectionError("redis down")
        self.expire_calls.append((key, ttl))

    async def zrangebyscore(self, key, min=None, max=None, withscores=False):  # noqa: A002
        if self._fail:
            raise ConnectionError("redis down")
        lo = float(min)
        items = [(uid, ts) for uid, ts in self._remote.items() if ts >= lo]
        return items if withscores else [uid for uid, _ in items]

    async def zremrangebyscore(self, key, min=None, max=None):  # noqa: A002
        if self._fail:
            raise ConnectionError("redis down")
        hi = float(max)
        stale = [uid for uid, ts in self._remote.items() if ts <= hi]
        for uid in stale:
            del self._remote[uid]
        self.zrem_calls.append((key, hi))
        return len(stale)


@pytest.mark.asyncio
async def test_redis_tracker_touch_is_locally_visible_and_shared():
    """touch 立即可被本实例读到（读侧接口是同步的），并把标记推给其它实例"""
    redis = _FakeActiveRedis()
    tracker = RedisActiveTracker(redis)
    tracker.touch("u1")
    assert tracker.is_active("u1")

    await tracker.pull()
    key = active_users_key()
    assert redis.zadd_calls and redis.zadd_calls[0][0] == key
    assert "u1" in redis.zadd_calls[0][1]
    # 整键 TTL 兜底：全集群都空闲时标记不会永久留着
    assert redis.expire_calls == [(key, ActiveTracker.DEFAULT_WINDOW + 60)]


@pytest.mark.asyncio
async def test_redis_tracker_touch_flushes_without_an_explicit_pull():
    """touch 自己就把标记推出去（不等到下一轮 pull）：owner 可能在本次轮询里就需要它"""
    redis = _FakeActiveRedis()
    tracker = RedisActiveTracker(redis)

    tracker.touch("u1")
    await asyncio.sleep(0)  # 让 _schedule_flush 建的任务跑完

    assert "u1" in redis.zadd_calls[0][1]


@pytest.mark.asyncio
async def test_redis_tracker_pull_merges_newer_remote_marks_only():
    """远端标记只**向前**合并；超出窗口的成员既不合并，也从 ZSET 清掉"""
    now = time.time()
    redis = _FakeActiveRedis({
        "u-other-instance": now,
        "u-expired": now - ActiveTracker.DEFAULT_WINDOW - 10,
    })
    tracker = RedisActiveTracker(redis)
    local_ts = now + 5
    tracker._last["u-other-instance"] = local_ts  # noqa: SLF001 — 本地更新，不该被回退

    await tracker.pull()

    assert tracker.is_active("u-other-instance")
    assert tracker._last["u-other-instance"] == local_ts  # noqa: SLF001
    assert "u-expired" not in tracker._last  # noqa: SLF001
    assert redis.zrem_calls and redis.zrem_calls[0][1] <= now


@pytest.mark.asyncio
async def test_redis_tracker_degrades_to_in_process_when_redis_down():
    """Redis 故障不能让 MCP 轮询链失败：退回进程内行为，读侧继续可用"""
    redis = _FakeActiveRedis(fail=True)
    tracker = RedisActiveTracker(redis)
    tracker.touch("u1")
    await tracker.pull()  # 不抛

    assert redis.zadd_calls == []
    assert tracker.is_active("u1")
    assert "u1" in tracker.active_users()


@pytest.mark.asyncio
async def test_pool_shares_marks_only_with_owner_lease(monkeypatch):
    """共享标记只在 owner 租约开启时启用

    租约关闭时每个实例都会自己建连，共享反而让所有实例去抢同一批"最近 N 名用户"
    （按时间排序的前 N 名各实例算出的几乎一致），把连接放大变成重复放大。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "mcp_owner_lease_enabled", True)
    pool = MCPClientPool(redis=object())
    try:
        assert isinstance(pool._tracker, RedisActiveTracker)
        assert pool.status()["active_marks_shared"] is True
    finally:
        await pool.stop()

    monkeypatch.setattr(settings, "mcp_owner_lease_enabled", False)
    pool2 = MCPClientPool(redis=object())
    try:
        assert type(pool2._tracker) is ActiveTracker
        assert pool2.status()["active_marks_shared"] is False
    finally:
        await pool2.stop()


@pytest.mark.asyncio
async def test_reconcile_syncs_marks_before_reading_budget(tmp_path):
    """reconcile 先 pull 再取活跃列表，否则预算截断用的仍是"本实例见过的那半张图"。"""
    calls: list[str] = []

    class _RecordingTracker(ActiveTracker):
        async def pull(self) -> None:
            calls.append("pull")

    pool = MCPClientPool(store=PluginStore(tmp_path), tracker=_RecordingTracker())
    try:
        await pool.reconcile()
    finally:
        await pool.stop()
    assert calls == ["pull"]


# ── registry owner 过滤 ───────────────────────────────────────────────────

async def _fake_owner_tool(**_kw):
    return {"ok": True}


@pytest.mark.asyncio
async def test_registry_owner_isolation():
    registry.register("global_tool_x", "global", {"type": "object"}, _fake_owner_tool)
    registry.register("mcp_u1_tool", "owned by u1", {"type": "object"}, _fake_owner_tool, owner="u1")

    try:
        # u1 可见 owner 工具 + 全局工具
        names_u1 = {t["function"]["name"] for t in registry.to_openai_tools(user_id="u1")}
        assert "mcp_u1_tool" in names_u1 and "global_tool_x" in names_u1

        # u2 不可见 owner 工具，但可见全局工具
        names_u2 = {t["function"]["name"] for t in registry.to_openai_tools(user_id="u2")}
        assert "mcp_u1_tool" not in names_u2 and "global_tool_x" in names_u2

        # 执行：u2 调用 u1 的 owner 工具被拒绝；u1 可调用
        denied = await registry.execute("mcp_u1_tool", {}, user_id="u2")
        assert "not available" in denied["error"]
        allowed = await registry.execute("mcp_u1_tool", {}, user_id="u1")
        assert allowed.get("ok") is True
    finally:
        registry.unregister("global_tool_x")
        registry.unregister("mcp_u1_tool")


# ── 连接池：配置去重共享 + 用户释放 ──────────────────────────────────────

class _FakeMCPClient:
    """替代 MCPClient：start 后提供固定工具。"""

    def __init__(self, servers):
        self._servers = servers
        self.tools = []
        self.closed = False

    async def start(self):
        self.tools = [
            type("T", (), {"name": f"{self._servers[0].name}_demo_tool", "description": "demo",
                            "input_schema": {"type": "object"}})()
        ]

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_pool_shares_connection_by_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr("app.plugins.pool.MCPClient", _FakeMCPClient)

    store = PluginStore(tmp_path)
    # 声明 read_only：这个用例测的是"连接共享按指纹复用"，属于**引擎直连**路径的逻辑。
    # 未声明的 server 会改走 MCP broker（凭据不出引擎进程），不建本地连接 —— 见
    # app/plugins/pool.py 的分支与 app/mcp/client.py 的连接守卫。
    cfg = [
        ServerConfig(
            name="git", command="npx", args=["-y", "git-server"], status="active", read_only=True
        )
    ]
    store.save("u1", cfg)
    store.save("u2", cfg)  # 相同配置

    tracker = ActiveTracker()
    tracker.touch("u1")
    tracker.touch("u2")
    pool = MCPClientPool(store=store, tracker=tracker)

    try:
        await pool.reconcile()
        # 相同配置 → 只建一个共享连接
        assert len(pool._conns) == 1  # noqa: SLF001
        shared = next(iter(pool._conns.values()))
        assert shared.users == {"u1", "u2"}

        # 工具注册且归属两个用户
        tool = registry.get("git_demo_tool")
        assert tool is not None and tool.owners == {"u1", "u2"}

        # 释放 u1 后：工具仍属于 u2，连接仍被 u2 引用
        tracker._last["u1"] = 0  # noqa: SLF001 — 模拟 u1 过期
        await pool.reconcile()
        tool = registry.get("git_demo_tool")
        assert tool is not None and tool.owners == {"u2"}

        # 全部释放后：工具注销、连接关闭
        tracker._last["u2"] = 0  # noqa: SLF001
        await pool.reconcile()
        assert registry.get("git_demo_tool") is None
        assert len(pool._conns) == 0
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_deactivating_plugin_removes_its_tools(tmp_path, monkeypatch):
    """**用户显式停用**（插件页关掉开关）后，其工具必须从模型可调用集合里消失。

    与上一条的区别：上一条测的是"用户长时间不活跃 → ActiveTracker 过期"这条**被动**路径；
    这条测用户在 Plugins 页把 `status` 改成 `inactive` 的**主动**路径 —— 如果它不生效，
    表现就是"以为停了，模型其实还能调用它"（安全面与成本面都不可接受）。
    """
    monkeypatch.setattr("app.plugins.pool.MCPClient", _FakeMCPClient)

    store = PluginStore(tmp_path)
    active = ServerConfig(
        name="git", command="npx", args=["-y", "git-server"], status="active", read_only=True
    )
    store.save("u1", [active])

    tracker = ActiveTracker()
    tracker.touch("u1")
    pool = MCPClientPool(store=store, tracker=tracker)

    try:
        await pool.reconcile()
        assert registry.get("git_demo_tool") is not None, "启用中的插件，其工具应可见"

        # 用户在插件页关掉开关：同一份配置，status 改成 inactive
        store.save("u1", [ServerConfig(
            name="git", command="npx", args=["-y", "git-server"],
            status="inactive", read_only=True,
        )])
        await pool.reconcile()

        assert registry.get("git_demo_tool") is None, "停用的插件不得继续向模型暴露工具"
        assert len(pool._conns) == 0, "停用后本地连接应关闭（凭据/进程都不该留着）"
    finally:
        await pool.stop()
