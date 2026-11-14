"""Watchdog —— 主循环之外的**语义级**进展判断。

**它回答的问题与既有零件不同**

`loop_guard.py` 只看 `(tool_name, args_hash)` 与结果指纹，答的是"这一次调用是不是和上一次一样"；
`recovery.py` 只对**失败**做路由；`subagent/registry.py` 的 `watchdog_tick` 只看 idle 与
wall-clock。三者都是"事件发生后的机械反应"，没有一个能回答 **"这一轮是否还在向用户的目标靠近"**
—— agent 可以每一步都换工具、每一个调用都成功，整体上却原地绕圈。

**为什么必须是纯函数**

判断一旦自己读时钟、读 DB、调模型，就无法在测试里回放"已知的兜圈子样本"。所以
:func:`assess` 只接收调用方已经取好的摘要（:class:`Sample`），不碰任何 IO ——
数据面在 `app/queue/watchdog_probe.py`，调度与审计在 `app/queue/worker.py`。

**fail-closed 到"不干预"**

这是"用一个判断去审视另一个判断"，误判的代价**不对称**：漏报只是少一次提示（用户自己会打断），
误报却会打断一个正在正常工作的回合。所以样本不足、摘要缺失、阈值非法、回合已结束——
一律 ``CONTINUE``。只有"连续多拍内容与工具调用都没变"这种**可证的事实**才升级。

**三档输出**（与 P1-3 的"继续 / 追加提示 / 建议终止"对齐）

============  ======================================================
``CONTINUE``  正常，什么都不做
``NUDGE``     建议追加一条提示（说明"最近 N 拍没有新内容"）
``SUGGEST_STOP``  建议终止 —— 仍然只是**建议**，是否执行由调用方按开关决定
============  ======================================================
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    """判断结果。顺序即严重度，便于调用方比较。"""

    CONTINUE = "continue"
    NUDGE = "nudge"
    SUGGEST_STOP = "suggest_stop"


#: 连续多少拍"内容与工具都没变"才建议追加提示。
#: 取 2 而不是 1：单拍无变化在正常回合里很常见（模型在思考、在等一个慢工具），
#: 一次就报警会把 watchdog 变成噪音源。
DEFAULT_NUDGE_AFTER = 2

#: 连续多少拍才建议终止。比 ``loop_guard`` 的 repeat 阈值（3 次**调用**）更宽 ——
#: 它看的是"整拍"（一拍里可能已经有很多次调用），时间尺度上其实更早。
DEFAULT_STOP_AFTER = 4


@dataclass(frozen=True)
class Sample:
    """一拍观测的**摘要**，不含原文。

    只保留哈希与计数：watchdog 每隔几分钟跑一次、可能同时看多个回合，把大段结果复制进
    采样历史既无必要也放大内存 —— 判断"有没有变化"只需要能比较的指纹。
    """

    #: 这一拍所属回合。空串表示调用方没给（此时整拍不可用）。
    turn_id: str = ""
    #: ``turns.status`` 的快照：``running`` / ``completed`` / ``failed`` / ``cancelled``。
    turn_status: str = ""
    #: 最近若干条消息**内容**的归一化哈希。
    content_digest: str = ""
    #: 最近若干次工具调用的签名哈希。
    tool_digest: str = ""
    #: 消息条数：用来区分"内容没变但确实在追加"（例如只写文件不回复）。
    message_count: int = 0

    @property
    def usable(self) -> bool:
        """能否参与判断。

        ``content_digest`` 为空 = 这一拍什么都没读到（DB 抖动 / 会话还没落消息）。
        把它算作"无变化"会凭空制造误报，所以直接排除 —— fail-closed 到不干预。
        """
        return bool(self.turn_id) and self.turn_status == "running" and bool(self.content_digest)


@dataclass(frozen=True)
class Assessment:
    """一次判断的结果。"""

    verdict: Verdict = Verdict.CONTINUE
    #: 结尾处"无进展"的连续拍数（``CONTINUE`` 时也可能非零，供日志观察）。
    stalled: int = 0
    #: 人类可读的原因，进审计与日志。空串表示不需要解释。
    reason: str = ""


def digest(parts: Iterable[str]) -> str:
    """把若干摘要片段折成一个可比较的指纹。

    归一化只做两件事：``strip()`` 与用 ``\\x00`` 分隔。**不做**大小写折叠或空白压缩 ——
    模型改写措辞时内容确实变了，那本来就是进展信号，不该被抹平。
    """
    joined = "\x00".join(str(p).strip() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def _stalled_tail(usable: Sequence[Sample]) -> int:
    """从末尾往前数：连续多少拍的（内容、工具、条数）三项都完全相同。

    三项必须同时相等才算"没有进展"：
    - 只看 content：模型可能只写文件不回复，内容不变但产出在增加；
    - 只看 message_count：模型可能反复追加同样的道歉句式。
    """
    if not usable:
        return 0
    tail = usable[-1]
    count = 1
    for prev in reversed(usable[:-1]):
        if (
            prev.content_digest == tail.content_digest
            and prev.tool_digest == tail.tool_digest
            and prev.message_count == tail.message_count
        ):
            count += 1
        else:
            break
    return count


def assess(
    samples: Sequence[Sample],
    *,
    nudge_after: int = DEFAULT_NUDGE_AFTER,
    stop_after: int = DEFAULT_STOP_AFTER,
) -> Assessment:
    """根据一串按时间正序排列的采样，判断"是否还在推进"。

    阈值设为 ``<= 0`` 表示关闭对应档位（与 ``loop_guard`` 的阈值语义一致）。

    **不抛异常**：判断器自身出错时，调用方需要的是"别干预"而不是一个 500。
    """
    if nudge_after <= 0 and stop_after <= 0:
        return Assessment(Verdict.CONTINUE, 0, "watchdog thresholds disabled")

    usable = [s for s in samples if s.usable]
    # 只有一拍时，"连续无变化"无从谈起 —— 至少要有两拍可比。
    if len(usable) < 2:
        return Assessment(Verdict.CONTINUE, 0, "insufficient samples")

    stalled = _stalled_tail(usable)
    if stop_after > 0 and stalled >= stop_after:
        return Assessment(
            Verdict.SUGGEST_STOP,
            stalled,
            f"{stalled} consecutive samples with no new content or tool calls",
        )
    if nudge_after > 0 and stalled >= nudge_after:
        return Assessment(
            Verdict.NUDGE,
            stalled,
            f"{stalled} consecutive samples with no new content or tool calls",
        )
    return Assessment(Verdict.CONTINUE, stalled, "")


#: 追加提示的正文模板。与 ``loop_guard.LOOP_HINTS`` 同一取向：**说清发生了什么**，
#: 而不是只说"你被盯上了"—— 模型据此才有机会改变做法。
NUDGE_MESSAGE = (
    "【进度检查】最近 {stalled} 次采样里，你的回复内容和工具调用都没有变化，"
    "看上去没有向目标推进。请先判断当前卡在哪一步：缺信息就换渠道获取，"
    "缺判断就直接给出结论或说明阻塞点；不要重复已经证明无效的做法。"
)


def nudge_text(assessment: Assessment) -> str:
    """把 ``NUDGE`` 判断转成注入给模型的提示；非 ``NUDGE`` 返回空串。"""
    if assessment.verdict is not Verdict.NUDGE:
        return ""
    return NUDGE_MESSAGE.format(stalled=assessment.stalled)
