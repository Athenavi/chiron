"""AI驱动的媒体内容分析器"""

import logging
import os
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


async def analyze_image(
    image_url: str, prompt: str = None, gateway: Any = None
) -> dict[str, Any]:
    """分析图片内容

    Args:
        image_url: 图片URL
        prompt: 自定义分析提示词
        gateway: GatewayRouter —— 由调用方**显式注入**（本模块不反向依赖 app.tools）。

    Returns:
        分析结果字典

    历史缺陷：这里曾 `from app.llm.client import get_llm_client` —— 该函数在仓库里
    **从未存在**（app/llm/client.py 只有 LLMClient，且它只提供 embed()，没有 chat()）。
    于是本函数必然抛 ImportError 并被下面的 except 吞成 {"success": False}，
    多模态分析从来没有真正工作过。现改为显式注入 gateway。
    """
    if not prompt:
        prompt = """请详细分析这张图片的内容，包括：
1. 主要对象和场景描述
2. 颜色、构图和风格
3. 可能的用途或含义
4. 任何文字内容（如有）

请用中文回答。"""

    if gateway is None:
        return {
            "success": False,
            "error": "no LLM gateway available: pass gateway=... (from app.tools.context.get_gateway)",
            "analyzed_at": datetime.now(UTC).isoformat(),
        }

    try:
        # 构建多模态消息
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ]

        # 从环境变量获取模型名，默认使用 gpt-4o（主线模型）
        model_name = os.getenv("VISION_MODEL", "gpt-4o")
        response = await gateway.chat(messages, model=model_name)
        return {
            "success": True,
            "analysis": (
                response.content if hasattr(response, "content") else str(response)
            ),
            "model": getattr(response, "model", "unknown"),
            "analyzed_at": datetime.now(UTC).isoformat(),
        }
    except Exception as e:
        logger.error(f"Image analysis failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "analyzed_at": datetime.now(UTC).isoformat(),
        }


#: 需要外部二进制才能解析的类型（本仓库未声明该依赖）。
_UNSUPPORTED_MEDIA_TYPES = {
    "video": "video metadata requires ffprobe (not installed); "
    "install ffmpeg/ffprobe and wire it here",
    "audio": "audio metadata requires ffprobe (not installed); "
    "install ffmpeg/ffprobe and wire it here",
}

#: 单次解析读取的最大字节数，避免一个超大文件把内存吃满。
_MAX_METADATA_BYTES = 32 * 1024 * 1024


async def _download(url: str) -> bytes:
    """下载媒体内容（超时与大小上限见上）。"""
    import httpx

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def _image_metadata(data: bytes) -> dict[str, Any]:
    """图片元数据：格式 / 尺寸 / 模式 + EXIF（若存在）。"""
    import io

    from PIL import ExifTags, Image

    with Image.open(io.BytesIO(data)) as img:
        info: dict[str, Any] = {
            "format": img.format,
            "width": img.width,
            "height": img.height,
            "mode": img.mode,
        }
        raw_exif = getattr(img, "getexif", lambda: None)()
        if raw_exif:
            # ExifTags.TAGS 把数字 tag 映射成可读名；未知 tag 保留数字以免丢失信息。
            exif = {}
            for tag_id, value in raw_exif.items():
                name = ExifTags.TAGS.get(tag_id, str(tag_id))
                # 二进制/不可序列化的值统一转成字符串，保证 JSON 可编码。
                exif[name] = value if isinstance(value, (str, int, float)) else str(value)
            info["exif"] = exif
        return info


def _document_metadata(data: bytes, media_url: str) -> dict[str, Any]:
    """文档元数据：PDF（pymupdf）/ docx（python-docx）/ 纯文本。"""
    lowered = media_url.lower()
    if lowered.endswith(".pdf") or data[:4] == b"%PDF":
        import fitz  # pymupdf

        with fitz.open(stream=data, filetype="pdf") as doc:
            return {
                "format": "pdf",
                "pages": doc.page_count,
                "doc_metadata": {k: str(v) for k, v in (doc.metadata or {}).items() if v},
            }
    if lowered.endswith(".docx") or data[:2] == b"PK":
        import io

        import docx

        d = docx.Document(io.BytesIO(data))
        return {
            "format": "docx",
            "paragraphs": len(d.paragraphs),
            "tables": len(d.tables),
        }
    # 兜底当纯文本（解码失败则只报字节数，不猜编码）
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"format": "binary", "bytes": len(data)}
    return {
        "format": "text",
        "lines": text.count("\n") + 1 if text else 0,
        "chars": len(text),
    }


async def extract_metadata(
    media_url: str, media_type: str, *, data: bytes | None = None
) -> dict[str, Any]:
    """提取媒体元数据（**真实解析**，不再是"not implemented"占位说明）。

    Args:
        media_url: 媒体 URL（用于回显；未提供 ``data`` 时也从这里下载）
        media_type: ``image`` / ``video`` / ``audio`` / ``document``
        data: 可选的文件字节。测试与"内容已到手"的调用方直接传它，免去一次网络往返。

    Returns:
        元数据字典。约定：

        * 成功解析 → ``{"success": True, "metadata": {...}}``
        * **能力缺失**（当前只有音视频，需 ffprobe）→ ``{"success": False,
          "available": False, "reason": ...}``

        刻意不再返回 ``note: "Basic metadata only, ... not implemented"`` ——
        那种"看起来成功、其实什么都没做"的返回值会被上层当成一次真实提取，
        与 `analyze_image` 里被吞掉的 ImportError 是同一类问题。
    """
    base = {
        "url": media_url,
        "type": media_type,
        "extracted_at": datetime.now(UTC).isoformat(),
    }

    if media_type in _UNSUPPORTED_MEDIA_TYPES:
        return {
            **base,
            "success": False,
            "available": False,
            "reason": _UNSUPPORTED_MEDIA_TYPES[media_type],
        }

    try:
        raw = data if data is not None else await _download(media_url)
        if len(raw) > _MAX_METADATA_BYTES:
            return {
                **base,
                "success": False,
                "error": f"file too large for metadata extraction ({len(raw)} bytes)",
            }

        if media_type == "image":
            detail = _image_metadata(raw)
        elif media_type == "document":
            detail = _document_metadata(raw, media_url)
        else:
            return {
                **base,
                "success": False,
                "available": False,
                "reason": f"unknown media_type: {media_type!r}",
            }

        return {**base, "success": True, "metadata": detail}
    except Exception as e:
        logger.error(f"Metadata extraction failed: {e}")
        return {**base, "success": False, "error": str(e)}
