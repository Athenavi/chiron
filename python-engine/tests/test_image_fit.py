"""``app.media.image_fit`` 的裁决回归。

对齐 Reasonix 的 ``internal/model/visionimage``。这里钉住四件事：类型按**字节**判
（不看扩展名）、限内**原样返回**、最长边夹到 ``MAX_DIM``、以及两种拒绝
（像素炸弹 / 非支持格式）。前者是"扩展名骗人"的防线，后两者是"宁可拒绝也不
原样转发"——超限图片到了 provider 那里会变成 *unsupported format*，并连带毁掉
这个会话之后的每一轮。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.media import image_fit
from app.media.image_fit import ImageUnfit


def _encoded(fmt: str, size: tuple[int, int], color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


def _bomb_png(side: int = 8000) -> bytes:
    """64M 像素的 1-bit PNG：过本模块的 50M 上限，但低于 Pillow 自身的炸弹线。

    用 1-bit 是为了低内存（8 MB 位图），这样测的是**我们的**判断而不是 Pillow 的。
    """
    buf = io.BytesIO()
    Image.new("1", (side, side), 0).save(buf, format="PNG")
    return buf.getvalue()


def test_sniff_mime_reads_bytes_not_extension():
    assert image_fit.sniff_mime(_encoded("PNG", (4, 4))) == "image/png"
    assert image_fit.sniff_mime(_encoded("JPEG", (4, 4))) == "image/jpeg"
    assert image_fit.sniff_mime(_encoded("GIF", (4, 4))) == "image/gif"
    assert image_fit.sniff_mime(_encoded("WEBP", (4, 4))) == "image/webp"
    assert image_fit.sniff_mime(b"PK\x03\x04 definitely not an image") == ""


def test_fit_within_budget_is_untouched():
    raw = _encoded("PNG", (200, 120))
    data, mime = image_fit.fit(raw, "image/png")
    assert data is raw  # 限内**不重编码**
    assert mime == "image/png"


def test_fit_downscales_longest_side_and_keeps_aspect():
    raw = _encoded("PNG", (3000, 1000))
    data, mime = image_fit.fit(raw, "image/png")
    assert mime == "image/png"  # 无损格式仍走 PNG
    with Image.open(io.BytesIO(data)) as im:
        width, height = im.size
    assert width == image_fit.MAX_DIM
    assert height == 1000 * image_fit.MAX_DIM // 3000


def test_fit_reencodes_jpeg_as_jpeg():
    raw = _encoded("JPEG", (2000, 2000))
    data, mime = image_fit.fit(raw, "image/jpeg")
    assert mime == "image/jpeg"
    assert data != raw
    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) == image_fit.MAX_DIM


def test_fit_refuses_pixel_bomb():
    # 64M 像素：超过本模块 50M 的解码上限，但低于 Pillow 自身的炸弹线，
    # 所以这一条测的是**我们的**判断，不是 Pillow 的。
    raw = _bomb_png()
    with pytest.raises(ImageUnfit, match="decode limit"):
        image_fit.fit(raw, "image/png")


def test_fit_refuses_unsupported_and_garbage():
    with pytest.raises(ImageUnfit):
        image_fit.fit(b"\x00\x01", "image/tiff")  # 没有解码器
    with pytest.raises(ImageUnfit):
        image_fit.fit(b"not an image at all", "image/png")  # 解不开
