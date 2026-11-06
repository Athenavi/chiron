"""内联媒体载荷不该参与字符 token 估算。

**这个测试守的是一个实测过的量级错误**：修正前，一张 800×800 的 PNG（1.9MB）在字符口径下
被判成 640,912 token（真实约 850），于是任何带图请求都会在错误前提下触发压缩或被上游拒绝。
图片成本由 `image_tokens()` 按像素单独计，字符口径必须先把 base64 折掉 —— 两条估算路径
（`TokenEstimator._chars` 与 `compaction._estimate_tokens` 的无 model 兜底）都要折。
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app.agent.compaction import _estimate_tokens
from app.agent.model_window import MEDIA_PLACEHOLDER, estimator, image_tokens, strip_inline_media


def _png(size: int = 64) -> bytes:
    """确定性噪声图。

    **不能用纯色**：PNG 会把纯色压到几百字节，于是"小图/大图"的载荷长度差不到 10 倍，
    也让裸 base64 落不到 `_INLINE_PAYLOAD_MIN_CHARS` 之上 —— 测试会测不到它想测的分支。
    """
    import random

    pixels = random.Random(size).randbytes(size * size * 3)
    buf = io.BytesIO()
    Image.frombytes("RGB", (size, size), pixels).save(buf, format="PNG")
    return buf.getvalue()


def _data_url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def _msgs(url: str) -> list[dict[str, object]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    ]


def test_data_url_is_folded_to_placeholder() -> None:
    """OpenAI 形态：`{"type":"image_url","image_url":{"url":"data:…"}}`。"""
    folded = strip_inline_media(_msgs(_data_url(_png())))[0]["content"]  # type: ignore[index]
    assert folded[1]["image_url"]["url"] == MEDIA_PLACEHOLDER


def test_bare_base64_payload_is_folded() -> None:
    """Anthropic 形态：`{"type":"image","source":{"data":<裸 base64>}}` 没有前缀可认。"""
    payload = base64.b64encode(_png()).decode()
    folded = strip_inline_media({"type": "image", "source": {"type": "base64", "data": payload}})
    assert folded["source"]["data"] == MEDIA_PLACEHOLDER


def test_short_data_field_is_left_alone() -> None:
    """`data` 是常见字段名，短值不该被当成载荷折掉。"""
    assert strip_inline_media({"data": "ok"})["data"] == "ok"


def test_text_is_untouched() -> None:
    text = "普通的正文，没有媒体"
    assert strip_inline_media(text) == text


def test_estimate_is_independent_of_image_bytes() -> None:
    """同一张图，载荷变大不该让字符口径估算跟着涨。"""
    small = _msgs(_data_url(_png(32)))
    large = _msgs(_data_url(_png(256)))
    assert len(large[0]["content"][1]["image_url"]["url"]) > 10 * len(  # type: ignore[index]
        small[0]["content"][1]["image_url"]["url"]  # type: ignore[index]
    )
    assert _estimate_tokens(small) == _estimate_tokens(large)


def test_image_cost_still_counted_once_per_pixel() -> None:
    """折掉载荷不等于忽略图片：有 model 时必须把 image_tokens 加回来。"""
    msgs = _msgs(_data_url(_png(800)))
    chars_only = estimator().estimate(msgs, model="gpt-4o")
    total = _estimate_tokens(msgs, "gpt-4o")
    assert chars_only < 200, "base64 不该再进字符口径"
    assert total == chars_only + image_tokens(msgs)
    assert image_tokens(msgs) > 0, "800×800 的图应当有像素成本"


@pytest.mark.parametrize("chars", [0, 1, 5000])
def test_strip_keeps_message_shape(chars: int) -> None:
    """折叠只换载荷值，消息结构（角色、块顺序、其它字段）保持原样。"""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "x" * chars}]}]
    assert strip_inline_media(msgs) == msgs
