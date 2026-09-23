"""MCPClientPool — 用户级 MCP 连接池（无状态友好的可重建缓存）。

- 按配置指纹去重：相同 command/args/env 的 MCP 服务器多用户共享一个子进程连接；
  工具注册到本地 registry 时合并归属用户集合（owner）。
- 25s 轮询「活跃用户」的插件配置：签名变化则重连该用户的服务器并重注册工具；
  用户不再活跃时释放其工具与连接引用（引用归零的连接关闭）。
- 状态均为进程内可重建缓存：实例重启后从 PluginStore 重新加载（幂等）。

多实例部署：各实例独立运行本池（MCP stdio 子进程无法跨实例共享）；
ActiveTracker 换 Redis 实现后，轮询范围由共享活跃标记驱动。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.mcp.client import MCPClient
from app.observability.metrics import (
    MCP_POOL_CONNECTIONS,
    MCP_POOL_REJECTED,
    MCP_POOL_USERS,
)
from app.plugins.owner_lease import MCPBridge, MCPOwnerLease
from app.plugins.store import ActiveTracker, PluginStore, ServerConfig
from app.tools.registry import SOURCE_MCP, registry

logger = logging.getLogger(__name__)

POLL_INTERVAL = 25  # 秒


def _fingerprint(server: ServerConfig) -> str:
    """配置指纹：name/command/args/env 的规范化 JSON。"""
    payload = {
        "command": server.command,
        "args": server.args,
        "env": server.env,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


@dataclass
class _SharedConnection:
    """按配置指纹共享的 MCP 连接及其用户集合。"""

    key: str
    client: MCPClient
    users: set[str] = field(default_factory=set)


_SELF_INSTANCE_ID: str | None = None


def _self_instance_id() -> str:
    """本实例标识（owner 租约用）：优先 settings.instance_id / pod_name，兜底进程内随机。"""
    global _SELF_INSTANCE_ID
    if _SELF_INSTANCE_ID:
        return _SELF_INSTANCE_ID
    import uuid as _uuid

    from app.config import settings as _settings

    _SELF_INSTANCE_ID = _settings.instance_id or _settings.pod_name or f"mcp-{_uuid.uuid4().hex[:8]}"
    return _SELF_INSTANCE_ID


class MCPClientPool:
    def __init__(
        self,
        store: PluginStore | None = None,
        tracker: ActiveTracker | None = None,
        redis=None,
    ) -> None:
        self._store = store or PluginStore()
        self._tracker = tracker or ActiveTracker()
        self._redis = redis
        # owner 租约（B1a）：启用时只有 owner 实例建 MCP 连接，其余实例注册代理工具
        self._lease: MCPOwnerLease | None = None
        self._bridge: MCPBridge | None = None
        self._owned_users: set[str] = set()
        self._conns: dict[str, _SharedConnection] = {}  # fingerprint -> shared conn
        self._user_sigs: dict[str, str] = {}  # user_id -> 已加载配置签名
        self._user_conns: dict[str, set[str]] = {}  # user_id -> 引用的指纹集合
        self._user_tools: dict[str, set[str]] = {}  # user_id -> 注册的工具名集合
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        from app.config import settings as _settings

        # owner 租约（B1a）：需要 Redis；未开启时保持单实例语义（本实例即为 owner）
        if _settings.mcp_owner_lease_enabled and self._redis is not None:
            self._lease = MCPOwnerLease(
                self._redis, _self_instance_id(), _settings.mcp_owner_lease_ttl
            )
            self._bridge = MCPBridge(
                self._redis,
                _self_instance_id(),
                self._handle_remote_invocation,
                timeout=_settings.mcp_bridge_timeout,
            )
            await self._bridge.start()
            logger.info(
                "mcp owner lease enabled (instance=%s, ttl=%ds)",
                _self_instance_id(),
                _settings.mcp_owner_lease_ttl,
            )
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _handle_remote_invocation(self, tool_name: str, args: dict) -> Any:
        """owner 侧入口：执行被其它实例转发的 MCP 工具调用（本地 registry，不再二次转发）。"""
        tool = registry.get(tool_name)
        if tool is None:
            raise RuntimeError(f"MCP tool not available on this owner instance: {tool_name}")
        return await tool.handler(**(args or {}))

    def _make_proxy_handler(self, uid: str, tool_name: str):
        """非 owner 侧的代理 handler：转发到 owner 实例执行。"""

        async def handler(**kwargs: Any) -> Any:
            owner = await self._lease.owner_of(uid)
            if not owner:
                raise RuntimeError(
                    f"MCP owner unavailable for user {uid} (lease expired); retry shortly"
                )
            return await self._bridge.invoke(owner, tool_name, kwargs)

        return handler

    async def _sync_proxy_tools_locked(self, uid: str) -> None:
        """非 owner 实例：按 owner 公布的清单注册代理工具（调用时经 Redis 转发）。"""
        owner = await self._lease.owner_of(uid) or ""
        sig = f"proxy:{owner}"
        if sig == self._user_sigs.get(uid):
            return
        await self._release_user_locked(uid)
        self._user_tools[uid] = set()
        tools = await self._lease.read_tools(uid)
        for t in tools:
            name = t.get("name") or ""
            if not name:
                continue
            registry.register(
                name=name,
                description=t.get("description") or "",
                parameters=t.get("schema") or {},
                handler=self._make_proxy_handler(uid, name),
                owner=uid,
                source=SOURCE_MCP,
            )
            self._user_tools[uid].add(name)
        self._user_sigs[uid] = sig
        if self._user_tools[uid]:
            logger.info(
                "user %s mcp proxy tools: %d (owner=%s)",
                uid,
                len(self._user_tools[uid]),
                owner or "unknown",
            )

    async def stop(self) -> None:
        if self._bridge is not None:
            await self._bridge.stop()
            self._bridge = None
        if self._lease is not None:
            for uid in list(self._owned_users):
                await self._lease.release(uid)
            self._owned_users.clear()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        async with self._lock:
            for key in list(self._conns):
                await self._close_conn(key)
            self._conns.clear()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                await self.reconcile()
            except Exception as e:  # 轮询失败不影响主流程
                logger.warning("mcp reconcile failed: %s", e, exc_info=True)

    async def reconcile(self) -> None:
        """处理活跃用户的配置变动；回收不再活跃用户。

        连接预算（B1）：活跃用户超过 mcp_max_users_per_instance 时只服务最近活跃的前 N 个，
        避免「实例数 × 用户数 × server 数」的连接放大打爆第三方 MCP server；
        被跳过的用户计入 mcp_pool_rejected_total（下一轮轮询会重试）。
        """
        from app.config import settings as _settings

        max_users = _settings.mcp_max_users_per_instance
        active_list = self._tracker.active_users_sorted(limit=max_users)
        skipped = len(self._tracker.active_users()) - len(active_list)
        if skipped > 0:
            MCP_POOL_REJECTED.inc(skipped)
            logger.warning(
                "mcp pool user budget reached (%d): serving %d most-recent users, skipped %d",
                max_users,
                len(active_list),
                skipped,
            )
        # owner 租约（B1a）：决定本轮谁是各活跃用户的 owner（抢租约 / 续期 / 放弃）
        if self._lease is not None:
            owned: set[str] = set()
            for uid in active_list:
                if await self._lease.acquire(uid):
                    owned.add(uid)
            for uid in list(self._owned_users):
                if uid in owned:
                    continue
                if await self._lease.renew(uid):
                    owned.add(uid)  # 仍持有：续期待到下一轮
                else:
                    await self._lease.release(uid)
            dropped = self._owned_users - owned
            if dropped:
                logger.info("mcp owner lease lost for %d user(s)", len(dropped))
            self._owned_users = owned
            if len(owned) < len(active_list):
                logger.debug(
                    "mcp owner: %d/%d active users owned by this instance",
                    len(owned),
                    len(active_list),
                )

        active = set(active_list)
        async with self._lock:
            # 1. 同步活跃用户
            for uid in active:
                await self._sync_user_locked(uid)
            # 2. 回收已非活跃用户
            for uid in list(self._user_sigs):
                if uid not in active:
                    await self._release_user_locked(uid)
            # 3. 关闭引用归零的连接
            for key in [k for k, c in self._conns.items() if not c.users]:
                await self._close_conn(key)
            # 4. 清理过期活跃标记（避免内存增长）
            self._tracker.prune()
        # 连接预算可观测性（B1）
        MCP_POOL_CONNECTIONS.set(len(self._conns))
        MCP_POOL_USERS.set(len(self._user_sigs))

    async def _sync_user_locked(self, uid: str) -> None:
        # owner 租约开启时：非归属实例不建 MCP 连接，改为注册代理工具（经 Redis 转发）
        if self._lease is not None and uid not in self._owned_users:
            await self._sync_proxy_tools_locked(uid)
            return
        sig = self._store.signature(uid)
        if sig == self._user_sigs.get(uid):
            return  # 配置无变动
        # 变更：先释放旧的，再按新配置建立
        await self._release_user_locked(uid)

        servers = self._store.active_servers(uid)
        self._user_conns[uid] = set()
        self._user_tools[uid] = set()
        from app.config import settings as _settings

        max_conns = _settings.mcp_max_connections_per_instance
        for server in servers:
            key = _fingerprint(server)
            shared = self._conns.get(key)
            if shared is None:
                # 连接预算（B1）：达到上限则本用户不再新建连接（共享连接仍可被复用）
                if max_conns > 0 and len(self._conns) >= max_conns:
                    MCP_POOL_REJECTED.inc()
                    logger.warning(
                        "mcp connection budget reached (%d), skip server %s for user %s",
                        max_conns,
                        server.name,
                        uid,
                    )
                    continue
                server_def = _server_to_def(server)
                if not server_def.read_only:
                    # 危险 server：引擎**不连**（assert_server_connectable 会拒）——连接与凭据
                    # 由独立 MCP broker 持有。这里只注册"经 broker 调用"的工具：功能与
                    # read_only server 等价，但凭据不落在 agent 能读到的进程里。
                    await _register_broker_tools(uid, server_def.name, self._user_tools[uid])
                    continue
                client = MCPClient([server_def])
                try:
                    await client.start()
                except Exception as e:  # 单个服务器失败不阻塞其余
                    logger.warning(
                        "mcp connect %s failed for user %s: %s", server.name, uid, e
                    )
                    continue
                shared = _SharedConnection(key=key, client=client)
                self._conns[key] = shared
                logger.info("mcp shared connection created: %s", key)
            shared.users.add(uid)
            self._user_conns[uid].add(key)
            for tool in shared.client.tools:
                registry.register(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.input_schema,
                    handler=_make_tool_handler(shared.client, tool.name),
                    owner=uid,
                    source=SOURCE_MCP,
                )
                self._user_tools[uid].add(tool.name)
        self._user_sigs[uid] = sig
        if self._user_tools[uid]:
            logger.info("user %s mcp tools: %d", uid, len(self._user_tools[uid]))
        # owner 公布工具清单（供非 owner 实例注册代理工具）
        if self._lease is not None and self._user_tools.get(uid):
            manifest = []
            for name in self._user_tools[uid]:
                t = registry.get(name)
                if t is None:
                    continue
                manifest.append(
                    {
                        "name": name,
                        "description": getattr(t, "description", "") or "",
                        "schema": getattr(t, "parameters", None) or {},
                    }
                )
            await self._lease.publish_tools(uid, manifest)

    async def _release_user_locked(self, uid: str) -> None:
        """移除用户对工具与连接的引用。"""
        for tool_name in self._user_tools.pop(uid, set()):
            tool = registry.get(tool_name)
            if tool is None:
                continue
            tool.owners.discard(uid)
            if not tool.owners:
                registry.unregister(tool_name)
        for key in self._user_conns.pop(uid, set()):
            if shared := self._conns.get(key):
                shared.users.discard(uid)
        self._user_sigs.pop(uid, None)

    async def _close_conn(self, key: str) -> None:
        shared = self._conns.pop(key, None)
        if shared is not None:
            try:
                await shared.client.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("mcp close %s failed: %s", key, e)

    def status(self) -> dict[str, Any]:
        from app.config import settings as _settings

        return {
            "active_users": len(self._tracker.active_users()),
            "shared_connections": len(self._conns),
            "user_loaded": len(self._user_sigs),
            "max_users_per_instance": _settings.mcp_max_users_per_instance,
            "max_connections_per_instance": _settings.mcp_max_connections_per_instance,
            "owner_lease_enabled": self._lease is not None,
            "owned_users": len(self._owned_users),
        }


def _make_broker_handler(uid: str, tool_name: str):
    """构造"经 MCP broker 调用"的 handler（broker 执行前会向服务端要授权）。"""

    async def handler(**kwargs: Any) -> dict[str, Any]:
        from app.plugins import broker_proxy
        from app.tools.context import get_session_id, get_tool_context

        try:
            return await broker_proxy.call_tool(
                uid,
                tool_name,
                kwargs,
                tenant_id=str(get_tool_context("tenant_id", "") or ""),
                session_id=get_session_id() or "",
                tool_call_id=str(get_tool_context("tool_call_id", "") or ""),
                tools_mode=str(get_tool_context("tools_mode", "auto") or "auto"),
            )
        except Exception as e:  # noqa: BLE001 - broker 不可用要如实回传给模型
            logger.warning("mcp broker call %s failed: %s", tool_name, e)
            return {"error": f"mcp broker call failed: {e}"}

    return handler


async def _register_broker_tools(uid: str, server_name: str, user_tools: set[str]) -> None:
    """把危险 server 的工具注册为"经 MCP broker 调用"的 handler。

    broker 不可用时只记日志、**不影响其余 server** —— 这些工具会不可用（fail-closed），
    但不会把整个用户的 MCP 一起拖垮。
    """
    from app.plugins import broker_proxy

    try:
        tools = await broker_proxy.list_tools(uid, server_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("mcp broker discovery failed for %s/%s: %s", uid, server_name, e)
        return
    registered = 0
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        registry.register(
            name=name,
            description=str(tool.get("description") or ""),
            parameters=tool.get("input_schema") or {},
            handler=_make_broker_handler(uid, name),
            owner=uid,
            source=SOURCE_MCP,
        )
        user_tools.add(name)
        registered += 1
    if registered:
        logger.info("mcp broker proxy tools for %s/%s: %d", uid, server_name, registered)


def _server_to_def(server: ServerConfig):
    from app.mcp.client import ServerDef

    return ServerDef(
        name=server.name,
        command=server.command,
        args=server.args,
        env=server.env,
        # 只读声明**必须透传**：漏传会让已声明的 read-only server 也被连接守卫挡住
        # （表现为"MCP 插件明明标了只读却连不上"）。
        read_only=getattr(server, "read_only", False),
    )


def _make_tool_handler(client: MCPClient, tool_name: str):
    """构造 MCP 工具 handler（绑定共享连接，带超时保护，R7 修复）。"""

    async def handler(**kwargs: Any) -> dict[str, Any]:
        # 30s 超时保护，防止 MCP 服务器挂死阻塞 Agent 循环
        try:
            return await asyncio.wait_for(
                client.call_tool(tool_name, kwargs), timeout=30.0
            )
        except TimeoutError:
            return {"error": f"MCP tool {tool_name} timed out after 30s"}
        except Exception as e:
            return {"error": f"MCP tool {tool_name} failed: {e}"}

    return handler
