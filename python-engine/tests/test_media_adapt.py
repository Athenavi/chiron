"""能力适配的回归：**图片 token 计入预算** + **非 vision 模型不塞图**。

两条都来自 Reasonix 的做法（`requestImageTokens` / 图片只在能读图的模型上出现），
目的都是同一个：别让一屏图把窗口算漏，也别把图塞给一个会整轮报错的模型。
"""

from __future__ import annotations

import base64
import io

from PIL import Image

from app.agent import model_capabilities
from app.agent.media_offload import adapt_images_for_model
from app.agent.model_window import image_tokens
from app.media.image_fit import MAX_DIM

_TOKENS_DIVISOR = 750


def _data_url(size: tuple[int, int] = (64, 64)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, (1, 2, 3)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _messages_with_image(url: str = "") -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看看这张图"},
                {"type": "image_url", "image_url": {"url": url or _data_url()}},
            ],
        }
    ]


def test_image_tokens_prices_a_data_url_by_its_own_size():
    # 64×64 = 4096 像素 → 向上取整 4096/750 = 6
    assert image_tokens(_messages_with_image()) == (64 * 64 + _TOKENS_DIVISOR - 1) // _TOKENS_DIVISOR


def test_image_tokens_prices_an_unreadable_url_at_max_dim():
    """网络 URL 读不到尺寸 ⇒ 按 MAX_DIM 计满（估高让压缩早触发，而不是溢出）。"""
    msgs = _messages_with_image("https://example.com/a.png")
    assert image_tokens(msgs) == (MAX_DIM * MAX_DIM + _TOKENS_DIVISOR - 1) // _TOKENS_DIVISOR


async def test_supports_vision_is_optimistic_then_corrected_by_refusal():
    """默认乐观（保持既有行为），被 provider 拒绝一次后就地纠正。"""
    model_capabilities.clear_cache()
    assert await model_capabilities.supports_vision("some-model") is True

    model_capabilities.record_vision_refusal("some-model")
    assert await model_capabilities.supports_vision("some-model") is False


async def test_adapt_replaces_images_for_visionless_model():
    model_capabilities.clear_cache()
    model_capabilities.remember_supports_vision("no-vision", False)

    out = await adapt_images_for_model(_messages_with_image(), model="no-vision")

    parts = out[0]["content"]
    assert all("image_url" not in part for part in parts)
    assert any("vision_analyze" in part.get("text", "") for part in parts)


async def test_adapt_keeps_images_for_capable_model():
    model_capabilities.clear_cache()
    model_capabilities.remember_supports_vision("reads-images", True)

    messages = _messages_with_image()
    out = await adapt_images_for_model(messages, model="reads-images")

    assert out is messages  # 原样返回，不复制
