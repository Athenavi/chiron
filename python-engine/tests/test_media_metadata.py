"""媒体元数据提取 —— 回归保护。

历史缺陷：``extract_metadata`` 对四种类型一律返回
``{"note": "Basic metadata only, ... not implemented"}`` —— 那是一个**看起来成功、
其实什么都没做**的返回值（调用方无从分辨）。配套的 ``POST /v1/media/{id}/extract-metadata``
端点则直接 501。

本次实现真实解析，并锁死三条契约：
* 图片：返回真实尺寸/格式，EXIF 存在时一并返回；
* 文档：PDF 报页数、docx 报段落/表格数、纯文本报行数与字符数；
* **能力缺失时明确标记** ``available: False`` + 原因（当前是音视频，需 ffprobe），
  绝不退回"假装成功"。
"""

from __future__ import annotations

import io

import pytest

from app.media.analyzer import extract_metadata


def _png_bytes(width: int = 8, height: int = 6) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _pdf_bytes(pages: int = 2) -> bytes:
    import fitz

    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


async def test_image_returns_real_dimensions():
    out = await extract_metadata(
        "media://x", "image", data=_png_bytes(width=8, height=6)
    )

    assert out["success"] is True
    assert out["metadata"]["format"] == "PNG"
    assert out["metadata"]["width"] == 8
    assert out["metadata"]["height"] == 6


async def test_document_pdf_reports_pages():
    out = await extract_metadata(
        "https://x/a.pdf", "document", data=_pdf_bytes(pages=3)
    )

    assert out["success"] is True
    assert out["metadata"]["format"] == "pdf"
    assert out["metadata"]["pages"] == 3


async def test_document_plain_text_reports_lines_and_chars():
    payload = "第一行\n第二行\n第三行".encode()

    out = await extract_metadata("https://x/a.txt", "document", data=payload)

    assert out["success"] is True
    assert out["metadata"]["format"] == "text"
    assert out["metadata"]["lines"] == 3
    assert out["metadata"]["chars"] == len("第一行\n第二行\n第三行")


async def test_document_undecodable_is_reported_not_guessed():
    out = await extract_metadata("https://x/a.bin", "document", data=b"\xff\xfe\x00\x01")

    assert out["success"] is True
    assert out["metadata"]["format"] == "binary"
    assert out["metadata"]["bytes"] == 4


@pytest.mark.parametrize("media_type", ["video", "audio"])
async def test_av_reports_unavailable_instead_of_fake_success(media_type):
    """音视频需要 ffprobe（未安装）—— 必须明确报不可用，不能返回"成功"。"""
    out = await extract_metadata(f"media://x.{media_type}", media_type, data=b"\x00")

    assert out["success"] is False
    assert out["available"] is False
    assert "ffprobe" in out["reason"]


async def test_av_does_not_even_need_to_download():
    """能力缺失的判断发生在**下载之前** —— 不该先白下载一遍再报不可用。"""
    out = await extract_metadata("https://unreachable.invalid/v.mp4", "video")

    assert out["success"] is False
    assert out["available"] is False


async def test_unknown_type_reports_unavailable():
    out = await extract_metadata("media://x", "hologram", data=b"x")

    assert out["success"] is False
    assert out["available"] is False


async def test_corrupt_image_fails_loudly():
    """坏数据要报 error，而不是抛异常或假成功。"""
    out = await extract_metadata("media://x", "image", data=b"not-an-image")

    assert out["success"] is False
    assert "error" in out
