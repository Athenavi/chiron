"""P1 回归测试：子 Agent 事件旁路（EventSink）与 L2 输出契约。

对应设计文档 docs/subagent-design.md §4.2/§4.3/§6 的验收点：
* 保留事件名与线上面（type/run_id/depth/parent_run_id/profile/status/usage）；
* 同频道增量按 merge_window 合并；终态不参与丢弃；缓冲上限触发 truncated；
* 每 run 每秒事件预算生效（预览可丢、终态必达）；
* 子 Agent 的思考（[thinking] 包装）与正文分离，思考不进 L2 输出。
"""
from __future__ import annotations

import pytest

from app.agent.event_sink import EV_DONE, EV_REASONING, EV_STARTED, EV_STATUS, EV_TEXT, ST_COMPLETED, ST_TOOL, EventSink
from app.agent.subagent_runner import _split_thinking

# ── 事件面（前端契约）──


def test_started_and_done_carry_hierarchy_fields():
    sink = EventSink()
    sink.emit_started(run_id="rs_1", parent_run_id="rs_0", depth=2, profile="reviewer")
    sink.emit_done(run_id="rs_1", status=ST_COMPLETED, parent_run_id="rs_0", depth=2,
                   profile="reviewer", usage={"steps": 3, "input_tokens": 10})
    payloads = [e.to_payload() for e in sink.drain()]

    assert payloads[0]["type"] == EV_STARTED
    assert payloads[0]["run_id"] == "rs_1"
    assert payloads[0]["parent_run_id"] == "rs_0"
    assert payloads[0]["depth"] == 2
    assert payloads[0]["profile"] == "reviewer"

    done = payloads[-1]
    assert done["type"] == EV_DONE
    assert done["status"] == ST_COMPLETED
    assert done["usage"] == {"steps": 3, "input_tokens": 10}
    # 无内容/未截断时不下发这些键（保持帧精简）
    assert "content" not in done and "truncated" not in done


def test_progress_payload_omits_empty_fields():
    sink = EventSink(merge_window=0.0)
    sink.emit_progress(run_id="rs_1", channel=EV_STATUS, status=ST_TOOL, depth=1)
    payload = sink.drain()[0].to_payload()
    assert payload == {"type": EV_STATUS, "run_id": "rs_1", "depth": 1, "status": ST_TOOL}


# ── 合并与限流 ──


def test_same_channel_increments_merge_into_one_frame():
    sink = EventSink(merge_window=0.0)
    for chunk in ("a", "b", "c"):
        sink.emit_progress(run_id="rs_1", channel=EV_TEXT, content=chunk)
    events = sink.drain()
    assert len(events) == 1
    assert events[0].type == EV_TEXT and events[0].content == "abc"


def test_merge_window_defers_flush():
    sink = EventSink(merge_window=60.0)  # 窗口很长 → 不 flush
    sink.emit_progress(run_id="rs_1", channel=EV_TEXT, content="hold")
    assert sink.drain() == []           # 仍在窗口内，不下发
    sink.emit_done(run_id="rs_1", status=ST_COMPLETED)
    types = [e.type for e in sink.drain()]
    assert types == [EV_TEXT, EV_DONE]  # 终态前强制 flush 缓冲


def test_buffer_limit_truncates_and_marks():
    sink = EventSink(merge_window=0.0, buffer_bytes=64)
    sink.emit_progress(run_id="rs_1", channel=EV_REASONING, content="x" * 200)
    event = sink.drain()[0]
    assert event.truncated is True
    assert len(event.content.encode()) <= 64


def test_per_run_budget_drops_preview_but_keeps_terminal():
    """每秒预算用非内容型（状态）事件验证：超限丢弃并计数，终态不受影响。

    内容型事件会先被同频道合并（本身已大幅降频），因此预算主要作用于状态类事件。
    """
    sink = EventSink(merge_window=0.0, per_run_budget=2)
    for _ in range(10):
        sink.emit_progress(run_id="rs_1", channel=EV_STATUS, status=ST_TOOL)
    sink.emit_done(run_id="rs_1", status=ST_COMPLETED)
    events = sink.drain()
    assert len([e for e in events if e.type == EV_STATUS]) == 2   # 预算内
    assert sink.dropped >= 8                                     # 超额被丢弃并计数
    assert events[-1].type == EV_DONE                            # 终态必达


def test_queue_limit_drops_preview_never_blocks():
    sink = EventSink(maxsize=2, merge_window=0.0, per_run_budget=1000)
    for _i in range(10):
        sink.emit_progress(run_id="rs_1", channel=EV_STATUS, status=ST_TOOL)
    sink.emit_done(run_id="rs_1", status=ST_COMPLETED)
    events = sink.drain()
    assert len(events) <= 3 and events[-1].type == EV_DONE


def test_drain_is_idempotent_and_non_blocking():
    sink = EventSink(merge_window=0.0)
    sink.emit_progress(run_id="rs_1", channel=EV_TEXT, content="x")
    assert len(sink.drain()) == 1
    assert sink.drain() == []           # 已排空


# ── 思考与正文分离 ──


@pytest.mark.parametrize("raw,thinking,answer", [
    ("[thinking]内部推理[/thinking]", "内部推理", ""),
    ("正文回答", "", "正文回答"),
    ("", "", ""),
])
def test_split_thinking(raw, thinking, answer):
    assert _split_thinking(raw) == (thinking, answer)


def test_thinking_never_reaches_l2_output():
    """L2 只应包含正文：思考走 reasoning 频道与 L0，不参与包装输出。"""
    from app.agent.subagent_runner import _wrap_result

    wrapped = _wrap_result("rs_1", "reviewer", ST_COMPLETED, "结论：改动没问题", False)
    assert "结论" in wrapped
    assert "[thinking]" not in wrapped
