"""C2 段 c：内联媒体卸载 + 历史参数截断（方案 01 §3.2）。

两个动作，都在**压缩前**发生，目标是"让历史变小，但不丢内容"：

1. **媒体卸载**：把历史里的 `data:<mime>;base64,<…>` 内联块搬到媒体库
   （`app/tools/media.py` 的 `persist_to_library` → Go `media_assets` + 对象存储，
   天然带租户/用户归属），消息里只留 ``<media_ref id="…" path="…" />``。
   **卸载失败写占位符**（``<media_omitted … />``）而不是静默丢弃 —— 让历史看得出
   "这里曾有内容"，也免得模型以为"当时就没给图"。
2. **参数截断**：历史 `tool_calls[].arguments` 超过 ``CompactionConfig.arg_max_chars`` 时
   保留 **JSON 骨架**（键结构不动）、把长字符串值截断并标注 ``...(argument truncated)``；
   **绝不改 `id` / `name`** —— 工具配对完整性是 API 契约（改坏就是 400）。

**异步与同步的分界**：落媒体库要过 HTTP ⇒ 卸载是 async；截断是纯字符串处理 ⇒ 同步。
所以调用点分两处（见 `runtime._compact_messages` 与回合前的卸载）。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from typing import Any

logger = logging.getLogger(__name__)

#: 内联 data URL（base64 段至少 64 字符才值得卸载 —— 短的直接留着更划算）
DATA_URL_RE = re.compile(
    r"data:(?P<mime>[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9.+-]+);base64,(?P<payload>[A-Za-z0-9+/=]{64,})"
)

#: 小于该体积的内联块不卸载（卸载有落库 + 往返成本）
DEFAULT_MIN_BYTES = 8 * 1024
#: 单个内联块的解码上限：挡住"解码一个 200MB 的 data URL"这种内存放大
MAX_INLINE_BYTES = 8 * 1024 * 1024
#: 参数截断的标注（与方案 §3.2 的措辞一致，便于日志/测试检索）
TRUNCATION_MARK = "...(argument truncated)"

_store_cache: Any = None


def _media_store() -> Any:
    """本地媒体 store 的**惰性单例**（`create_store()` 每次都会重建索引，别每块调一次）。"""
    global _store_cache
    if _store_cache is None:
        from app.media.store import create_store

        _store_cache = create_store()
    return _store_cache


def _attr(value: str) -> str:
    """XML 属性值的最小转义 —— 引用是**渲染进提示词的文本**，不能被内容撑破结构。"""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _ext_for(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "application/pdf": ".pdf",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "text/plain": ".txt",
    }.get(mime, ".bin")


async def _persist(name: str, data: bytes, mime: str) -> dict[str, Any] | None:
    """落库：优先媒体库（带租户归属，能在「媒体库」页面看到），失败回退引擎本地 store。

    两条都失败 → 返回 None，由调用方写占位符。**不做第三条兜底**：静默保留 base64
    等于"卸载没发生"，而调用方（压缩）正因为体积才走这条路。
    """
    asset_type = "image" if mime.startswith("image/") else "file"
    try:
        from app.tools.media import persist_to_library

        stored, reason = await persist_to_library(
            name,
            data,
            asset_type=asset_type,
            category="inline-history",
            mime_type=mime,
            tags=["inline-history"],
        )
        if stored:
            return stored
        # 未进媒体库（网关不可达 / 拒绝）：回退本地并把原因记下来，
        # 免得"卸载发生了但资产在媒体库里找不到"没有任何线索。
        logger.info("media offload fell back to engine-local store: %s", reason)
    except Exception as exc:  # noqa: BLE001 — 落库失败不阻断压缩
        logger.warning("media offload via gateway failed: %s", exc)

    try:
        asset = _media_store().write(
            name,
            data,
            asset_type=asset_type,
            category="inline-history",
            tags=["inline-history"],
            fmt=mime,
        )
        return {"id": asset.id, "file_url": asset.file_url}
    except Exception as exc:  # noqa: BLE001
        logger.warning("media offload local fallback failed: %s", exc)
        return None


async def _offload_one(mime: str, payload: str, approx_bytes: int) -> str:
    """把单个内联块换成引用（或占位符）—— 返回**替换文本**。"""
    if approx_bytes > MAX_INLINE_BYTES:
        return (
            f'<media_omitted reason="too_large" mime="{_attr(mime)}" '
            f'bytes="{approx_bytes}" />'
        )
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception:  # noqa: BLE001 — 坏 base64：留下痕迹，不猜内容
        return (
            f'<media_omitted reason="invalid_base64" mime="{_attr(mime)}" '
            f'bytes="{approx_bytes}" />'
        )

    name = f"inline-{uuid.uuid4().hex[:8]}{_ext_for(mime)}"
    stored = await _persist(name, data, mime)
    if not stored:
        return (
            f'<media_omitted reason="offload_failed" mime="{_attr(mime)}" '
            f'bytes="{len(data)}" />'
        )
    asset_id = str(stored.get("id") or stored.get("asset_id") or "")
    url = str(stored.get("file_url") or stored.get("url") or "")
    # 取回通道：`read_image` 只读沙箱工作区，读不了媒体库的 URL —— 因此显式写出该用哪个
    # 工具（两者都按 URL 取，且都过 SSRF 防护）。不给提示的话，模型很可能去 `read_file`
    # 这个 path，然后失败。
    hint = "vision_analyze" if mime.startswith("image/") else "file_analyzer"
    return (
        f'<media_ref id="{_attr(asset_id)}" path="{_attr(url)}" '
        f'mime="{_attr(mime)}" bytes="{len(data)}" tool="{hint}" />'
    )


async def _rewrite_text(text: str, *, min_bytes: int) -> str:
    """把文本里的（够大的）内联 data URL 逐个换成引用。"""
    if "data:" not in text:
        return text
    pieces: list[str] = []
    cursor = 0
    hit = False
    for match in DATA_URL_RE.finditer(text):
        approx = len(match.group("payload")) * 3 // 4
        if approx < min_bytes:
            continue
        pieces.append(text[cursor : match.start()])
        pieces.append(await _offload_one(match.group("mime"), match.group("payload"), approx))
        cursor = match.end()
        hit = True
    if not hit:
        return text
    pieces.append(text[cursor:])
    return "".join(pieces)


async def _rewrite_content(content: Any, *, min_bytes: int) -> Any:
    if isinstance(content, str):
        return await _rewrite_text(content, min_bytes=min_bytes)
    if not isinstance(content, list):
        return content

    out: list[Any] = []
    for part in content:
        if not isinstance(part, dict):
            out.append(part)
            continue

        text = part.get("text")
        if isinstance(text, str):
            new_text = await _rewrite_text(text, min_bytes=min_bytes)
            out.append({**part, "text": new_text} if new_text != text else part)
            continue

        image = part.get("image_url")
        url = image.get("url") if isinstance(image, dict) else None
        if isinstance(url, str) and url.startswith("data:"):
            approx = len(url) * 3 // 4
            if approx >= min_bytes:
                # 卸载后**换成 text 块**：`image_url.url` 必须是合法 URL，
                # 把 `<media_ref>` 塞进去会让 provider 直接报错。
                out.append(
                    {
                        "type": "text",
                        "text": await _rewrite_text(url, min_bytes=min_bytes),
                    }
                )
                continue
        out.append(part)
    return out


async def offload_inline_media(
    messages: list[dict[str, Any]], *, min_bytes: int = DEFAULT_MIN_BYTES
) -> list[dict[str, Any]]:
    """把消息正文里的内联 `data:` 块卸载到媒体库，替换为 ``<media_ref>``。

    只处理**超过 `min_bytes`** 的块：小内联块留着更划算（卸载它们只是制造无谓的媒体资产）。
    任何一步失败都落到 ``<media_omitted>`` 占位符 —— **绝不静默丢弃**。
    """
    out: list[dict[str, Any]] = []
    changed = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            if "data:" not in content:
                out.append(msg)
                continue
        elif isinstance(content, list):
            if not any(isinstance(part, dict) for part in content):
                out.append(msg)
                continue
        else:
            out.append(msg)
            continue

        new_content = await _rewrite_content(content, min_bytes=min_bytes)
        if new_content != content:
            changed += 1
            out.append({**msg, "content": new_content})
        else:
            out.append(msg)
    if changed:
        logger.info("C2-c: offloaded inline media in %d message(s)", changed)
    return out


# ── 历史参数截断（同步） ────────────────────────────────────────────────


def _trim_values(value: Any, *, budget: int) -> Any:
    """递归截断**字符串值**，保持 JSON 骨架（键结构）不变。"""
    if isinstance(value, str):
        return value[:budget] + TRUNCATION_MARK if len(value) > budget else value
    if isinstance(value, list):
        return [_trim_values(item, budget=budget) for item in value]
    if isinstance(value, dict):
        return {key: _trim_values(item, budget=budget) for key, item in value.items()}
    return value


def _skeleton(raw: str, limit: int) -> str:
    """把超长 arguments 压到 limit 以内，并**标注**截断（不假装是原文）。"""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # 不是 JSON：只能文本截断（仍然标注，避免被当成完整参数）
        keep = max(0, limit - len(TRUNCATION_MARK))
        return raw[:keep] + TRUNCATION_MARK

    text = json.dumps(_trim_values(parsed, budget=max(64, limit // 2)), ensure_ascii=False)
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(TRUNCATION_MARK))
    return text[:keep] + TRUNCATION_MARK


def truncate_tool_call_arguments(
    messages: list[dict[str, Any]], cfg: Any
) -> list[dict[str, Any]]:
    """截断历史 `assistant.tool_calls[].arguments`（C2 段 c）。

    `cfg.arg_max_chars` ≤ 0 表示不截断。**只改 `arguments` 内容** —— `id` / `name`
    一个字节都不动，因此 assistant(tool_calls) 与 tool 结果的配对始终完整（改坏就是 API 400）。
    """
    limit = int(getattr(cfg, "arg_max_chars", 0) or 0)
    if limit <= 0:
        return messages

    out: list[dict[str, Any]] = []
    changed = 0
    for msg in messages:
        calls = msg.get("tool_calls")
        if msg.get("role") != "assistant" or not isinstance(calls, list) or not calls:
            out.append(msg)
            continue
        new_calls: list[Any] = []
        truncated_here = False
        for call in calls:
            args = call.get("arguments") if isinstance(call, dict) else None
            if isinstance(args, str) and len(args) > limit:
                new_calls.append({**call, "arguments": _skeleton(args, limit)})
                changed += 1
                truncated_here = True
            else:
                new_calls.append(call)
        out.append({**msg, "tool_calls": new_calls} if truncated_here else msg)
    if changed:
        logger.info("C2-c: truncated arguments of %d tool call(s) (limit=%d)", changed, limit)
    return out
