"""循环护栏 —— 检测 agent「卡在循环里」。

**为什么需要它**

现有预算都不拦这个形态：轮次没超（`max_turns`）、时间没超（wall）、token 还在涨 ——
agent 只是在反复调同一个工具、拿回同样的结果。它既烧钱又不推进任务，而且是工具连续失败
或 prompt injection 后最常见的收敛失败形态。

**三条规则**（滑动窗口内）

| 规则 | 判据 |
|---|---|
| ``repeat`` | 相同 ``(tool_name, 参数哈希)`` 连续出现 N 次 |
| ``no_progress`` | 连续 M 次工具调用的**结果哈希**相同（换了工具/参数，但什么都没得到） |
| ``oscillation`` | 在两种调用之间来回 ≥ K 次（看着像"探索"，实际零进展） |

**命中后不静默杀死**：由调用方回灌一条结构化提示（重复了几次、结果是否相同），让模型有机会
换策略；连续命中两次以上才终止该轮（接入点见 ``AgentRuntime.run``）。

**"未执行"不算 repeat**（:meth:`LoopGuard.observe_skipped`）

这是一条容易搞反的判据。``repeat`` 的本意是"它已经在反复做同一件**做过了的事**"；
而"参数非法 JSON → 拒绝执行 → 模型补全参数重发"是完全正确的自纠行为 —— 第一次
根本没动手，第二次是必要重试，把它判成死循环等于让自纠永远没有第二次机会。

所以调用方在得知某次调用**没有真正执行**（参数截断被拒、并发批无法交互、计划重复）
时要调 :meth:`observe_skipped`：撤回刚登记的那条调用记录，但**仍然登记结果哈希** ——
这样"连续 N 次同样的失败"会落进 ``no_progress``（那才是真卡住，该换做法而不是重发）。

被 ``repeat`` 自己拦下的调用**不能**撤回，否则它永远拦不住第二次。

参数哈希复用 :func:`app.agent.tool_policy.args_hash` —— 与审批票据同一份归一化，
避免"换个写法就绕开检测"（`./a.txt` vs `a.txt`）。
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

from app.agent.tool_policy import args_hash

REPEAT = "repeat"
NO_PROGRESS = "no_progress"
OSCILLATION = "oscillation"

#: 窗口只用来做"最近若干次"的判定，不保留原文（大结果不该被复制一份）
WINDOW = 12

DEFAULT_REPEAT_THRESHOLD = 3
DEFAULT_NO_PROGRESS_THRESHOLD = 3
DEFAULT_OSCILLATION_THRESHOLD = 4


@dataclass
class LoopVerdict:
    """循环判定结果；``kind`` 为空串表示正常。"""

    kind: str = ""
    count: int = 0
    detail: str = ""

    @property
    def hit(self) -> bool:
        return bool(self.kind)

    def __bool__(self) -> bool:
        # 必须显式定义：dataclass 默认**总是 truthy**，于是 `detect_a() or detect_b()`
        # 会被"未命中"的结果短路掉，第二条规则永远不会被检查（实测 oscillation 因此失效）。
        return self.hit


def _result_digest(result: Any) -> str:
    """结果指纹：只用于"是否变化"的比较。"""
    try:
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 - 不可序列化时退化为 str
        payload = str(result)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class LoopGuard:
    """一次提交（一个 AgentRuntime 实例）内的循环检测器。"""

    def __init__(
        self,
        *,
        repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
        no_progress_threshold: int = DEFAULT_NO_PROGRESS_THRESHOLD,
        oscillation_threshold: int = DEFAULT_OSCILLATION_THRESHOLD,
    ) -> None:
        # 阈值设 0 表示关闭对应规则
        self._repeat_threshold = max(0, repeat_threshold)
        self._no_progress_threshold = max(0, no_progress_threshold)
        self._oscillation_threshold = max(0, oscillation_threshold)
        self._calls: deque[str] = deque(maxlen=WINDOW)
        self._results: deque[str] = deque(maxlen=WINDOW)

    # ── 观测 ──

    def observe_call(self, tool_name: str, args: Any) -> LoopVerdict:
        """登记一次工具调用，返回是否命中 repeat / oscillation。

        ``args`` 可以是 dict，也可以是模型给的**原始 JSON 字符串**（内部解析；解析失败按空参数）
        —— 调用点就不必先解析一遍。
        """
        self._calls.append(self._call_key(tool_name, args))
        return self._detect_repeat() or self._detect_oscillation()

    def _call_key(self, tool_name: str, args: Any) -> str:
        """归一化调用指纹；`observe_call` / `observe_skipped` 必须用同一份。"""
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:  # noqa: BLE001 - 半截 JSON 按空参数（与栅栏的拒绝策略一致）
                args = {}
        return f"{tool_name}|{args_hash(tool_name, args)}"

    def observe_skipped(self, tool_name: str, args: Any) -> bool:
        """撤回"这次调用**没有真正执行**"占用的 repeat 计数；返回是否撤回成功。

        用于参数非法 JSON 被拒、并发批无法交互等**未动手**的情形。

        **为什么必须撤回**：模型补全被 `max_tokens` 截断的 arguments 再发一次，是
        正确的自纠；连续几次被拒后重发同一调用，恰恰是它唯一可做的事。把这些算进
        ``repeat``，第 3 次就会被判死循环、连执行机会都没有 —— 护栏反过来惩罚了自纠。

        **为什么不动结果**：失败次数本身是有信息的，连续 M 次同样的拒绝说明"重发"
        走不通、该换做法 —— 那由收尾处的 ``observe_result`` 照常抓（它的提示语是
        "换策略"而非"别再重复"）。在这里重复登记结果会让一次调用算两次，
        把 ``no_progress`` 的阈值提前一半触发。

        只弹**最后一条且指纹相同**的记录：并发批里多个调用交错登记，
        盲弹 tail 会把别人的记录弹掉。
        """
        key = self._call_key(tool_name, args)
        if self._calls and self._calls[-1] == key:
            self._calls.pop()
            return True
        return False

    def observe_result(self, result: Any) -> LoopVerdict:
        """登记一次工具结果，返回是否命中 no_progress。"""
        self._results.append(_result_digest(result))
        return self._detect_no_progress()

    def reset(self) -> None:
        self._calls.clear()
        self._results.clear()

    # ── 判定 ──

    def _detect_repeat(self) -> LoopVerdict:
        n = self._repeat_threshold
        tail = list(self._calls)
        if n <= 0 or len(tail) < n:
            return LoopVerdict()
        recent = tail[-n:]
        if len(set(recent)) != 1:
            return LoopVerdict()
        name = recent[0].split("|", 1)[0]
        return LoopVerdict(
            REPEAT,
            count=n,
            detail=f"same call repeated {n} times in a row: {name}",
        )

    def _detect_no_progress(self) -> LoopVerdict:
        m = self._no_progress_threshold
        tail = list(self._results)
        if m <= 0 or len(tail) < m:
            return LoopVerdict()
        if len(set(tail[-m:])) != 1:
            return LoopVerdict()
        return LoopVerdict(
            NO_PROGRESS,
            count=m,
            detail=f"last {m} tool calls returned identical results (no new information)",
        )

    def _detect_oscillation(self) -> LoopVerdict:
        k = self._oscillation_threshold
        tail = list(self._calls)
        if k <= 0 or len(tail) < k:
            return LoopVerdict()
        recent = tail[-k:]
        # 恰好两种调用交替（A B A B …）—— 看着在探索，其实在原地打转
        if len(set(recent)) != 2:
            return LoopVerdict()
        if any(recent[i] == recent[i + 1] for i in range(len(recent) - 1)):
            return LoopVerdict()
        return LoopVerdict(
            OSCILLATION,
            count=k,
            detail=f"alternating between two calls for {k} steps without new information",
        )


#: 回灌给模型的提示：**说清发生了什么**，而不是只说"你被拦住了"。
#: 模型据此才有机会换策略（换工具、换参数、或直接回答）。
LOOP_HINTS = {
    REPEAT: (
        "你正在重复同一次工具调用（连续 {count} 次）。若结果确实没有变化，请改变做法："
        "换一个工具或参数、或直接用已有信息作答；不要再次发同一个调用。"
    ),
    NO_PROGRESS: (
        "最近 {count} 次工具调用的结果完全相同，说明这条路没有带来新信息。"
        "请换一种方式获取所需信息，或基于现有信息直接给出结论。"
    ),
    OSCILLATION: (
        "你在两种工具调用之间来回切换了 {count} 次，没有任何进展。"
        "请停下来判断：缺的是信息还是判断？需要信息就换渠道，不需要就直接作答。"
    ),
}


def loop_hint(verdict: LoopVerdict) -> str:
    """把判定转成给模型的回灌提示。"""
    template = LOOP_HINTS.get(verdict.kind, "")
    if not template:
        return ""
    return template.format(count=verdict.count)
