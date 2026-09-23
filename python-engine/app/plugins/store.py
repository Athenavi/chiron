"""插件配置存储与活跃追踪（无状态友好的抽象层）。

设计目标（S 安全 / 可扩展）：
- PluginStore：读写 {PLUGIN_DATA_DIR}/{user_id}/plugins.json（与 Go 网关同一目录）。
  实例无进程内业务状态——多副本共享数据目录即可水平扩展；
  未来可加 Redis 实现（仅替换本类，接口不变）。
- ActiveTracker：标记"有活跃会话"的用户（驱动 MCP 25s 轮询范围）。
  当前为进程内实现；多实例场景可替换为 Redis（SETEX + TTL）实现。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

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

    进程内实现：touch 记录 last_active，is_active 按时间窗口判断。
    多实例部署时可替换为 Redis 实现（同一接口），让各实例共享活跃标记。
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
