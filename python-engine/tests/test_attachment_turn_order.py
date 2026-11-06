"""本轮附件的装配顺序：**历史先卸载，本轮再注入**。

这个顺序不是风格问题，而是"用户刚发的图能不能被模型看到"的分界：

* `offload_inline_media` 不看消息新旧，只要见到 >8KiB 的 `data:` 图片就换成
  `<media_ref>` 文本（历史该这么瘦身，否则每轮都要重发 base64）。
* `attachments.to_content_parts` 产出的图片**永远**是 `data:` URL（它没有别的形态，
  见 `_image_data_url`）。

两者一旦顺序颠倒，本轮图片会在送出前那一刻被换成一行引用文本 —— 实测复现：
parts 从 `['image_url','text']` 变成 `['text','text','text']`，模型根本没看到图，
而用户以为"我明明发了图"。normal 模式默认开压缩，所以这是默认路径上的行为。

本文件钉两件事：

1. **组合语义**（行为）：先卸载历史、再注入附件之后，本轮图片仍是 `image_url` 块，
   历史那张变成引用 —— 并顺带证明反过来做会坏（否则这个测试没有说服力）。
2. **接线顺序**（源码守卫）：`runtime.py` 里两个调用的先后，以及"附件注入不在
   `enable_compaction` 分支里"（minimal 模式关压缩，但附件照样要给模型）。
   用 `tests.source_scan` 剥注释再匹配 —— 只匹配字面量的话，把调用注释掉也能骗过它。
"""

from __future__ import annotations

import base64
import io
import random
from pathlib import Path
from typing import Any

from PIL import Image

from app.agent.media_offload import offload_inline_media
from app.agent.runtime import _with_attachment_parts
from app.agent.runtime_types import AgentTask
from tests.source_scan import code_of

RUNTIME = Path(__file__).resolve().parents[1] / "app" / "agent" / "runtime.py"
#: 单条消息在本轮用户消息之前的锚点（`_with_attachment_parts` 靠 role+content 定位）
QUESTION = "这轮的问题"


def _png(size: int = 200) -> bytes:
    """噪声图：纯色图会被 PNG 压到几百字节，落不到 8KiB 卸载阈值之上。"""
    pixels = random.Random(size).randbytes(size * size * 3)
    buf = io.BytesIO()
    Image.frombytes("RGB", (size, size), pixels).save(buf, format="PNG")
    return buf.getvalue()


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _task(attachment_url: str) -> AgentTask:
    return AgentTask(
        id="t1",
        tenant_id=1,
        user_id="u1",
        session_id="s1",
        content=QUESTION,
        llm_config={"mode": "normal"},
        max_turns=1,
        attachments=[
            {"id": "a1", "name": "本轮.png", "mime_type": "image/png", "url": attachment_url}
        ],
    )


def _history(history_url: str) -> list[dict[str, Any]]:
    """历史里已有一张内联图，然后是本轮那条用户消息。"""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "上一轮"},
                {"type": "image_url", "image_url": {"url": history_url}},
            ],
        },
        {"role": "assistant", "content": "收到"},
        {"role": "user", "content": QUESTION},
    ]


def _types(content: Any) -> list[str]:
    return [part.get("type", "") for part in content if isinstance(part, dict)]


async def _fake_persist(name: str, data: bytes, mime: str) -> dict[str, Any]:
    return {"id": "asset_hist", "file_url": "https://gw/v1/media/asset_hist"}


async def test_history_offloaded_before_turn_attachment_injected(monkeypatch):
    """正确顺序：历史变引用，本轮图片原样进模型。"""
    monkeypatch.setattr("app.agent.media_offload._persist", _fake_persist)

    messages = await offload_inline_media(_history(_data_url(_png())))
    messages = await _with_attachment_parts(messages, _task(_data_url(_png())), "gpt-4o")

    # 历史那条：base64 没了，留下可追溯的引用
    assert _types(messages[0]["content"]) == ["text", "text"]
    assert "<media_ref" in messages[0]["content"][1]["text"]

    # 本轮那条：图片必须还在
    assert "image_url" in _types(messages[-1]["content"]), (
        "本轮附件图片被卸载了 —— 模型看到的只会是一行引用文本"
    )


async def test_injecting_before_offload_loses_the_turn_image(monkeypatch):
    """反证：旧顺序（先注入、后卸载）会让本轮图片消失。

    保留这条是为了让"顺序为什么不能动"在测试里有据可查：删掉上面的顺序改动，
    这个用例仍然通过、而上一个失败 —— 失败信息就指向真正的原因。
    """
    monkeypatch.setattr("app.agent.media_offload._persist", _fake_persist)

    messages = await _with_attachment_parts(_history(_data_url(_png())), _task(_data_url(_png())), "gpt-4o")
    messages = await offload_inline_media(messages)

    assert "image_url" not in _types(messages[-1]["content"]), (
        "先注入后卸载时，本轮图片应当被换掉 —— 这正是要防的行为；"
        "如果这里通过了，说明 offload 的行为变了，两个用例都要重看"
    )


# ── 接线顺序（源码守卫）──────────────────────────────────────────────────


def _code_lines() -> list[str]:
    return code_of(RUNTIME).splitlines()


def _find(lines: list[str], needle: str) -> int:
    hits = _find_all(lines, needle)
    assert len(hits) == 1, f"{needle!r} 应当恰好出现一次（实际 {len(hits)} 次）"
    return hits[0]


def _find_all(lines: list[str], needle: str) -> list[int]:
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert hits, f"{needle!r} 在源码里找不到（接线被删了？）"
    return hits


def test_runtime_offloads_history_before_injecting_attachments():
    lines = _code_lines()
    offload_at = _find(lines, "await offload_inline_media(")
    inject_at = _find(lines, "await _with_attachment_parts(")

    assert offload_at < inject_at, (
        "runtime 必须先卸载历史内联媒体、再注入本轮附件；顺序颠倒会让本轮图片被换成引用文本"
    )


def test_turn_attachment_injection_is_outside_the_compaction_branch():
    """minimal 模式关压缩，但附件照样要给模型 —— 注入不能挂在 `enable_compaction` 里面。

    `if mode_cfg.enable_compaction:` 在文件里出现多次（回合前、回合中、收尾各一处），
    所以判据不是"找那一条语句"，而是"注入行上方最近的那个 gate 是否与它同缩进"：
    同缩进 ⇒ 注入在 gate 块之外；更深 ⇒ 被裹进了分支。
    """
    lines = _code_lines()
    inject_at = _find(lines, "await _with_attachment_parts(")
    gates = [i for i in _find_all(lines, "if mode_cfg.enable_compaction:") if i < inject_at]
    assert gates, "注入之前应当至少有一个 enable_compaction 判定"

    def indent(index: int) -> int:
        return len(lines[index]) - len(lines[index].lstrip())

    gate_at = gates[-1]
    assert indent(inject_at) <= indent(gate_at), (
        "附件注入被放进了 enable_compaction 分支：关闭压缩的模式下附件会整段丢失"
    )
