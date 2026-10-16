"""`app.media.attachments` 的回归：附件 → 模型可见内容。

两种产物各测一次（`to_content_parts` 给 SSE 链路、`to_text` 给统一链路），覆盖：
图片（模型能读图 / 不能读图）、docx、纯文本、以及取不回的 URL。全部走 **data URL**，
所以测试不触网。
"""

from __future__ import annotations

import base64
import io

from app.agent import model_capabilities
from app.media.attachments import to_content_parts, to_text

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _png(size: tuple[int, int] = (8, 8)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (9, 9, 9)).save(buf, format="PNG")
    return buf.getvalue()


def _docx(text: str) -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _att(name: str, mime: str, data: bytes = b"", *, url: str = "", is_image: bool = False) -> dict:
    return {
        "id": name,
        "name": name,
        "mime_type": mime,
        "url": url or _data_url(data, mime),
        "is_image": is_image,
    }


async def test_content_parts_gives_image_block_for_capable_model():
    model_capabilities.clear_cache()
    model_capabilities.remember_supports_vision("reader", True)

    parts = await to_content_parts(
        [_att("a.png", "image/png", _png(), is_image=True)], model="reader"
    )

    assert [p["type"] for p in parts] == ["image_url", "text"]
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_content_parts_degrades_image_for_visionless_model():
    model_capabilities.clear_cache()
    model_capabilities.remember_supports_vision("blind", False)

    parts = await to_content_parts(
        [_att("a.png", "image/png", _png(), is_image=True)], model="blind"
    )

    assert all(p["type"] == "text" for p in parts)
    assert "vision_analyze" in parts[0]["text"]


async def test_content_parts_extracts_docx_text():
    parts = await to_content_parts([_att("报告.docx", _DOCX_MIME, _docx("这是附件正文"))])

    assert len(parts) == 1
    assert "这是附件正文" in parts[0]["text"]
    assert "附件「报告.docx」" in parts[0]["text"]


async def test_content_parts_reads_plain_text():
    parts = await to_content_parts([_att("notes.txt", "text/plain", "hello\nworld".encode())])

    assert len(parts) == 1
    assert "hello" in parts[0]["text"]


async def test_content_parts_notes_an_unreachable_attachment():
    # 坏 base64 → 取不回：给一行说明，而不是抛错或静默丢弃
    att = _att("x.pdf", "application/pdf", url="data:application/pdf;base64,!!!")
    parts = await to_content_parts([att])

    assert len(parts) == 1
    assert "未能取回" in parts[0]["text"]


async def test_to_text_flags_images_and_extracts_docx():
    out = await to_text([
        _att("pic.png", "image/png", _png(), is_image=True),
        _att("d.docx", _DOCX_MIME, _docx("统一链路正文")),
    ])

    assert "vision_analyze" in out          # 这条路径只能传文本，必须说清
    assert "统一链路正文" in out            # 文档照常提取
