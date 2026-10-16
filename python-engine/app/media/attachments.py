"""把「媒体库选中的附件」变成模型可见内容 —— 两条链路共用**一处**判定。

附件形状（Go 网关透传）：``{id, name, mime_type, url, is_image}``。

两条链路产出的形状不同，但"附件是什么、能给出什么"由这里决定：

* **SSE 链路**（``/v1/agent/submit`` → AgentRuntime）：``to_content_parts()`` 产出 OpenAI
  兼容 content parts —— 图片走 ``image_fit``（尺寸/格式）并在模型读不了图时退化成文字
  提示（``model_capabilities`` 判定）；文档提取为文本块；其余给出取回提示。
* **统一链路**（``/v1/chat/submit`` → SubAgent，只吃字符串）：``to_text()`` 把同样的内容
  压成文本。

**为什么必须放在一处**：同一个文件在两个入口下的表现不能不同 —— 之前这两条链路各写一半
（`main._resolve_attachments` 只认 content 里的 Markdown 链接），于是"选了文件却用不上"
的观感取决于用户从哪个入口进。

取不回来 / 提取不出时一律**优雅退化**成一行说明：附件的问题不该让整轮对话失败。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 单个附件提取出的文本上限（与 `main._resolve_attachments` 同值，避免出现两套口径）。
MAX_ATTACHMENT_CHARS = 8000
#: 单个附件的下载上限：超过就不再下载，直接给提示（别为一个附件把内存拉满）。
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

#: 当文本处理的扩展名（与 `_resolve_attachments` 的历史清单一致）。
_TEXT_EXTS = frozenset({
    "txt", "md", "csv", "json", "xml", "yaml", "yml", "py", "js", "ts", "go",
    "java", "c", "cpp", "h", "rs", "sh", "bat", "ps1", "sql", "html", "css",
    "toml", "ini", "cfg", "conf", "log",
})


def _display_name(att: dict[str, Any]) -> str:
    return str(att.get("name") or att.get("id") or "附件")


def is_image_attachment(att: dict[str, Any]) -> bool:
    """是不是图片：看声明的 mime，其次看前端给的 `is_image`。"""
    if str(att.get("mime_type") or "").lower().startswith("image/"):
        return True
    return bool(att.get("is_image"))


async def _fetch_attachment(att: dict[str, Any]) -> tuple[bytes, str] | None:
    """按 URL 取回附件字节（走 SSRF 防护）；取不回返回 ``None``。"""
    url = str(att.get("url") or "")
    if not url:
        return None
    if url.startswith("data:"):
        return _decode_data_url(url)
    try:
        from app.config import settings
        from app.http_client import shared_client
        from app.tools.ssrf import fetch_url_safe

        async with shared_client(
            timeout=settings.http_timeout_web, follow_redirects=False
        ) as client:
            resp = await fetch_url_safe(client, url)
            if resp.status_code != 200:
                logger.info("attachment fetch: %s -> HTTP %s", url, resp.status_code)
                return None
            data = resp.content[: MAX_ATTACHMENT_BYTES + 1]
            if len(data) > MAX_ATTACHMENT_BYTES:
                logger.info("attachment fetch: %s exceeds %d bytes", url, MAX_ATTACHMENT_BYTES)
                return None
            declared = str(att.get("mime_type") or "")
            mime = declared or (resp.headers.get("content-type", "") or "").split(";")[0].strip()
            return data, mime
    except Exception as exc:  # noqa: BLE001 — 取不回只是"这个附件这次用不了"
        logger.info("attachment fetch failed (%s): %s", url, exc)
        return None


def _decode_data_url(url: str) -> tuple[bytes, str] | None:
    import base64

    header, _, payload = url.partition(",")
    if not payload:
        return None
    mime = header[5:].split(";")[0] if header.startswith("data:") else ""
    try:
        data = base64.b64decode(payload, validate=False)
    except Exception:  # noqa: BLE001
        return None
    if not data or len(data) > MAX_ATTACHMENT_BYTES:
        # 空内容也算取不回：坏 base64 在非严格模式下会被宽容解成空字节，
        # 若不放这里拦，它会以"提取不出"的形式出现，掩盖真正的原因。
        return None
    return data, mime


def _image_data_url(data: bytes, mime: str, name: str) -> str | None:
    """图片 → data URL（按**字节**判型、超限先降到 ``MAX_DIM`` 内）。不合法返回 ``None``。"""
    import base64

    from app.media.image_fit import ImageUnfit, fit, sniff_mime

    sniffed = sniff_mime(data) or mime
    try:
        data, mime = fit(data, sniffed)
    except ImageUnfit as exc:
        logger.info("attachment image %s unfit: %s", name, exc)
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


# ── 文本提取（docx / pdf / 纯文本）────────────────────────────────────────


def _pdf_text(data: bytes) -> str:
    try:
        import pymupdf

        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            return "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 — 解析失败按"提取不出"处理
        logger.info("attachment pdf extract failed: %s", exc)
        return ""


def _docx_text(data: bytes) -> str:
    """docx：按**段落 + 表格**取文本（docx 不是 Markdown 链接能覆盖的二进制格式）。"""
    try:
        import io

        import docx

        document = docx.Document(io.BytesIO(data))
        chunks = [p.text for p in document.paragraphs if p.text]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    chunks.append(" | ".join(cells))
        return "\n".join(chunks)
    except Exception as exc:  # noqa: BLE001
        logger.info("attachment docx extract failed: %s", exc)
        return ""


def _extract_text(data: bytes, mime: str, name: str) -> str:
    """按类型提取文本；提取不出时返回**提示**（不是乱码、也不是静默丢弃）。"""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    lowered = mime.lower()
    if lowered == "application/pdf" or ext == "pdf":
        text = _pdf_text(data)
    elif ext == "docx" or "wordprocessingml" in lowered:
        text = _docx_text(data)
    elif lowered.startswith("text/") or ext in _TEXT_EXTS:
        text = data.decode("utf-8", "replace")
    else:
        text = ""

    if not text.strip():
        return _unsupported_note(name, mime)
    snippet = text[:MAX_ATTACHMENT_CHARS]
    truncated = (
        f"\n... (已截断，仅显示前 {MAX_ATTACHMENT_CHARS} 字符)" if len(text) > MAX_ATTACHMENT_CHARS else ""
    )
    return f"===== 附件「{name}」内容 =====\n{snippet}{truncated}\n===== 附件结束 ====="


def _unsupported_note(name: str, mime: str) -> str:
    return (
        f"[附件「{name}」（{mime or '未知类型'}）无法直接提取文本。"
        "如需其内容，请用 file_analyzer 或相应工具按路径/URL 读取。]"
    )


def _unavailable_note(name: str) -> str:
    return f"[附件「{name}」本次未能取回（链接失效或不可达）；如需其内容请重新选择或提供文本。]"


def _visionless_note(name: str) -> str:
    return (
        f"[图片附件「{name}」未交给模型：当前模型不支持图片输入。"
        "如需理解图片内容，请调用 vision_analyze。]"
    )


# ── 两种产物 ────────────────────────────────────────────────────────────


async def to_content_parts(
    attachments: list[Any] | None, *, model: str = ""
) -> list[dict[str, Any]]:
    """附件 → OpenAI 兼容 content parts（图片块 + 文本块）。

    `model` 用于判定"这个模型能不能读图"；传空串时按可读处理（由调用方决定）。
    """
    if not attachments:
        return []
    from app.agent.model_capabilities import supports_vision

    vision = await supports_vision(model) if model else True
    parts: list[dict[str, Any]] = []
    for raw in attachments:
        if not isinstance(raw, dict):
            continue
        name = _display_name(raw)
        fetched = await _fetch_attachment(raw)
        if fetched is None:
            parts.append({"type": "text", "text": _unavailable_note(name)})
            continue
        data, mime = fetched
        if is_image_attachment(raw):
            if not vision:
                parts.append({"type": "text", "text": _visionless_note(name)})
                continue
            data_url = _image_data_url(data, mime, name)
            if data_url is None:
                parts.append({"type": "text", "text": _unavailable_note(name)})
                continue
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
            parts.append({"type": "text", "text": f"[图片附件：{name}]"})
            continue
        parts.append({"type": "text", "text": _extract_text(data, mime, name)})
    return parts


async def to_text(attachments: list[Any] | None) -> str:
    """附件 → 一段文本（给只吃字符串的执行路径，如统一链路的 SubAgent）。"""
    if not attachments:
        return ""
    blocks: list[str] = []
    for raw in attachments:
        if not isinstance(raw, dict):
            continue
        name = _display_name(raw)
        fetched = await _fetch_attachment(raw)
        if fetched is None:
            blocks.append(_unavailable_note(name))
            continue
        data, mime = fetched
        if is_image_attachment(raw):
            # 这条路径只能传文本：明确说出"图没交给模型"，并指出该用哪个工具。
            blocks.append(
                f"[图片附件：{name}]（本入口只能传文本，未交给模型；"
                "如需理解图片内容请调用 vision_analyze。）"
            )
            continue
        blocks.append(_extract_text(data, mime, name))
    return "\n\n".join(blocks)
