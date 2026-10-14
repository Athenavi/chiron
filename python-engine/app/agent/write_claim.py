"""write_claim — 并行**写占用**的裁决（P4）。

对照实现：Reasonix `internal/runtime/writeclaim`（"子代理运行时谁可写哪里的写占用裁决"），
以及 `isolation`（worktree 级隔离）。Chiron 的形态与 Reasonix 不同 —— 工作区是
`SANDBOX_ROOT/{tenant}/{user}/workspace/` 的用户文件区，**不保证是 git 仓库**，
所以 worktree 隔离并不普适。这里落实的是同一目标里**不依赖 git** 的那一半：

> 当**多个 agent 并行**（主 agent + 若干子 agent，或同一批并发的子 agent）时，
> 同一个文件同一时刻**只能有一个写者**。

没有它的后果是真实且难查的：两个子 agent 各自 `write_file` 同一个文件 → 后写覆盖先写，
而两者都报告"成功"；父 agent 看到的最终内容取决于完成顺序（竞态），
且**没有任何一处会报错**。

设计取舍：

* **owner 是"谁在写"** —— 用 agent 的 session 标识（子 agent 为 ``sub:<parent>:<run>``，
  主 agent 为其会话 id）。同一 owner 反复写同一文件是**正常**的（它就是在迭代自己的文件），
  只有**跨 owner** 才算冲突。
* **TTL 是自我保护**：写者崩溃/被取消时不能留下永久锁。默认 5 分钟，写操作通常远短于此。
* **进程内为准**：单副本下并行子 agent 是主要场景，进程内裁决已覆盖。
  多副本共享卷时进程内看不到对方 —— 此时**应当**由外部（卷/编排）保证不同副本不写同一文件；
  本模块不假装能跨副本加锁（那需要 Redis 往返，代价与复杂度都不值当）。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

#: 占用默认存活时间（秒）。写者崩溃不留死锁。
DEFAULT_TTL_SECONDS = 300.0

#: 需要占用裁决的**文件写类**工具。与 `app/agent/tool_policy` 的 write/delete 级别对应。
FILE_WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "delete_file"})

#: path -> (owner, expires_at)
_claims: dict[str, tuple[str, float]] = {}


def _prune(now: float) -> None:
    for path in [p for p, (_, exp) in _claims.items() if exp <= now]:
        _claims.pop(path, None)


def claim(path: str, owner: str, *, ttl: float = DEFAULT_TTL_SECONDS) -> tuple[bool, str]:
    """尝试为 ``owner`` 占用 ``path``。

    返回 ``(True, "")`` 表示占用成功（或本来就是这个 owner）；
    ``(False, holder)`` 表示被**另一个** owner 占着，``holder`` 是它的标识。

    同一 owner 重复 claim 会**续期**，不冲突。
    """
    path = (path or "").strip()
    owner = (owner or "").strip()
    if not path or not owner:
        # 路径/owner 缺失：不做裁决（交给下游的参数校验去报错），绝不放行一个错误的裁决。
        return True, ""
    now = time.time()
    _prune(now)
    existing = _claims.get(path)
    if existing is not None and existing[0] != owner:
        return False, existing[0]
    _claims[path] = (owner, now + ttl)
    return True, ""


def release(path: str, owner: str) -> None:
    """释放 ``owner`` 对 ``path`` 的占用（只释放自己的）。"""
    path = (path or "").strip()
    existing = _claims.get(path)
    if existing is not None and existing[0] == (owner or "").strip():
        _claims.pop(path, None)


def release_all(owner: str) -> int:
    """释放某 owner 的**全部**占用（agent 结束时调用）。返回释放数量。"""
    owner = (owner or "").strip()
    if not owner:
        return 0
    mine = [p for p, (o, _) in _claims.items() if o == owner]
    for p in mine:
        _claims.pop(p, None)
    return len(mine)


def path_of(tool_name: str, params: dict | None) -> str:
    """从工具参数里取被写的文件路径（与各写工具的字段名对齐）。"""
    if tool_name not in FILE_WRITE_TOOLS or not isinstance(params, dict):
        return ""
    for key in ("path", "file_path", "filepath", "filename", "file", "target"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def reset() -> None:
    """清空全部占用（测试用）。"""
    _claims.clear()
