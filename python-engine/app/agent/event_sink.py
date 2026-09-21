"""子 Agent 事件旁路（EventSink）—— 把子 Agent 的进度送到父会话的 SSE 流。

背景（docs/subagent-design.md §4.3）：子 Agent 在工具调用**内部**运行（``subagent`` handler），
它产生的 ``AgentEvent`` 默认被丢弃，前端因此看不到子 Agent 在做什么。本模块提供一条
**有界旁路**：子事件经限流/合并写入队列，由 ``main.py`` 的事件生成器合并进父 SSE 流。

线上契约（保留事件名，前端按 ``subagent.`` 前缀识别；见 §4.2）：

======================  ==================================================
``subagent.started``     建树 + 卡片出现（载荷：run_id/parent/depth/profile）
``subagent.status``      阶段：queued|running|reasoning|responding|tool|retrying|completed|failed|cancelled
``subagent.reasoning``   思考增量（受限）
``subagent.text``        回答增量（受限）
``subagent.notice``      提示/告警（含"预览被截断"）
``subagent.done``        唯一终态（载荷：status/usage/result_ref）
======================  ==================================================

必须遵守的边界（性能，§6）：

* **绝不阻塞子 Agent**：队列有界，满时丢弃最早的**预览**事件并置 ``truncated``；
* **限流**：同 (run_id, 频道) 的增量按 ``merge_window`` 合并，每 run 每秒事件预算有限；
* **终态不可丢**：``subagent.done`` 绕过节流与丢弃策略，且终态前排空缓冲。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── 保留事件名（前端契约，勿随意改名）──
EV_STARTED = "subagent.started"
EV_STATUS = "subagent.status"
EV_REASONING = "subagent.reasoning"
EV_TEXT = "subagent.text"
EV_NOTICE = "subagent.notice"
EV_DONE = "subagent.done"

PREVIEW_EVENTS = frozenset({EV_STATUS, EV_REASONING, EV_TEXT, EV_NOTICE})
TERMINAL_EVENTS = frozenset({EV_STARTED, EV_DONE})

# 阶段取值（与前端徽标一致）
ST_QUEUED = "queued"
ST_RUNNING = "running"
ST_REASONING = "reasoning"
ST_RESPONDING = "responding"
ST_TOOL = "tool"
ST_RETRYING = "retrying"
ST_COMPLETED = "completed"
ST_FAILED = "failed"
ST_CANCELLED = "cancelled"

# 默认上限（可由构造参数覆盖）
DEFAULT_QUEUE_MAXSIZE = 256
DEFAULT_MERGE_WINDOW = 0.25      # 同频道合并窗口（秒）
DEFAULT_PER_RUN_BUDGET = 20      # 每 run 每秒非终态事件预算
DEFAULT_BUFFER_BYTES = 8 * 1024  # 同频道未发送缓冲上限


@dataclass
class SubagentEvent:
    """一条待发给前端的子 Agent 进度事件（会被序列化进父 SSE 帧）。"""

    type: str
    run_id: str
    parent_run_id: str = ""
    depth: int = 1
    profile: str = ""
    status: str = ""
    content: str = ""
    truncated: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_payload(self) -> dict[str, Any]:
        """转成 SSE frame 的 payload（与 main.py 既有字段风格一致）。"""
        payload: dict[str, Any] = {
            "type": self.type,
            "run_id": self.run_id,
            "depth": self.depth,
        }
        if self.parent_run_id:
            payload["parent_run_id"] = self.parent_run_id
        if self.profile:
            payload["profile"] = self.profile
        if self.status:
            payload["status"] = self.status
        if self.content:
            payload["content"] = self.content
        if self.truncated:
            payload["truncated"] = True
        if self.usage:
            payload["usage"] = self.usage
        return payload


class EventSink:
    """有界、限流的子 Agent 事件队列（父 SSE 生成器消费）。"""

    def __init__(
        self,
        *,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        merge_window: float = DEFAULT_MERGE_WINDOW,
        per_run_budget: int = DEFAULT_PER_RUN_BUDGET,
        buffer_bytes: int = DEFAULT_BUFFER_BYTES,
    ):
        self._queue: deque[SubagentEvent] = deque()
        self._maxsize = maxsize
        self._merge_window = merge_window
        self._budget = per_run_budget
        self._buffer_bytes = buffer_bytes
        # (run_id, channel) -> (pending_text, first_pending_ts, truncated)
        self._pending: dict[tuple[str, str], list] = {}
        # run_id -> (window_start, count)
        self._budget_state: dict[str, list] = {}
        self.dropped = 0

    # ── 写入端（由 SubAgentRunner 调用）──

    def emit_started(self, *, run_id: str, parent_run_id: str = "", depth: int = 1, profile: str = "") -> None:
        self._push(SubagentEvent(type=EV_STARTED, run_id=run_id, parent_run_id=parent_run_id,
                                 depth=depth, profile=profile, status=ST_RUNNING))

    def emit_progress(
        self,
        *,
        run_id: str,
        channel: str,
        content: str = "",
        status: str = "",
        parent_run_id: str = "",
        depth: int = 1,
        profile: str = "",
    ) -> None:
        """写入一条预览事件（可能被合并/丢弃 —— 不保证送达）。"""
        if not content and channel in (EV_REASONING, EV_TEXT, EV_NOTICE):
            return
        key = (run_id, channel)
        if content:
            # 内容型：同频道合并后由 drain() 统一发出；**层级字段随缓冲保留**
            # （否则增量事件会丢失 parent_run_id/depth，前端无法归到正确节点）
            key = (run_id, channel)
            entry = self._pending.get(key)
            if entry is None:
                text, truncated = content, False
                if len(text.encode("utf-8", "ignore")) > self._buffer_bytes:
                    # 单条即超限：直接截断并标记（避免一条超大增量绕过上限）
                    text = _tail_bytes(text, self._buffer_bytes)
                    truncated = True
                self._pending[key] = {
                    "text": text,
                    "ts": time.time(),
                    "truncated": truncated,
                    "parent_run_id": parent_run_id,
                    "depth": depth,
                    "profile": profile,
                }
            else:
                entry["text"] += content
                if len(entry["text"].encode("utf-8", "ignore")) > self._buffer_bytes:
                    # 超限：保留尾部（UTF-8 安全），并标记截断
                    entry["text"] = _tail_bytes(entry["text"], self._buffer_bytes)
                    entry["truncated"] = True
                if parent_run_id:
                    entry["parent_run_id"] = parent_run_id
                if depth:
                    entry["depth"] = depth
                if profile:
                    entry["profile"] = profile
            return  # 内容型事件统一由 drain() 合并发出
        # 非内容型（状态）直接入队
        self._push(SubagentEvent(type=channel, run_id=run_id, parent_run_id=parent_run_id,
                                 depth=depth, profile=profile, status=status))

    def emit_done(
        self,
        *,
        run_id: str,
        status: str,
        parent_run_id: str = "",
        depth: int = 1,
        profile: str = "",
        usage: dict[str, Any] | None = None,
    ) -> None:
        """写入唯一终态：先排空该 run 的预览缓冲，再入队（不参与丢弃）。"""
        self._flush_pending(run_id, force=True)
        self._push(SubagentEvent(type=EV_DONE, run_id=run_id, parent_run_id=parent_run_id,
                                 depth=depth, profile=profile, status=status, usage=usage or {}),
                   terminal=True)

    # ── 消费端（由父 SSE 生成器调用）──

    def drain(self) -> list[SubagentEvent]:
        """取出当前可发事件（非阻塞）。

        先把等待超过 ``merge_window`` 的内容缓冲合并入队，再排空队列。
        预算与容量控制已在入队（``_push``）时完成，这里只负责"取走"。
        """
        self._flush_pending()
        out: list[SubagentEvent] = []
        while self._queue:
            out.append(self._queue.popleft())
        return out

    def pending_count(self) -> int:
        return len(self._queue)

    # ── 内部 ──

    def _push(self, event: SubagentEvent, *, terminal: bool = False) -> None:
        # 每秒预算：非终态事件超限即丢弃（终态始终放行）—— 在入队时判定，
        # 比在 drain 时判定更早释放内存，也避免积压后集中丢弃造成的抖动。
        if not terminal and event.type not in TERMINAL_EVENTS:
            if not self._consume_budget(event.run_id):
                self.dropped += 1
                return
        if len(self._queue) >= self._maxsize:
            if terminal:
                # 终态必须入队：丢一个最旧的预览腾位置
                self._drop_oldest_preview()
            else:
                self._drop_oldest_preview()
                self.dropped += 1
                return
        self._queue.append(event)

    def _drop_oldest_preview(self) -> None:
        for i, event in enumerate(self._queue):
            if event.type not in TERMINAL_EVENTS:
                del self._queue[i]
                return
        # 全是终态：放弃最旧的一条
        if self._queue:
            self._queue.popleft()

    def _flush_pending(self, run_id: str | None = None, *, force: bool = False) -> None:
        """把到期的内容缓冲合并成事件入队（层级字段随事件一起带出）。"""
        now = time.time()
        for (rid, channel), entry in list(self._pending.items()):
            if run_id is not None and rid != run_id:
                continue
            if not force and (now - entry["ts"]) < self._merge_window:
                continue
            if entry["text"] or entry["truncated"]:
                self._push(SubagentEvent(
                    type=channel,
                    run_id=rid,
                    parent_run_id=entry["parent_run_id"],
                    depth=entry["depth"],
                    profile=entry["profile"],
                    content=entry["text"],
                    truncated=entry["truncated"],
                ))
            self._pending.pop((rid, channel), None)

    def _consume_budget(self, run_id: str) -> bool:
        """每 run 每秒非终态事件预算。"""
        now = time.time()
        state = self._budget_state.get(run_id)
        if state is None or now - state[0] >= 1.0:
            self._budget_state[run_id] = [now, 1]
            return True
        if state[1] >= self._budget:
            return False
        state[1] += 1
        return True


def _tail_bytes(text: str, limit: int) -> str:
    """按 UTF-8 字节安全地取尾部（避免截断多字节字符）。"""
    encoded = text.encode("utf-8", "ignore")
    if len(encoded) <= limit:
        return text
    return encoded[-limit:].decode("utf-8", "ignore")


def get_event_sink() -> EventSink | None:
    """从工具上下文取当前 run 的事件旁路（未启用时为 None）。"""
    from app.tools.context import get_tool_context

    return get_tool_context("event_sink")
