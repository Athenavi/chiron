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
``subagent.approval``    子 Agent 请求批准工具调用（载荷含 tool_call_id/name/arguments，**必达**）
``subagent.done``        唯一终态（载荷：status/usage/result_ref）
======================  ==================================================

必须遵守的边界（性能，§6）：

* **绝不阻塞子 Agent**：队列有界，满时丢弃最早的**预览**事件并置 ``truncated``；
* **限流**：同 (run_id, 频道) 的增量按 ``merge_window`` 合并，每 run 每秒事件预算有限；
* **终态不可丢**：``subagent.done`` 绕过节流与丢弃策略，且终态前排空缓冲。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# ── 保留事件名（前端契约，勿随意改名）──
EV_STARTED = "subagent.started"
EV_STATUS = "subagent.status"
EV_REASONING = "subagent.reasoning"
EV_TEXT = "subagent.text"
EV_NOTICE = "subagent.notice"
EV_APPROVAL = "subagent.approval"
EV_ASK = "subagent.ask"
EV_DONE = "subagent.done"

PREVIEW_EVENTS = frozenset({EV_STATUS, EV_REASONING, EV_TEXT, EV_NOTICE})
TERMINAL_EVENTS = frozenset({EV_STARTED, EV_DONE})
#: 必须送达的事件：终态（结论）、审批与提问（用户在等它，丢了就空转到超时）。
#: 它们**绕过每秒预算**，队列满时也不会被丢弃策略牺牲。
IMPORTANT_EVENTS = frozenset({EV_STARTED, EV_DONE, EV_APPROVAL, EV_ASK})

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
DEFAULT_DELIVER_QUEUE = 1024     # 常驻投递器的待发队列上限（满了计数丢弃，不阻塞 emit）


@dataclass
class SubagentEvent:
    """一条待发给前端的子 Agent 进度事件（会被序列化进父 SSE 帧）。"""

    type: str
    run_id: str
    #: 父会话 id —— 前端 SSE 按它过滤，**必须**带上（缺了会被投给所有订阅者，造成串扰）
    session_id: str = ""
    parent_run_id: str = ""
    depth: int = 1
    profile: str = ""
    status: str = ""
    content: str = ""
    truncated: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    #: 审批事件的回传凭据（前端据此调用 /v1/agent/approval）
    tool_call_id: str = ""
    tool_name: str = ""
    tool_arguments: str = ""
    #: 提问事件（``subagent.ask``）的建议答案：前端渲染成可点击选项。
    #: 与主 Agent 的 ask 事件同形，因此前端可以复用同一个 AskCard。
    options: list[str] = field(default_factory=list)
    allow_free_text: bool = True
    ts: float = field(default_factory=time.time)

    def to_payload(self) -> dict[str, Any]:
        """转成 SSE frame 的 payload（与 main.py 既有字段风格一致）。"""
        payload: dict[str, Any] = {
            "type": self.type,
            "run_id": self.run_id,
            "depth": self.depth,
        }
        if self.session_id:
            payload["session_id"] = self.session_id
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
        if self.summary:
            payload["summary"] = self.summary
        # 审批字段：只发这一组**明确的**名字，不做别名 ——
        # 回放路径（Go 的 eventsFromRedis）会把 payload 的 `id` 覆盖成 Redis Stream 消息 id，
        # 若前端按 `id` 回传审批决定，就会拿一个 stream id 去当 tool_call_id（必然失败）。
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_name:
            payload["tool_name"] = self.tool_name
        if self.tool_arguments:
            payload["tool_arguments"] = self.tool_arguments
        # 提问字段：与主 Agent 的 ask 帧同名（question/options/allow_free_text），
        # 前端因此能直接复用 AskCard 组件与 /v1/agent/answer 回传通道。
        if self.type == EV_ASK:
            payload["question"] = self.content
            payload["options"] = list(self.options)
            payload["allow_free_text"] = self.allow_free_text
        return payload


class EventSink:
    """有界、限流的子 Agent 事件队列（父 SSE 生成器消费）。"""

    def __init__(
        self,
        *,
        session_id: str = "",
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        merge_window: float = DEFAULT_MERGE_WINDOW,
        per_run_budget: int = DEFAULT_PER_RUN_BUDGET,
        buffer_bytes: int = DEFAULT_BUFFER_BYTES,
    ):
        self._session_id = session_id
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
        # 常驻投递器：**不依赖父 SSE 生成器存活**（见 attach_persistent 的说明）
        self._deliverers: list[Callable[[dict[str, Any]], Awaitable[None]]] = []
        self._deliver_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=DEFAULT_DELIVER_QUEUE)
        self._deliver_task: asyncio.Task | None = None
        self.delivery_dropped = 0

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

    def emit_approval(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str = "",
        tool_arguments: str = "",
        content: str = "",
        parent_run_id: str = "",
        depth: int = 1,
        profile: str = "",
    ) -> None:
        """子 Agent 正在等用户批准某个工具调用 —— **必须送达**。

        为什么不能像其它进度事件那样"能丢就丢"：子 Agent 此刻**阻塞**在这个工具调用上
        （runtime 在等 ``submit_approval``），事件丢了用户就永远不知道该批准什么，
        表现为空转 300 秒后以 ``approval timed out`` 被拒 —— 一次必然发生的空转，
        而且用户不知道为什么在等。

        因此它走 ``IMPORTANT_EVENTS``：绕过每秒预算、队列满时不牺牲；同时**不做内容合并**
        （合并会把两次不同的审批揉成一条）。回传凭据是 ``tool_call_id`` ——
        前端用它调 ``POST /v1/agent/approval``，与主 Agent 的审批走完全同一条通道。
        """
        if not tool_call_id:
            # 没有回传凭据的审批事件是**无用的**：前端没法把决定送回引擎。
            # 宁可记一条告警（可见），也不发一张点了没反应的卡片。
            logger.warning(
                "subagent %s: approval event without tool_call_id dropped", run_id
            )
            return
        self._push(SubagentEvent(
            type=EV_APPROVAL,
            run_id=run_id,
            parent_run_id=parent_run_id,
            depth=depth,
            profile=profile,
            content=content,
            status=ST_RUNNING,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_arguments=tool_arguments,
        ))

    def emit_ask(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        question: str,
        options: list[str] | None = None,
        allow_free_text: bool = True,
        parent_run_id: str = "",
        depth: int = 1,
        profile: str = "",
    ) -> None:
        """子 Agent 正在向用户提问 —— **必须送达**（与审批同构）。

        子 Agent 调用 ``ask_user`` 时会阻塞等答案：事件丢了用户就不知道它问了什么，
        只能空转到超时（默认 300s）。载荷刻意与主 Agent 的 ask 帧**同名**
        （``question`` / ``options`` / ``allow_free_text``），前端因此能复用同一个
        ``AskCard``，回传走同一条 ``POST /v1/agent/answer``（答案是文本，不是布尔）。
        """
        if not tool_call_id:
            # 没有回传凭据的提问事件是**无用**的：前端没法把答案送回去。
            logger.warning("subagent %s: ask event without tool_call_id dropped", run_id)
            return
        self._push(SubagentEvent(
            type=EV_ASK,
            run_id=run_id,
            parent_run_id=parent_run_id,
            depth=depth,
            profile=profile,
            content=question or "",
            status=ST_RUNNING,
            tool_call_id=tool_call_id,
            options=[str(o) for o in (options or []) if str(o).strip()],
            allow_free_text=bool(allow_free_text),
        ))

    def emit_done(
        self,
        *,
        run_id: str,
        status: str,
        parent_run_id: str = "",
        depth: int = 1,        profile: str = "",
        usage: dict[str, Any] | None = None,
        summary: str = "",
    ) -> None:
        """写入唯一终态：先排空该 run 的预览缓冲，再入队（不参与丢弃）。

        带上 ``summary``（L1 摘要）后，前端收到终态的那一刻就能显示结论，
        不必再等下一轮 ``GET /v1/subagent/runs`` 轮询。
        """
        self._flush_pending(run_id, force=True)
        self._push(SubagentEvent(type=EV_DONE, run_id=run_id, parent_run_id=parent_run_id,
                                 depth=depth, profile=profile, status=status,
                                 usage=usage or {}, summary=summary), terminal=True)

    # ── 常驻投递端（生命周期与 sink 绑定，**不依赖父 SSE 生成器**）──

    def attach_persistent(self, deliver: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """注册一个常驻投递器，并启动后台冲刷任务。

        为什么需要它：``drain()`` 的调用方是父 SSE 生成器（``main.py``），而父 turn 一结束
        生成器就 return —— 此时 ``run_in_background=True`` 的子 Agent 往往才跑到一半，
        它后续 emit 的事件既进不了 SSE、也写不进运行期缓存；前端面板与
        ``GET /v1/subagent/runs/{id}/events`` 于是永远看不到后台 run 的进度。

        投递器挂在 sink 自己身上：只要还有任务持有这个 sink（contextvar 会随
        ``asyncio.create_task`` 复制给后台任务），事件就会被投递出去。
        """
        self._deliverers.append(deliver)
        if self._deliver_task is None:
            # 持引用，避免后台任务被 GC 静默回收
            self._deliver_task = asyncio.create_task(self._flush_deliveries())

    async def _flush_deliveries(self) -> None:
        while True:
            payload = await self._deliver_queue.get()
            for deliver in list(self._deliverers):
                try:
                    await deliver(payload)
                except Exception as exc:  # noqa: BLE001 - 投递失败不能影响子 Agent
                    logger.warning("subagent event delivery failed: %s", str(exc)[:200])

    def _fanout(self, event: SubagentEvent) -> None:
        """把**已通过限流**的事件投递给常驻投递器（非阻塞；队列满则计数丢弃）。"""
        if not self._deliverers:
            return
        try:
            self._deliver_queue.put_nowait(event.to_payload())
        except asyncio.QueueFull:
            self.delivery_dropped += 1

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
        # 会话归属：每条事件的每一份拷贝都要带父会话 id（前端 SSE 靠它路由）
        if not event.session_id:
            event.session_id = self._session_id
        # 终态与审批属于 IMPORTANT_EVENTS：不受每秒预算约束（预算超限时丢弃的预览事件
        # 是"可再生的进度"，而审批/终态丢了就是**结论丢失**或**空转到超时**）。
        important = terminal or event.type in IMPORTANT_EVENTS
        # 每秒预算：非重要事件超限即丢弃 —— 在入队时判定，
        # 比在 drain 时判定更早释放内存，也避免积压后集中丢弃造成的抖动。
        if not important:
            if not self._consume_budget(event.run_id):
                self.dropped += 1
                return
        if len(self._queue) >= self._maxsize:
            self._drop_oldest_preview()
            if not important:
                self.dropped += 1
                return
        self._queue.append(event)
        self._fanout(event)

    def _drop_oldest_preview(self) -> None:
        """腾一个位置：牺牲最旧的非重要事件（终态/审批绝不牺牲）。"""
        for i, event in enumerate(self._queue):
            if event.type not in IMPORTANT_EVENTS:
                del self._queue[i]
                return
        # 全是重要事件：放弃最旧的一条
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
