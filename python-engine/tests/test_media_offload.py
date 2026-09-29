"""C2 段 c：内联媒体卸载 + 历史参数截断（方案 01 §3.2）。

两件事的验收：

* **媒体**：大内联块 → ``<media_ref>``（带 id / path / bytes / tool）；**卸载失败写占位符**
  （``<media_omitted>``）而不是静默丢弃；小内联块不动（卸载它们只是制造无谓的媒体资产）。
* **参数**：超长 `tool_calls[].arguments` → 保留 **JSON 骨架** + 标注 ``...(argument truncated)``，
  且**绝不动 `id` / `name`** —— 工具配对完整性是 API 契约，改坏就是 400。
"""

from __future__ import annotations

import base64
import json

from app.agent.media_offload import (
    DEFAULT_MIN_BYTES,
    TRUNCATION_MARK,
    _attr,
    offload_inline_media,
    truncate_tool_call_arguments,
)
from app.agent.runtime import CompactionConfig, _compact_messages

# 够大（超过卸载阈值）与够小（能匹配 data URL 正则但低于阈值）
BIG = b"x" * (DEFAULT_MIN_BYTES + 1024)
SMALL = b"t" * 100


def _data_url(payload: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


# ── 参数截断 ────────────────────────────────────────────────────────────


def test_truncates_oversized_arguments_keeping_skeleton():
    args = json.dumps({"path": "a.txt", "content": "y" * 5000}, ensure_ascii=False)
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "name": "write_file", "arguments": args}],
        }
    ]

    out = truncate_tool_call_arguments(messages, CompactionConfig(arg_max_chars=500))
    call = out[0]["tool_calls"][0]

    assert len(call["arguments"]) <= 500
    assert TRUNCATION_MARK in call["arguments"], "截断必须被标注（否则被误当成完整参数）"
    # 骨架（键）还在：模型能看出"这是 write_file，参数被截了"
    assert "path" in call["arguments"] and "content" in call["arguments"]
    # 配对凭据一个字节都不动
    assert call["id"] == "call_1" and call["name"] == "write_file"


def test_tool_pairs_stay_intact():
    args = json.dumps({"content": "z" * 5000})
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_9", "name": "write_file", "arguments": args}],
        },
        {"role": "tool", "tool_call_id": "call_9", "content": "ok"},
    ]

    out = truncate_tool_call_arguments(messages, CompactionConfig(arg_max_chars=200))

    assert out[0]["tool_calls"][0]["id"] == "call_9"
    assert out[1]["tool_call_id"] == "call_9"


def test_short_arguments_untouched_identity_preserved():
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "c", "name": "t", "arguments": '{"a": 1}'}]}
    ]
    out = truncate_tool_call_arguments(messages, CompactionConfig())
    assert out[0] is messages[0], "没动过的消息应保持同一对象（避免无谓重建）"


def test_disabled_when_zero():
    long_args = "x" * 10000
    messages = [{"role": "assistant", "tool_calls": [{"id": "c", "name": "t", "arguments": long_args}]}]
    out = truncate_tool_call_arguments(messages, CompactionConfig(arg_max_chars=0))
    assert out[0]["tool_calls"][0]["arguments"] == long_args


def test_non_json_arguments_truncated_and_marked():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "c", "name": "t", "arguments": "not json " * 500}],
        }
    ]
    out = truncate_tool_call_arguments(messages, CompactionConfig(arg_max_chars=100))
    value = out[0]["tool_calls"][0]["arguments"]
    assert len(value) <= 100
    assert value.endswith(TRUNCATION_MARK)


def test_compact_messages_applies_truncation():
    """截断挂在压缩链上（`_compact_messages` 的所有策略路径都会经过）。"""
    long_args = json.dumps({"content": "y" * 5000})
    messages = [
        {"role": "system", "content": "s"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "write_file", "arguments": long_args}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]

    out = _compact_messages(messages, CompactionConfig())

    assistant = next(m for m in out if m.get("role") == "assistant")
    assert TRUNCATION_MARK in assistant["tool_calls"][0]["arguments"]


# ── 媒体卸载 ────────────────────────────────────────────────────────────


async def _no_persist(name: str, data: bytes, mime: str):  # pragma: no cover - 默认替身
    raise AssertionError("persist should not be called")


async def test_offloads_large_inline_media(monkeypatch):
    async def fake_persist(name: str, data: bytes, mime: str):
        return {"id": "asset_1", "file_url": "https://gw/v1/media/asset_1"}

    monkeypatch.setattr("app.agent.media_offload._persist", fake_persist)

    url = _data_url(BIG)
    out = await offload_inline_media([{"role": "user", "content": f"看这张图 {url}"}])

    text = out[0]["content"]
    assert url not in text, "内联 base64 必须被移出历史"
    assert '<media_ref id="asset_1" path="https://gw/v1/media/asset_1"' in text
    assert f'bytes="{len(BIG)}"' in text
    assert 'tool="vision_analyze"' in text, "要显式写出取回通道（read_image 读不了媒体库 URL）"


async def test_non_image_uses_file_analyzer_hint(monkeypatch):
    async def fake_persist(name: str, data: bytes, mime: str):
        return {"id": "a2", "file_url": "https://gw/m/a2"}

    monkeypatch.setattr("app.agent.media_offload._persist", fake_persist)

    url = _data_url(BIG, mime="application/pdf")
    out = await offload_inline_media([{"role": "user", "content": url}])
    assert 'tool="file_analyzer"' in out[0]["content"]


async def test_image_block_becomes_text_block(monkeypatch):
    """`image_url.url` 必须是合法 URL —— 把引用塞进去会让 provider 直接报错。"""

    async def fake_persist(name: str, data: bytes, mime: str):
        return {"id": "a3", "file_url": "https://gw/m/a3"}

    monkeypatch.setattr("app.agent.media_offload._persist", fake_persist)

    messages = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": _data_url(BIG)}}]}
    ]
    out = await offload_inline_media(messages)

    parts = out[0]["content"]
    assert parts[0]["type"] == "text"
    assert "<media_ref" in parts[0]["text"]


async def test_small_inline_block_untouched():
    url = _data_url(SMALL)
    messages = [{"role": "user", "content": f"x {url}"}]
    out = await offload_inline_media(messages)
    assert out[0] is messages[0]


async def test_offload_failure_writes_placeholder(monkeypatch):
    """卸载失败写占位符 —— 让历史看得出"这里曾有内容"，而不是静默消失。"""

    async def fake_persist(name: str, data: bytes, mime: str):
        return None

    monkeypatch.setattr("app.agent.media_offload._persist", fake_persist)

    url = _data_url(BIG)
    out = await offload_inline_media([{"role": "user", "content": url}])

    text = out[0]["content"]
    assert "<media_omitted" in text
    assert 'reason="offload_failed"' in text
    assert url not in text


async def test_invalid_base64_writes_placeholder(monkeypatch):
    monkeypatch.setattr("app.agent.media_offload._persist", _no_persist)

    # 长度 % 4 == 1 ⇒ b64decode 必定报错；字符本身合法，所以正则仍会匹配。
    # （min_bytes=1 是为了绕开"小块不卸载"的阈值，直击解码分支）
    payload = "A" * 197
    out = await offload_inline_media(
        [{"role": "user", "content": f"data:image/png;base64,{payload}"}], min_bytes=1
    )
    assert 'reason="invalid_base64"' in out[0]["content"]


async def test_too_large_writes_placeholder(monkeypatch):
    async def fake_persist(name: str, data: bytes, mime: str):  # pragma: no cover
        raise AssertionError("oversized payload must not be decoded/persisted")

    monkeypatch.setattr("app.agent.media_offload._persist", fake_persist)
    monkeypatch.setattr("app.agent.media_offload.MAX_INLINE_BYTES", 10)

    url = _data_url(SMALL)
    out = await offload_inline_media([{"role": "user", "content": url}], min_bytes=1)
    assert 'reason="too_large"' in out[0]["content"]


def test_attribute_escaping():
    """引用是**渲染进提示词的文本**，属性值必须转义（否则内容能撑破结构）。"""
    assert _attr('a"b<c>&') == "a&quot;b&lt;c&gt;&amp;"
