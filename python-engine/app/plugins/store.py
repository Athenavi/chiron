"""插件配置存储与活跃追踪（无状态友好的抽象层）。

设计目标（S 安全 / 可扩展）：
- PluginStore：读写 {PLUGIN_DATA_DIR}/{user_id}/plugins.json（与 Go 网关同一目录）。
  实例无进程内业务状态——多副本共享数据目录即可水平扩展；
  未来可加 Redis 实现（仅替换本类，接口不变）。
- ActiveTracker：标记"有活跃会话"的用户（驱动 MCP 25s 轮询范围）。
  进程内实现 + `RedisActiveTracker`（Redis ZSET 双写，多实例共享轮询范围）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── 插件配置存储 ─────────────────────────────────────────────────────────


def plugin_data_dir() -> Path:
    """插件数据根目录：环境 PLUGIN_DATA_DIR 优先，默认项目根 data/plugins
    （引擎 cwd 为 python-engine 时上溯一级）。"""
    env = os.environ.get("PLUGIN_DATA_DIR")
    if env:
        return Path(env).resolve()
    return Path(os.getcwd()).resolve().parent / "data" / "plugins"


@dataclass
class ServerConfig:
    """MCP 服务器配置（与 Go plugins.json 结构一致）。"""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    version: str = ""
    status: str = "active"
    #: 只读声明：引擎只直连 read_only 的 MCP server（见 app/mcp/client.py 的连接守卫）。
    #: 未声明 = False = **不直连** —— 宁可工具不可用，也不把高权限凭据留在 agent 可触达处。
    read_only: bool = False


class PluginStore:
    """按用户读写插件配置（文件实现；接口供未来 Redis 实现替换）。"""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._root = Path(data_dir) if data_dir else plugin_data_dir()

    def _path(self, user_id: str) -> Path:
        return self._root / user_id / "plugins.json"

    def save(self, user_id: str, servers: list[ServerConfig]) -> None:
        """写入用户插件配置（生产由 Go 网关写入；本方法供测试/对称）。"""
        p = self._path(user_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"mcp_servers": [s.__dict__ for s in servers]}
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self, user_id: str) -> list[ServerConfig]:
        """读取用户插件配置；文件不存在/损坏时返回空列表。"""
        p = self._path(user_id)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        servers = []
        for s in data.get("mcp_servers", []) or []:
            try:
                servers.append(
                    ServerConfig(
                        name=s["name"],
                        command=s["command"],
                        args=s.get("args", []) or [],
                        env=s.get("env", {}) or {},
                        description=s.get("description", ""),
                        version=s.get("version", ""),
                        status=s.get("status", "active"),
                        read_only=bool(s.get("read_only", False)),
                    )
                )
            except (KeyError, TypeError):
                continue
        return servers

    def active_servers(self, user_id: str) -> list[ServerConfig]:
        """仅返回 status=active 的服务器配置。"""
        return [s for s in self.load(user_id) if s.status == "active"]

    def signature(self, user_id: str) -> str:
        """配置指纹（用于变更检测：内容 hash）。"""
        servers = sorted(self.load(user_id), key=lambda s: s.name)
        return json.dumps(
            [s.__dict__ for s in servers], ensure_ascii=False, sort_keys=True
        )


# ── 活跃会话追踪 ─────────────────────────────────────────────────────────


class ActiveTracker:
    """标记有活跃会话的用户（驱动 MCP 轮询范围）。

    进程内实现：touch 记录 last_active，is_active 按时间窗口判断。多实例部署请用
    下面的 `RedisActiveTracker`（同一接口，额外把标记同步到 Redis）。
    """

    DEFAULT_WINDOW = 30 * 60  # 30 分钟无请求视为空闲

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def touch(self, user_id: str) -> None:
        if not user_id:
            return
        with self._lock:
            self._last[user_id] = time.time()

    def _window(self, window: int | None) -> int:
        return self.DEFAULT_WINDOW if window is None else window

    def is_active(self, user_id: str, window: int | None = None) -> bool:
        if not user_id:
            return False
        with self._lock:
            ts = self._last.get(user_id, 0)
        return (time.time() - ts) <= self._window(window)

    def active_users(self, window: int | None = None) -> list[str]:
        cutoff = time.time() - self._window(window)
        with self._lock:
            return [uid for uid, ts in self._last.items() if ts > cutoff]

    def active_users_sorted(self, window: int | None = None, limit: int = 0) -> list[str]:
        """按最近活跃降序返回活跃用户；limit>0 时只取前 N 个。

        用于连接预算截断：预算不足时优先服务最近有交互的用户。
        """
        cutoff = time.time() - self._window(window)
        with self._lock:
            items = sorted(
                ((uid, ts) for uid, ts in self._last.items() if ts > cutoff),
                key=lambda kv: kv[1],
                reverse=True,
            )
        users = [uid for uid, _ in items]
        return users[:limit] if limit > 0 else users

    def prune(self, window: int | None = None) -> int:
        """清理过期条目，返回清理数。"""
        cutoff = time.time() - self._window(window)
        with self._lock:
            stale = [uid for uid, ts in self._last.items() if ts <= cutoff]
            for uid in stale:
                del self._last[uid]
        return len(stale)

    #: 是否跨实例共享（诊断用；Redis 实现覆盖为 True）
    shared = False

    async def pull(self) -> None:
        """与远端存储同步标记。进程内实现无事可做，接口留给 Redis 实现覆盖，
        这样调用方（MCPClientPool.reconcile）不必分辨实现类型。"""
        return None


def active_users_key() -> str:
    """共享活跃标记的 ZSET 键（成员=user_id，分值=最近活跃时间戳）。"""
    from app.redis_keys import rkey

    return rkey("mcp:active_users")


class RedisActiveTracker(ActiveTracker):
    """进程内标记 + Redis ZSET 双写：让各实例看到同一份"谁还活跃"。

    为什么保留本地字典而不是纯 Redis：读侧接口是**同步**的（`status()` 与
    `active_users_sorted()` 的调用点都不在 await 上下文里），没法 await Redis。
    所以本地继续作读缓存，Redis 只负责把别的实例打过的标记合并过来 —— 可见延迟
    ≤ 一个轮询周期（25s），而活跃标记只决定"这一轮去轮询谁"，晚一拍既不丢连接、
    也不会多占连接预算。

    Redis 出错时**降级为进程内行为**并告警：活跃范围是效率问题，不是安全边界，
    不值得为此让整条 MCP 轮询链失败。
    """

    shared = True

    def __init__(self, redis: Any, *, window: int | None = None) -> None:
        super().__init__()
        self._redis = redis
        self._window_override = window
        self._pending: dict[str, float] = {}
        self._flush_tasks: set[Any] = set()
        self._key = active_users_key()

    def touch(self, user_id: str) -> None:
        if not user_id:
            return
        ts = time.time()
        with self._lock:
            self._last[user_id] = ts
            self._pending[user_id] = ts
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        """有事件循环就立刻 ZADD；没有（同步启动路径）留到下一次 pull()。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # 留住强引用：asyncio 只持弱引用，任务在完成前可能被回收（表现为标记静默不写）
        task = loop.create_task(self.flush_pending())
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def flush_pending(self) -> None:
        with self._lock:
            if not self._pending:
                return
            items = dict(self._pending)
            self._pending.clear()
        try:
            await self._redis.zadd(self._key, items)
            # 整键 TTL 兜底：全集群长期空闲时不要让标记永久留着
            await self._redis.expire(self._key, self._window(None) + 60)
        except Exception as e:  # noqa: BLE001 — 降级为进程内，不影响轮询
            logger.warning("MCP 活跃标记写入失败（降级为进程内）: %s", e)
            with self._lock:
                for uid, ts in items.items():
                    if ts > self._pending.get(uid, 0):
                        self._pending[uid] = ts

    async def pull(self) -> None:
        """先把本地待发标记写出去，再把其它实例的标记合并进读缓存。"""
        await self.flush_pending()
        cutoff = time.time() - self._window(None)
        try:
            remote = await self._redis.zrangebyscore(
                self._key, min=cutoff, max="+inf", withscores=True
            )
            await self._redis.zremrangebyscore(self._key, min="-inf", max=cutoff)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP 活跃标记同步失败（沿用本实例视角）: %s", e)
            return
        with self._lock:
            for uid, ts in remote:
                ts_f = float(ts)
                if ts_f > self._last.get(str(uid), 0):
                    self._last[str(uid)] = ts_f

    def _window(self, window: int | None) -> int:
        if window is not None:
            return window
        if self._window_override is not None:
            return self._window_override
        return self.DEFAULT_WINDOW
