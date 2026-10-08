"""审批**入口**清单（与 `tests/test_exec_audit_inventory.py` 同一手法）。

**为什么需要它**：`approval_audit` 此前**只记"决定"、不记"请求"** ⇒ "**问了但没等到决定**"
（等待期间进程被杀、副本被回收、连接断开后无人再答）在流水里**完全看不出来**。
补上 `asked` 之后，真正的风险变成"**将来新增一条审批入口时忘了记 `asked`**" ——
这类缺口靠"看代码"发现不了，只能靠枚举：本用例枚举 `app/` 下**发出审批请求**的位置，
要求逐条归类；新增一处未归类即失败。

四类（与 exec 审计清单同构）：

1. `AUDITED` —— 该文件发出审批事件，**并且**调用 `record_approval_request`（入口留痕）；
2. `FORWARDS_A_RECORDED_ASK` —— 把**已被记录过**的子 Agent 审批**转发**给父 sink，
   自己不是发起方（发起方是子 Agent 自己的 runtime）；
3. `EXEMPT` —— 不是"发起审批"，理由必须写明；
4. `KNOWN_UNAUDITED` —— 已知缺口（**当前为空**，有缺口就必须显式记账）。

判据口径：**"有审计" ≠ "入口有审计"**（见 `vendor/规划.md` §4 的"有审计就等于每条执行路径
都留痕"一行）—— 出口记的是"发生了的结果"，入口记的是"发生过的事"。
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

#: 视为"发出审批请求"的源码形态（三种：主 Agent 的事件、子 Agent 的事件、转发调用）。
_PATTERNS = (
    re.compile(r'type\s*=\s*"approval"'),
    re.compile(r"type\s*=\s*EV_APPROVAL"),
    re.compile(r"\bemit_approval\s*\("),
)

#: 发起方：自己发事件 + 自己记入口。
AUDITED: dict[str, str] = {
    "agent/runtime.py": "主/子 Agent 的 confirm 分支：发 approval 事件并调 _record_approval_request",
}
#: 转发方：把子 Agent 已记录的审批转发给父 sink（发起方是子 Agent 自己的 runtime）。
FORWARDS_A_RECORDED_ASK: dict[str, str] = {
    "agent/event_sink.py": "定义 emit_approval（子 Agent 审批的转发事件，非发起）",
    "agent/subagent_runner.py": "调用 sink.emit_approval 转发子 Agent 的审批事件",
}
EXEMPT: dict[str, str] = {}
KNOWN_UNAUDITED: dict[str, str] = {}


def _sources() -> dict[str, str]:
    """`app/` 下所有 `.py`（相对 `app/` 的 posix 路径 → 文本）。"""
    out: dict[str, str] = {}
    for path in APP.rglob("*.py"):
        out[path.relative_to(APP).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
    return out


def test_every_approval_entry_point_is_classified() -> None:
    """枚举 `app/` 下所有发出审批请求的位置；未归类即失败。"""
    sources = _sources()
    found = {
        rel
        for rel, text in sources.items()
        if any(p.search(text) for p in _PATTERNS)
    }
    classified = set(AUDITED) | set(FORWARDS_A_RECORDED_ASK) | set(EXEMPT) | set(KNOWN_UNAUDITED)

    unclassified = sorted(found - classified)
    assert not unclassified, (
        "这些文件发出审批请求但未归类（新增审批入口必须归类，否则可能绕过 asked 留痕）：\n  "
        + "\n  ".join(unclassified)
        + "\n加入 AUDITED / FORWARDS_A_RECORDED_ASK / EXEMPT（写明理由）或 KNOWN_UNAUDITED。"
    )

    stale = sorted(classified - found)
    assert not stale, f"这些文件已归类但已不再发出审批请求，请清理：{stale}"


def test_audited_files_really_record_the_request() -> None:
    """`AUDITED` 里的文件必须**真的**能看到入口留痕调用 —— 防止把缺口写成"已修"。"""
    sources = _sources()
    missing = [
        rel
        for rel in AUDITED
        if rel in sources and "record_approval_request" not in sources[rel]
    ]
    assert not missing, f"这些文件被标为 AUDITED 但找不到 record_approval_request：{missing}"


def test_classification_sets_do_not_overlap() -> None:
    """一个文件只能属于一类 —— 否则"归类"本身就没有判据。"""
    overlap = (set(KNOWN_UNAUDITED) & (set(AUDITED) | set(FORWARDS_A_RECORDED_ASK) | set(EXEMPT)))
    assert not overlap, f"同一个文件被归了多类：{sorted(overlap)}"
