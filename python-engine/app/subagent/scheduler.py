"""R2：子 Agent 写路径仲裁（vendor/规划.md §3.5）。

## 为什么需要

`background=True` 的子 Agent 与父 turn **真并发**（同进程的 asyncio 任务），而它们**共享同一个
工作区**（`app/tools/sandbox.py` 的 `workspace_dir()`）—— 两个任务写同一个文件会互相覆盖，
而且**没有任何报错**：后写者赢，先写者的产出静默消失。此前只有"是否只读"这一个粗粒度开关
（`allow_write` / `ProfileSpec.read_only`），它拦得住"只读的子 Agent 去写"，**拦不住**
"两个可写的子 Agent 写同一文件"。

## 与 Reasonix 的关系（`internal/agent/scheduler.go`）

| 能力 | Reasonix | 本模块 |
|---|---|---|
| 并发上限（total / writers **双维**） | ✅ | ✅ |
| 同路径串行、不同路径并行 | ✅ | ✅ |
| 目录声明：目录↔目录可并行，目录 vs 它里面的文件**冲突** | ✅ | ✅ |
| 父写预留（父工具调用期间挡住重叠的子 Agent） | ✅ | ✅ |
| 嵌套**即时失败**（子等父持有的槽 ⇒ 必然死锁） | ✅ | ✅ |
| `Realize`（目录声明 → 实际写到哪些文件的细化） | ✅ | ❌ **不做** |
| `MarkOpaque`（无法预知 ⇒ 独占整工作区） | ✅ | ❌ **不做** |

后两项要在**事前声明**之外再加一层"运行期把声明收窄到具体文件"的协议；Chiron 目前只有**事后**
记录（`subagent_runs.write_paths`），学它得先有声明协议本身 —— 按 §3.5 的「刻意简化」先不做。
已知边界（**不是疏漏**）：目录声明在本模块里一直按"目录级"对待，不细化到文件，因此两个都声明
`docs/` 的子 Agent 会排队而不是并行写 `docs/a.md` 与 `docs/b.md`。

## 默认关（§1.3 的"默认值必须是'什么都不做'"）

`settings.subagent_write_arbitration` 默认 `False` ⇒ :func:`get_scheduler` 返回 `None`，
`SubAgentRunner` 既不做 acquire 也不做 release。**零行为变化**。

## 冲突一律"显式失败"，不静默降级

声明非法（空条目 / glob / 逃出工作区）**抛 `ValueError`**；嵌套无容量**抛 `WriteConflictError`**。
两者都被 `SubAgentRunner` 记成这次委派的失败原因（走既有的 failed 收尾路径），而不是"悄悄不仲裁、
继续写" —— 后者会退化成"以为有保护其实没有"。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from weakref import WeakValueDictionary

logger = logging.getLogger(__name__)

#: 会话级并发上限（对位 Reasonix 的 `DefaultMaxSubagentConcurrency`）。
DEFAULT_MAX_CONCURRENCY = 6
#: 可写 run 的上限（对位 `DefaultMaxParallelWriters`）。
DEFAULT_MAX_WRITERS = 3
#: 两个旋钮共同的硬上限。
MAX_CONCURRENCY_LIMIT = 32

KIND_FILE = "file"
KIND_DIR = "dir"

#: 冲突文案一律**不含具体路径**：它会作为 `error` 回给父模型与前端，路径属于内容面。
_CONFLICT_RUNNING = (
    "write path is claimed by a running subagent; wait for it to finish "
    "or declare non-overlapping write_paths"
)
_CONFLICT_PARENT = "write path is claimed by a parent write in progress"


class WriteConflictError(RuntimeError):
    """写路径/并发槽冲突且**无法排队**（嵌套自等即死锁，父写预留不能等后台任务）。"""


def _noop() -> None:
    """release 的零值（`None` claim 时返回它，调用方无需判空）。"""


# ── 路径集合 ──


@dataclass(frozen=True)
class WritePathSet:
    """一次委派**声明**要写的路径集合（已规范化：绝对、去重、限定在工作区内）。

    ``kinds`` 与 ``paths`` **平行**：目录声明允许"目录↔目录"并行，文件声明不允许。
    """

    paths: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    #: 未声明路径的可写 run 按"整工作区"对待（保守方向）。
    whole_workspace: bool = False
    workspace_root: str = ""

    @property
    def empty(self) -> bool:
        """只读 / 无声明。"""
        return not self.whole_workspace and not self.paths

    def kind_at(self, index: int) -> str:
        if 0 <= index < len(self.kinds):
            return self.kinds[index]
        return KIND_FILE

    def describe(self) -> str:
        """**本地诊断用**的一行形状描述。不要放进给模型/前端的文本（含路径）。"""
        if self.whole_workspace:
            return f"whole-workspace({self.workspace_root or '-'})"
        return ", ".join(self.paths) if self.paths else "(none)"


def normalize_concurrency_limits(total: int, writers: int) -> tuple[int, int]:
    """把 total/writers 夹到 1–``MAX_CONCURRENCY_LIMIT``，并保证 writers ≤ total。

    0 / 负数 ⇒ 取默认（6 / 3）。这样"配置里没写"不会变成"上限为 0 = 谁都起不来"。
    """
    if total <= 0:
        total = DEFAULT_MAX_CONCURRENCY
    if writers <= 0:
        writers = DEFAULT_MAX_WRITERS
    total = min(total, MAX_CONCURRENCY_LIMIT)
    writers = min(writers, MAX_CONCURRENCY_LIMIT)
    return total, min(writers, total)


def normalize_write_paths(workspace_root: str, raw: Sequence[str] | None) -> WritePathSet:
    """把声明的 ``write_paths`` 规范化；**非法声明抛 `ValueError`**。

    拒绝：空条目 · glob（`* ? [` —— 通配会让"声明"失去意义，也就无从仲裁）· 逃出工作区的路径
    （含 `../` 与 symlink 逃逸）。空/缺省声明返回空集合（= 只读，不参与写仲裁）。
    """
    root = _normalize_root(workspace_root)
    entries = [str(e) for e in (raw or [])]
    if not entries:
        return WritePathSet()
    if not root:
        raise ValueError("write_paths requires a workspace root")

    paths: list[str] = []
    kinds: list[str] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        text = entry.strip()
        if not text:
            raise ValueError(f"write_paths[{i}]: path is required")
        if any(ch in text for ch in "*?["):
            raise ValueError(f"write_paths[{i}]: globs are not allowed ({text!r})")
        trailing_sep = text.endswith(("/", "\\"))
        body = text.rstrip("/\\") or text
        candidate = Path(body)
        if not candidate.is_absolute():
            candidate = Path(root) / body
        resolved = _resolve_for_claim(candidate)
        if not _within(root, resolved):
            raise ValueError(f"write_paths[{i}]: {text!r} is outside the workspace")
        key = _fold(resolved)
        if key in seen:
            continue
        seen.add(key)
        paths.append(resolved)
        kinds.append(KIND_DIR if (trailing_sep or _is_dir(resolved)) else KIND_FILE)
    return WritePathSet(paths=tuple(paths), kinds=tuple(kinds), workspace_root=root)


def whole_workspace_claim(workspace_root: str) -> WritePathSet:
    """整工作区声明（可写但没声明路径的 run 用）。"""
    return WritePathSet(whole_workspace=True, workspace_root=_normalize_root(workspace_root))


def schedule_overlaps(a: WritePathSet, b: WritePathSet) -> bool:
    """两个**声明**能否同时开始？对位 Reasonix 的 `ScheduleOverlaps`。

    比"能力重叠"更宽松的一处：目录↔目录声明**可以并行**（各自往不同文件写）；但目录与它里面的
    具体文件**冲突**（目录声明者可能覆盖那个文件）。
    """
    if a.empty or b.empty:
        return False
    if a.whole_workspace or b.whole_workspace:
        return claims_overlap(a, b)
    for i, pa in enumerate(a.paths):
        for j, pb in enumerate(b.paths):
            if not (_within(pa, pb) or _within(pb, pa)):
                continue
            if a.kind_at(i) == KIND_DIR and b.kind_at(j) == KIND_DIR:
                continue
            return True
    return False


def claims_overlap(a: WritePathSet, b: WritePathSet) -> bool:
    """**能力**口径的重叠（比 :func:`schedule_overlaps` 严格：不做目录↔目录的例外）。"""
    if a.empty or b.empty:
        return False
    if a.whole_workspace or b.whole_workspace:
        # 契约未知的整工作区声明与**任何**声明冲突。
        if not a.workspace_root or not b.workspace_root:
            return True
        return _within(a.workspace_root, b.workspace_root) or _within(b.workspace_root, a.workspace_root)
    for pa in a.paths:
        for pb in b.paths:
            if _within(pa, pb) or _within(pb, pa):
                return True
    return False


# ── 请求与槽位 ──


@dataclass(frozen=True)
class AcquireRequest:
    """一次并发槽申请。"""

    #: 可写 run 才占 writer 槽；只读 run 只占总槽。
    writer: bool = False
    #: 声明要写的路径。``writer=True`` 且为空 ⇒ 由仲裁器**升级**为整工作区声明。
    write_paths: WritePathSet = field(default_factory=WritePathSet)
    #: 嵌套申请（子 Agent 里的子 Agent）：无容量时**立即失败**而不是排队 ——
    #: 父正持有它要等的槽，排队就是自等死锁。
    nested: bool = False
    #: 调用方当前持有的父写预留 id（父写预留内部派发的委派不该被它自己挡住）。
    parent_claim_id: int = 0


@dataclass
class _LiveClaim:
    claim_id: int
    writer: bool
    paths: WritePathSet
    parent_claim_id: int = 0


@dataclass
class _ParentClaim:
    claim_id: int
    paths: WritePathSet


class _Waiter:
    __slots__ = ("req", "event", "claim_id")

    def __init__(self, req: AcquireRequest) -> None:
        self.req = req
        self.event = asyncio.Event()
        self.claim_id = 0


# ── 仲裁器 ──


class WriteScheduler:
    """会话级的"并发上限 + 写路径"仲裁器。

    临界区全是**纯同步**代码（无 `await`），因此内部用 `threading.Lock` 而不是 `asyncio.Lock`：
    这样 `release()` 可以是同步回调（调用方在 `finally` 里调它，不必再 await），
    锁也永远不会被"持锁 await"拖住事件循环。排队等待用 `asyncio.Event`。
    """

    def __init__(
        self,
        *,
        workspace_root: str = "",
        max_total: int = 0,
        max_writers: int = 0,
    ) -> None:
        self._workspace_root = _normalize_root(workspace_root)
        self._max_total, self._max_writers = normalize_concurrency_limits(max_total, max_writers)
        self._mu = threading.Lock()
        self._active_total = 0
        self._active_writers = 0
        self._active: list[_LiveClaim] = []
        self._parent_claims: list[_ParentClaim] = []
        self._waiters: deque[_Waiter] = deque()
        self._next_id = 0

    # ── 只读诊断 ──

    @property
    def limits(self) -> tuple[int, int]:
        return self._max_total, self._max_writers

    @property
    def workspace_root(self) -> str:
        return self._workspace_root

    def active_counts(self) -> tuple[int, int]:
        with self._mu:
            return self._active_total, self._active_writers

    def active_writer_claims(self) -> list[WritePathSet]:
        with self._mu:
            return [c.paths for c in self._active if c.writer]

    def pending_waiter_count(self) -> int:
        with self._mu:
            return len(self._waiters)

    def try_claim_write_paths(self, paths: WritePathSet) -> str | None:
        """只检查冲突、不占槽（诊断用）。返回冲突原因或 ``None``。"""
        if paths.empty:
            return None
        with self._mu:
            return self._conflict_with_others_locked(0, paths)

    # ── 申请 / 释放 ──

    async def acquire(self, req: AcquireRequest) -> tuple[Callable[[], None], int]:
        """取一个并发槽（可写时同时取写声明）。返回 ``(release, claim_id)``。

        `release` **幂等**且可在 `acquire` 失败后安全调用（返回的是 no-op）。非嵌套申请会
        **FIFO 排队**直到有容量；嵌套申请无容量时立刻抛 :class:`WriteConflictError`。
        """
        effective = self._effective(req)
        with self._mu:
            ok, reason = self._can_start_incoming_locked(effective)
            if ok:
                claim_id = self._activate_locked(effective)
                return self._make_release(claim_id), claim_id
            if effective.nested:
                raise WriteConflictError(
                    f"{reason}; nested subagents fail fast instead of queueing "
                    "(the parent holds the slot they would wait for)"
                )
            waiter = _Waiter(effective)
            self._waiters.append(waiter)

        try:
            await waiter.event.wait()
        except asyncio.CancelledError:
            # 取消可能发生在"已被唤醒并激活"之后 —— 两种都要收干净，否则槽位永久泄漏。
            with self._mu:
                if waiter.claim_id:
                    self._deactivate_locked(waiter.claim_id)
                else:
                    self._remove_waiter_locked(waiter)
                self._pump_waiters_locked()
            raise
        return self._make_release(waiter.claim_id), waiter.claim_id

    def reserve_parent_write(self, paths: WritePathSet) -> tuple[Callable[[], None], int]:
        """父（depth 0 工具调用）在写文件期间占住这些路径，挡住重叠的子 Agent。

        **不排队、不占子 Agent 槽**：父此刻正卡在工具调用里，让它去等后台任务就是死锁
        （后台任务可能就在等父这一轮的返回值）。冲突即抛 :class:`WriteConflictError`。
        """
        if paths.empty:
            return _noop, 0
        with self._mu:
            conflict = self._conflict_with_others_locked(0, paths)
            if conflict:
                raise WriteConflictError(conflict)
            self._next_id += 1
            claim_id = self._next_id
            self._parent_claims.append(_ParentClaim(claim_id=claim_id, paths=paths))
        return self._make_parent_release(claim_id), claim_id

    # ── 内部：请求规范化 ──

    def _effective(self, req: AcquireRequest) -> AcquireRequest:
        if req.writer and req.write_paths.empty:
            # 可写但**没声明写了哪儿** ⇒ 按整工作区独占。反过来（当作"什么都不写"）会让两个
            # writer 静默并行地互相覆盖 —— 那正是本模块要拦的东西。
            return replace(req, write_paths=whole_workspace_claim(self._workspace_root))
        return req

    # ── 内部：容量与冲突判定（调用方持锁）──

    def _can_start_locked(self, req: AcquireRequest) -> tuple[bool, str]:
        if self._active_total >= self._max_total:
            return False, f"subagent concurrency limit reached ({self._active_total}/{self._max_total})"
        if not req.writer:
            return True, ""
        if self._active_writers >= self._max_writers:
            return False, f"writer concurrency limit reached ({self._active_writers}/{self._max_writers})"
        if req.write_paths.whole_workspace:
            for live in self._active:
                if live.writer:
                    return False, "whole-workspace claim conflicts with a running writer"
        for live in self._active:
            if schedule_overlaps(req.write_paths, live.paths):
                return False, _CONFLICT_RUNNING
        for parent in self._parent_claims:
            if parent.claim_id != req.parent_claim_id and schedule_overlaps(req.write_paths, parent.paths):
                return False, _CONFLICT_PARENT
        return True, ""

    def _can_start_incoming_locked(self, req: AcquireRequest) -> tuple[bool, str]:
        """新申请**不得越过**已在排队的整工作区 writer（否则源源不断的小 writer 会让它饿死）。

        例外与 Reasonix 一致：持有父写预留的委派不受这条限制 —— 父写预留是"父在原地等"的
        路径，让它排在后面会与父形成死锁。
        """
        if req.writer and not self._holds_parent_claim_locked(req.parent_claim_id):
            for waiter in self._waiters:
                if waiter.req.writer and waiter.req.write_paths.whole_workspace:
                    return False, "a queued whole-workspace writer has priority"
        return self._can_start_locked(req)

    def _conflict_with_others_locked(self, skip_id: int, paths: WritePathSet) -> str | None:
        if paths.empty:
            return None
        caller_parent = 0
        if skip_id:
            for live in self._active:
                if live.claim_id == skip_id:
                    caller_parent = live.parent_claim_id
                    break
        for live in self._active:
            if live.claim_id == skip_id:
                continue
            if schedule_overlaps(live.paths, paths):
                return _CONFLICT_RUNNING
        for parent in self._parent_claims:
            if parent.claim_id != caller_parent and schedule_overlaps(parent.paths, paths):
                return _CONFLICT_PARENT
        return None

    def _holds_parent_claim_locked(self, claim_id: int) -> bool:
        if not claim_id:
            return False
        return any(parent.claim_id == claim_id for parent in self._parent_claims)

    # ── 内部：激活 / 释放 / 唤醒 ──

    def _activate_locked(self, req: AcquireRequest) -> int:
        self._active_total += 1
        self._next_id += 1
        claim_id = self._next_id
        if req.writer:
            self._active_writers += 1
        self._active.append(_LiveClaim(
            claim_id=claim_id,
            writer=req.writer,
            paths=req.write_paths,
            parent_claim_id=req.parent_claim_id,
        ))
        return claim_id

    def _deactivate_locked(self, claim_id: int) -> None:
        for i, live in enumerate(self._active):
            if live.claim_id != claim_id:
                continue
            del self._active[i]
            self._active_total = max(0, self._active_total - 1)
            if live.writer:
                self._active_writers = max(0, self._active_writers - 1)
            return

    def _make_release(self, claim_id: int) -> Callable[[], None]:
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            with self._mu:
                self._deactivate_locked(claim_id)
                self._pump_waiters_locked()

        return release

    def _make_parent_release(self, claim_id: int) -> Callable[[], None]:
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            with self._mu:
                self._parent_claims = [c for c in self._parent_claims if c.claim_id != claim_id]
                self._pump_waiters_locked()

        return release

    def _remove_waiter_locked(self, target: _Waiter) -> None:
        self._waiters = deque(w for w in self._waiters if w is not target)

    def _pump_waiters_locked(self) -> None:
        if not self._waiters:
            return
        remaining: deque[_Waiter] = deque()
        whole_writer_pending = False
        while self._waiters:
            waiter = self._waiters.popleft()
            req = waiter.req
            if (
                whole_writer_pending
                and req.writer
                and not self._holds_parent_claim_locked(req.parent_claim_id)
            ):
                remaining.append(waiter)
                continue
            ok, _reason = self._can_start_locked(req)
            if ok:
                waiter.claim_id = self._activate_locked(req)
                waiter.event.set()
                continue
            remaining.append(waiter)
            if req.writer and req.write_paths.whole_workspace:
                whole_writer_pending = True
        self._waiters = remaining


# ── 会话级注册表 ──


#: session_id → 仲裁器。用**弱引用**值：run 结束后没人再持有它，条目自动消失，
#: 不会随"跑过的会话数"无界增长（而 run 期间所有并发委派共享同一个实例）。
_schedulers: WeakValueDictionary[str, WriteScheduler] = WeakValueDictionary()


def get_scheduler(session_id: str, *, workspace_root: str = "") -> WriteScheduler | None:
    """取（或建）该会话的仲裁器；**未开启仲裁时返回 `None`**（调用方按"无仲裁"处理）。"""
    from app.config import settings

    if not bool(getattr(settings, "subagent_write_arbitration", False)):
        return None
    if not session_id:
        return None
    sched = _schedulers.get(session_id)
    if sched is None:
        sched = WriteScheduler(
            workspace_root=workspace_root,
            max_total=int(getattr(settings, "subagent_max_concurrency", 0) or 0),
            max_writers=int(getattr(settings, "subagent_max_writers", 0) or 0),
        )
        _schedulers[session_id] = sched
    return sched


def reset_schedulers() -> None:
    """测试用：清空注册表（回到"没有仲裁器"的干净态）。"""
    _schedulers.clear()


# ── 路径 helpers ──


def _normalize_root(root: str) -> str:
    text = (root or "").strip()
    if not text:
        return ""
    return _resolve_for_claim(Path(text))


def _resolve_for_claim(path: Path) -> str:
    """尽量解析 symlink（含已存在的最深祖先），与工具的写路径口径一致。"""
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def _is_dir(path: str) -> bool:
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def _fold(path: str) -> str:
    """大小写折叠键（Windows/macOS 上大小写不同即同一条路径）。"""
    return os.path.normcase(os.path.normpath(path))


def _within(root: str, path: str) -> bool:
    """``path`` 是否等于 ``root`` 或在其子树内（大小写按平台折叠）。"""
    if not root or not path:
        return False
    r, p = _fold(root), _fold(path)
    if r == p:
        return True
    return p.startswith(r.rstrip(os.sep) + os.sep)


__all__ = [
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MAX_WRITERS",
    "MAX_CONCURRENCY_LIMIT",
    "AcquireRequest",
    "WriteConflictError",
    "WritePathSet",
    "WriteScheduler",
    "claims_overlap",
    "get_scheduler",
    "normalize_concurrency_limits",
    "normalize_write_paths",
    "reset_schedulers",
    "schedule_overlaps",
    "whole_workspace_claim",
]
