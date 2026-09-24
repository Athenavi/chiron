"""fs_guard — read-before-write 观测策略（对应 deepseek fs-observation-policy）

read_file 记录文件版本签名（mtime+size+首块 hash）；write/edit 前若发现
文件"被读过但已变化"则拒绝（FS_NOT_OBSERVED 语义），防止模型基于过期
视图编辑。从未被 read 过的文件不受限制（兼容旧行为）。

S 安全修复：字典 key 包含 tenant_id + user_id 前缀，防止跨租户写入冲突。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_observed: dict[str, tuple[int, int, str]] = {}


def _tenant_prefix() -> str:
    """返回当前租户/用户隔离前缀，防止跨租户文件签名冲突。"""
    from app.tools.context import get_tenant_id, get_user_id
    tenant = get_tenant_id() or "default"
    user = get_user_id() or "anonymous"
    return f"{tenant}:{user}:"


def _signature(path: Path) -> tuple[int, int, str]:
    st = path.stat()
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError:
        head = b""
    return (st.st_mtime_ns, st.st_size, hashlib.md5(head).hexdigest())


def observe(path: Path) -> None:
    """记录文件的当前版本（read_file 成功后调用）。"""
    try:
        key = _tenant_prefix() + str(path.resolve())
        _observed[key] = _signature(path)
    except OSError:
        pass


def check_before_write(path: Path) -> str | None:
    """返回 None 表示可写；否则返回拒绝原因。

    仅拦截"被读过且版本已变"的文件；未读过的文件直接放行。
    """
    key = _tenant_prefix() + str(path.resolve())
    recorded = _observed.get(key)
    if recorded is None:
        return None
    if not path.exists():
        return "file was read earlier but no longer exists — re-read before writing"
    if _signature(path) != recorded:
        return "file changed since it was last read — call read_file first to refresh"
    return None
