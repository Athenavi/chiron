"""宿主证据（host receipts，方案见 `vendor/规划.md` §4.5 的 R1）。

## 为什么需要它

父模型看到的 `summary` 是子 Agent 的**自述**。它说"已修复并测试通过"——宿主并不核实。对"委派"这个
动作而言，**可信度就是它的核心价值**：不能核实的委派，等于让父模型去信任一段它无法检查的文字。

对齐的参照是 Reasonix 的 `subagent_report.go`：**宿主自己**核验子 Agent 的声明，产出"收据"附在结果
后面；并且**只读子 Agent 保持沉默**（不产收据 = 沉默即正确）。

## 本实现的两个证据源（都是宿主观测，不是子 Agent 自述）

1. **工作区差分**：子 Agent 执行**前后**各拍一次工作区快照，对比得出"实际被创建/修改/删除的文件"。
   这是**宿主的眼睛** —— 与子 Agent 说了什么无关；
2. **执行观测**：由 runner 从**它自己看到的事件流**累计（成功执行数 / 失败数 / 被拦数）。刻意**不**去
   读 `logs/exec_audit.jsonl`：那是全局追加的单文件，读它要先扫全量再按身份过滤，而 runner 本来就
   在遍历这次 run 的事件 —— 让它自己计数更直接、也更准。

## 违规判定（对位 Reasonix 的 `claimViolations`，取最小形态）

**只读子 Agent 却有文件变化 ⇒ 违规。** 这条不需要"子 Agent 事先声明要写什么"就能判，而且**真的能抓到
问题**（只读承诺被打破）。Reasonix 那套"写路径声明 + 仲裁"（`Scheduler` 的 `WritePathSet`）在我们的
R2 里做，本模块不预设它。

## 诚实的局限（必须写在收据里，不能藏）

`sandbox_root()` 是**进程级**的，父子共享同一个工作区，且可能**有并发 run 同时写**。所以"工作区差分"
只能说明"**这个工作区内**发生了这些变化"，**无法**归因到某一个子 Agent。收据如实这么写 —— 把"看不清"
说成"看清了"比不给收据更糟。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: 快照最多记多少条。超出即截断并标注 —— 宁可说"没看全"，也不要为了看全而把引擎拖慢。
DEFAULT_MAX_SNAPSHOT_ENTRIES = 2000

#: 收据里最多列多少个路径。超出折叠成计数。
DEFAULT_MAX_LISTED_PATHS = 12

#: 收据块的分隔标记。用显式的行标记（而不是 Markdown 注释）便于人眼与 grep 都能认出。
RECEIPTS_MARKER = "[宿主观测收据]"

#: 这些目录不参与快照：与子 Agent 的产出无关，且体量可能很大。
_SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                        "node_modules", ".venv", "venv"})


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """工作区的一瞥：相对路径 →（大小, mtime_ns）。"""

    entries: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: 是否因条目上限而**没看全**。为真时收据必须标注（否则"没有变化"会变成假结论）。
    truncated: bool = False

    def __bool__(self) -> bool:
        return bool(self.entries) or self.truncated


@dataclass(frozen=True)
class WorkspaceDiff:
    created: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.created or self.modified or self.removed)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.modified) + len(self.removed)


@dataclass(frozen=True)
class ExecObservation:
    """由 runner 从事件流累计的执行观测（宿主观测，非子 Agent 自述）。"""

    executions: int = 0
    failures: int = 0
    blocked: int = 0

    @property
    def is_empty(self) -> bool:
        return self.executions == 0 and self.failures == 0 and self.blocked == 0


@dataclass(frozen=True)
class HostReceipts:
    """一次委派的宿主证据。`violations` 非空表示**只读承诺被打破**。"""

    diff: WorkspaceDiff = field(default_factory=WorkspaceDiff)
    execs: ExecObservation = field(default_factory=ExecObservation)
    read_only: bool = False
    #: 快照是否没看全（见 `WorkspaceSnapshot.truncated`）。为真时收据必须如实标注。
    incomplete: bool = False

    @property
    def violations(self) -> tuple[str, ...]:
        """只读子 Agent 却改了工作区 ⇒ 违规（每条是"类别: 路径"）。"""
        if not self.read_only:
            return ()
        out = [f"created: {p}" for p in self.diff.created]
        out += [f"modified: {p}" for p in self.diff.modified]
        out += [f"removed: {p}" for p in self.diff.removed]
        return tuple(out)

    @property
    def should_report(self) -> bool:
        """**沉默规则**（对位 Reasonix："read-only children stay silent"）。

        只读、且没有任何可报告的事实、且快照看全了 ⇒ 不产收据。沉默是**正确**的表现，不是漏报。
        """
        if self.violations:
            return True
        if not self.diff.is_empty or not self.execs.is_empty:
            return True
        return self.incomplete  # 没看全就必须说，否则"什么都没有"会被误读为"确认没变"


# ── 快照 ────────────────────────────────────────────────────────────────


def capture_workspace(
    root: Path | str, *, max_entries: int = DEFAULT_MAX_SNAPSHOT_ENTRIES
) -> WorkspaceSnapshot:
    """拍一次工作区快照。**有界**：达到上限即停并标记 `truncated`。

    用 `os.walk` 而不是 `rglob`：前者能**边走边停**（`rglob` 会先构造整棵树的列表），
    在"工作区很大"时这个差别就是"能不能承受"。
    """
    base = Path(root)
    entries: dict[str, tuple[int, int]] = {}
    truncated = False
    if not base.is_dir():
        return WorkspaceSnapshot(entries={}, truncated=False)

    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        # 就地裁剪：跳过噪音目录，且不跟随符号链接（避免走出工作区）
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue  # 竞争删除 / 权限：跳过比中断更有用
            try:
                rel = full.relative_to(base).as_posix()
            except ValueError:
                continue
            entries[rel] = (st.st_size, st.st_mtime_ns)
            if len(entries) >= max_entries:
                truncated = True
                return WorkspaceSnapshot(entries=entries, truncated=truncated)
    return WorkspaceSnapshot(entries=entries, truncated=truncated)


def diff_workspace(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> WorkspaceDiff:
    """两个快照的差。**纯函数**（无 IO），因此可确定性测试。"""
    b, a = before.entries, after.entries
    created = tuple(sorted(set(a) - set(b)))
    removed = tuple(sorted(set(b) - set(a)))
    modified = tuple(sorted(p for p in set(a) & set(b) if a[p] != b[p]))
    return WorkspaceDiff(created=created, modified=modified, removed=removed)


# ── 组装与渲染 ──────────────────────────────────────────────────────────


def build_receipts(
    *,
    before: WorkspaceSnapshot | None,
    after: WorkspaceSnapshot | None,
    execs: ExecObservation,
    read_only: bool,
) -> HostReceipts:
    """组装收据。缺任一端快照时 diff 记为空并标注 `incomplete`（不假装"没有变化"）。"""
    if before is None or after is None:
        return HostReceipts(
            diff=WorkspaceDiff(), execs=execs, read_only=read_only, incomplete=True
        )
    return HostReceipts(
        diff=diff_workspace(before, after),
        execs=execs,
        read_only=read_only,
        incomplete=before.truncated or after.truncated,
    )


def _bounded(paths: tuple[str, ...], limit: int) -> str:
    shown = ", ".join(paths[:limit])
    if len(paths) > limit:
        shown += f" …（另有 {len(paths) - limit} 个）"
    return shown


def format_host_receipts(
    receipts: HostReceipts, *, max_listed: int = DEFAULT_MAX_LISTED_PATHS
) -> str:
    """渲染收据块。`should_report` 为假时返回空串（**沉默**）。"""
    if not receipts.should_report:
        return ""

    lines: list[str] = [RECEIPTS_MARKER]
    d, e = receipts.diff, receipts.execs

    if receipts.violations:
        lines.append(
            f"⚠️ 违规：本次委派声明为**只读**，但工作区发生了变化（{len(receipts.violations)} 处）"
        )
        lines.append(f"  {_bounded(receipts.violations, max_listed)}")

    if d.created:
        lines.append(f"新建（{len(d.created)}）：{_bounded(d.created, max_listed)}")
    if d.modified:
        lines.append(f"修改（{len(d.modified)}）：{_bounded(d.modified, max_listed)}")
    if d.removed:
        lines.append(f"删除（{len(d.removed)}）：{_bounded(d.removed, max_listed)}")

    if not e.is_empty:
        detail = f"执行 {e.executions} 次"
        if e.failures:
            detail += f"，失败 {e.failures} 次"  # 失败也记：不掩盖
        if e.blocked:
            detail += f"，被拦 {e.blocked} 次"
        lines.append(detail)

    if receipts.incomplete:
        lines.append("注：工作区快照**未看全**（条目数达上限），上面的清单可能不全。")
    lines.append(
        "注：以上是**宿主观测**（工作区差分与事件流），不是子 Agent 的自述；"
        "工作区为进程级共享，并发委派的改动在此无法区分来源。"
    )
    return "\n".join(lines)


def split_host_receipts(answer: str) -> tuple[str, str]:
    """把回答拆成（散文, 收据）。没有收据时第二项为空串。

    对位 Reasonix 的 `splitHostReceipts`：让调用方能把**人读的部分**与**宿主的证词**分开处理
    （例如只把散文回灌给父模型，或只把收据写进审计）。
    """
    marker = f"\n{RECEIPTS_MARKER}"
    idx = answer.find(marker)
    if idx < 0:
        # 也容忍收据就在开头（无前导换行）
        if answer.startswith(RECEIPTS_MARKER):
            return "", answer
        return answer, ""
    # 去掉拼接时引入的分隔空白：调用方要的是"散文本身"，不该带上我们加的空行
    return answer[:idx].rstrip(), answer[idx + 1 :]


def append_host_receipts(answer: str, receipts: HostReceipts) -> str:
    """把收据追加到回答/摘要末尾。`should_report` 为假时**原样返回**（沉默）。"""
    block = format_host_receipts(receipts)
    if not block:
        return answer
    return f"{answer}\n\n{block}" if answer else block


__all__ = [
    "DEFAULT_MAX_LISTED_PATHS",
    "DEFAULT_MAX_SNAPSHOT_ENTRIES",
    "RECEIPTS_MARKER",
    "ExecObservation",
    "HostReceipts",
    "WorkspaceDiff",
    "WorkspaceSnapshot",
    "append_host_receipts",
    "build_receipts",
    "capture_workspace",
    "diff_workspace",
    "format_host_receipts",
    "split_host_receipts",
]
