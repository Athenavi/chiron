"""Media tools 注册到本地工具注册表。

实现对标 Go `internal/tools/media.go` 注册的两个工具：

- ``media_create``：创建**可下载的资产** —— 文本（Markdown/CSV/JSON/代码…），
  或经 base64 的二进制（docx/xlsx/pdf/png…）。这是 agent 产出"用户能打开的文件"的正道。
- ``image_generate``：生成图片（未配置 ``IMAGE_GEN_API_URL`` 时**明确报错**，不伪造成功）。

落库路径与**用户直传**完全相同：``persist_to_library`` → 网关
``POST /v1/internal/media/assets`` → 对象存储 + ``media_assets`` 表，因此产物会出现在
「媒体库」页面（``GET /v1/media``）并可下载（``GET /v1/media/{id}/download``）。

网关不可达或拒绝时回退引擎本地 store —— 这是刻意的（引擎要能独立运行），但
**必须把原因带出去**：``media_create`` 的返回里带 ``stored_in`` 与 ``warning``，
否则"工具说创建成功、用户在媒体库里找不到"就成了没有信号的黑洞。
"""

from __future__ import annotations

import base64
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from app.media.store import create_store
from app.tools.context import get_gateway, get_tenant_id, get_user_id
from app.tools.registry import registry
from app.tools.ssrf import assert_safe_url, fetch_url_safe

logger = logging.getLogger(__name__)
_store = create_store()


#: 单个资产的体积上限。**必须与网关内部端点的上限一致**（见
#: `internal/api/media_internal.go` 的 `maxInternalAssetBytes`）。这里做前置校验，是为了
#: 给出**可操作**的错误，而不是让请求跑到网关再被拒、再回退本地。
MAX_ASSET_BYTES = 32 * 1024 * 1024

#: 落库列的硬长度（`media_assets` 表）：超了 INSERT 会**直接失败**，而工具此前会静默回退
#: 本地 store ⇒ 用户看到"创建成功"、媒体库里却没有。这里逐一按列上限前置校验。
#: name varchar(255) / tags varchar(255) / type varchar(16) / category varchar(64)。
MAX_NAME_CHARS = 200
MAX_TAGS_TOTAL_CHARS = 200
MAX_TAG_CHARS = 48
MAX_CATEGORY_CHARS = 48

#: 资产类型取值 —— 与前端媒体库的筛选 tab 一一对应
#: （frontend-vue/src/views/MediaView.vue：image / document / video / audio / file / text）。
ASSET_TYPES: tuple[str, ...] = ("image", "video", "audio", "document", "file", "text")

#: 按扩展名推断资产类型。**注意 `.py` / `.sh` / `.html` 归到 text 是类型层面的事实**，
#: 不代表它们能落进媒体库 —— 网关的 `isExecutableMIME` 对 text/plain 会按扩展名拒绝
#: 脚本类文件（那是用户上传路径既有的安全策略，agent 走同一条落库路径，因此同样适用）。
#: 这里刻意**不**复制那份拒绝名单：两份名单必然漂移，单一事实源留在 Go 侧。
_EXT_TO_TYPE: dict[str, str] = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".bmp": "image", ".svg": "image", ".ico": "image", ".tif": "image", ".tiff": "image",
    ".mp4": "video", ".mov": "video", ".webm": "video", ".avi": "video", ".mkv": "video",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".flac": "audio", ".ogg": "audio",
    ".aac": "audio",
    ".pdf": "document", ".doc": "document", ".docx": "document", ".xls": "document",
    ".xlsx": "document", ".ppt": "document", ".pptx": "document", ".odt": "document",
    ".ods": "document", ".odp": "document", ".rtf": "document", ".epub": "document",
    ".txt": "text", ".md": "text", ".markdown": "text", ".csv": "text", ".tsv": "text",
    ".json": "text", ".yaml": "text", ".yml": "text", ".xml": "text", ".log": "text",
    ".html": "text", ".htm": "text", ".css": "text", ".sql": "text", ".toml": "text",
    ".ini": "text", ".conf": "text", ".env": "text",
    ".py": "text", ".js": "text", ".ts": "text", ".jsx": "text", ".tsx": "text",
    ".vue": "text", ".go": "text", ".java": "text", ".c": "text", ".h": "text",
    ".cpp": "text", ".rs": "text", ".rb": "text", ".php": "text", ".sh": "text",
}

#: 按扩展名推断 MIME（用于让网关的 magic-bytes 校验有机会采用"声明的"值）。
_EXT_TO_MIME: dict[str, str] = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".csv": "text/csv", ".json": "application/json", ".md": "text/markdown",
    ".txt": "text/plain", ".html": "text/html", ".xml": "application/xml",
}


def _infer_mime(name: str, *, binary: bool) -> str:
    """按扩展名猜 MIME；猜不到时按"是不是二进制"给一个保守值。"""
    ext = os.path.splitext(name or "")[1].lower()
    if ext in _EXT_TO_MIME:
        return _EXT_TO_MIME[ext]
    return "application/octet-stream" if binary else "text/plain"


def _infer_asset_type(mime: str, name: str) -> str:
    """推断资产类型：先看 MIME，再看扩展名 —— 与网关 `detectType` 同一取向。

    为什么必须推断而不是沿用旧的默认 `"text"`：前端媒体库按 type 分 tab，
    一份 .docx 被标成 text 就等于"在『文档』里找不到"。
    """
    m = (mime or "").lower()
    for prefix, kind in (("image/", "image"), ("video/", "video"), ("audio/", "audio")):
        if m.startswith(prefix):
            return kind
    if "pdf" in m or "document" in m or "officedocument" in m or "oasis" in m:
        return "document"
    ext = os.path.splitext(name or "")[1].lower()
    if ext in _EXT_TO_TYPE:
        return _EXT_TO_TYPE[ext]
    if m.startswith("text/") or "json" in m or "xml" in m:
        return "text"
    return "file" if m else "text"

# 文件下载大小限制：100MB
MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024


def _gateway_error_text(resp: Any) -> str:
    """从网关的错误响应里取**可操作**的文本（网关的文案本来就写明了原因）。"""
    try:
        body = resp.json()
        if isinstance(body, dict):
            for key in ("error", "message", "detail"):
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:200]
    except Exception:  # noqa: BLE001 - 非 JSON 响应直接退回文本
        pass
    return str(getattr(resp, "text", "") or "").strip()[:200]


async def persist_to_library(
    name: str,
    data: bytes,
    *,
    asset_type: str,
    category: str = "generated",
    mime_type: str = "",
    tags: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """把资产写入 Go 侧媒体库（media_assets + 对象存储）。

    这是 agent 产物进入「媒体库」页面的唯一通道：工具执行上下文携带
    tenant_id/user_id（见 app/tools/context.py），据此以内部 token 调用网关的
    ``POST /v1/internal/media/assets``。**与用户直传（POST /v1/media/upload）落同一张表、
    同一套对象键布局**，因此产物在媒体库里可列出、可下载。

    返回 ``(asset, failure_reason)``：成功时 reason 为空串；失败时 asset 为 ``None``，
    reason 说明**为什么**。

    为什么把失败原因带出来：此前失败一律静默回退本地 store，于是"资产在媒体库里看不到"
    这件事**没有任何可见信号** —— 工具照样报"创建成功"，日志里只有一行 warning。
    调用方现在必须把 reason 转达给模型与用户（见 `media_create`）。
    """
    from app.config import settings

    tenant_id = get_tenant_id()
    user_id = get_user_id()
    if not (settings.internal_token and tenant_id and user_id):
        return None, (
            "media library unreachable from this tool context "
            f"(internal_token={'set' if settings.internal_token else 'missing'}, "
            f"tenant_id={'set' if tenant_id else 'missing'}, "
            f"user_id={'set' if user_id else 'missing'})"
        )

    payload: dict[str, Any] = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "name": name,
        "type": asset_type,
        "category": category,
        "mime_type": mime_type,
        "tags": tags or [],
    }
    try:
        payload["content"] = data.decode("utf-8")
    except UnicodeDecodeError:
        payload["content_base64"] = base64.b64encode(data).decode("ascii")

    url = f"{settings.gateway_internal_url.rstrip('/')}/v1/internal/media/assets"
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout_long) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"X-Internal-Token": settings.internal_token},
            )
    except Exception as exc:  # noqa: BLE001 — 网络/超时：如实回退，但把原因带出去
        logger.warning("persist media asset to gateway failed: %s", exc)
        return None, f"media library call failed: {str(exc)[:200]}"

    if resp.status_code >= 400:
        # 不再 raise_for_status 后吞掉响应体：网关的文案是**可操作的**
        # （"file type not allowed: …" / "asset too large: …"），必须原样带给模型。
        reason = _gateway_error_text(resp)
        logger.warning("media library rejected asset (HTTP %d): %s", resp.status_code, reason)
        return None, f"media library rejected the asset (HTTP {resp.status_code}): {reason}"

    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 - 落库成功但响应不可解析：仍算失败（无法确认归属）
        return None, f"media library returned an unreadable response: {str(exc)[:120]}"
    asset = body.get("data", body) if isinstance(body, dict) else None
    if not isinstance(asset, dict):
        return None, "media library returned an unexpected payload"
    return asset, ""


def _decode_base64(raw: str) -> bytes:
    """宽容解析模型生成的 base64。

    模型输出的 base64 常见三种形态：分块粘贴（带换行/空格）、URL-safe 字母表、
    省略 padding。逐字严格校验会把它们全判成"非法 base64"。
    """
    compact = "".join(raw.split())
    if not compact:
        raise ValueError("content_base64 is empty")
    try:
        return base64.b64decode(compact, validate=True)
    except Exception:
        padded = compact.rstrip("=")
        return base64.urlsafe_b64decode(padded + "=" * (-len(padded) % 4))


def _normalize_tags(tags: list[str] | None) -> list[str]:
    """按落库列的长度上限收紧 tags（`media_assets.tags varchar(255)`，逗号拼接）。

    超长会让 INSERT 直接失败 —— 而失败会退化成"静默回退本地 store"，
    看起来像成功。所以这里主动截断（截断比报错更合适：tags 只是元数据）。
    """
    out: list[str] = []
    total = 0
    for raw in tags or []:
        tag = str(raw).strip()[:MAX_TAG_CHARS]
        if not tag or tag in out:
            continue
        extra = len(tag) + (1 if out else 0)  # 分隔逗号
        if total + extra > MAX_TAGS_TOTAL_CHARS:
            break
        out.append(tag)
        total += extra
    return out


def _sanitize_filename(prompt: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9 _\-]", "", prompt).strip()
    name = re.sub(r"\s+", "_", name)
    return name[:48] or "image"


# ── media_create ──────────────────────────────────────────────


def _read_workspace_asset(rel: str) -> tuple[bytes, str | None]:
    """把工作区里的一个文件读成字节。返回 `(数据, 错误原因)`（错误时数据为空）。

    三条纪律：

    * 路径必须过 `sandbox.safe_join` —— clamp 到**该用户自己的工作区**，`../` 与绝对路径
      一律拒绝。没有这一步，这个参数就是一条新的任意文件读通道；
    * **先 `stat()` 再读**：上限必须在**消费之前**生效（读完再判大小 = 把一个不受控的文件
      整份读进内存。§4 的同族教训："有上限"不等于"在消费前拦住"）；
    * 不存在 / 是目录 ⇒ **明确说清怎么办**（先生成到工作区），不静默回退到别处。
    """
    from app.tools.sandbox import safe_join

    try:
        target = safe_join(rel)
    except ValueError as exc:
        return b"", f"path is outside the sandbox workspace: {exc}"
    if not target.exists():
        return b"", (
            f"no such file in the workspace: {rel!r} — generate it first (e.g. with "
            "execute_python) and pass its path relative to the workspace"
        )
    if target.is_dir():
        return b"", f"{rel!r} is a directory; pass a single file"
    try:
        size = target.stat().st_size
    except OSError as exc:
        return b"", f"cannot stat {rel!r}: {exc}"
    if size > MAX_ASSET_BYTES:
        return b"", (
            f"asset too large: {size} bytes "
            f"(max {MAX_ASSET_BYTES // (1024 * 1024)} MB)"
        )
    try:
        return target.read_bytes(), None
    except OSError as exc:
        return b"", f"cannot read {rel!r}: {exc}"


async def media_create(
    name: str,
    content: str = "",
    type: str = "",
    category: str = "generated",
    tags: list[str] | None = None,
    content_base64: str = "",
    mime_type: str = "",
    from_workspace: str = "",
) -> dict[str, Any]:
    """创建一份**可下载的资产**并写入媒体库（文本 / 二进制 / **工作区里的文件**）。

    这是 agent 产出"用户能打开/下载的文件"的**正确通道**：不要在 shell / python 里往
    宿主路径写文件 —— 沙箱会拒绝绝对路径，而且即便写进工作区，产物也不在媒体库里，
    用户根本拿不到。

    走的是与**用户直传**（POST /v1/media/upload）完全相同的落库路径：同一个对象存储键
    布局（`media/<tenant>/<assetID>/<name>`）+ 同一张 `media_assets` 表，因此产物会出现在
    「媒体库」页面并可下载。

    `from_workspace`（2026-10-09）：给一个**工作区相对路径**，把那个文件直接发布到媒体库。
    这是"**先生成、再发布**"那条路的落点 —— 用 `execute_python` 在工作区里生成
    docx/xlsx/pptx 之后，**不必**把字节编成 base64 递回来（那是这条路上最大的摩擦与出错源），
    在这里指名该文件即可。三个来源（`content` / `content_base64` / `from_workspace`）
    **互斥**，只能给一个。
    """
    display_name = (name or "").strip()
    workspace_rel = (from_workspace or "").strip()
    if not display_name and workspace_rel:
        # 兜底：名字可从工作区路径推断（schema 仍要求给 `name` —— 通常就是那个文件名）
        display_name = Path(workspace_rel).name
    if not display_name:
        return {"error": "name is required (include an extension, e.g. 'report.docx')"}
    if len(display_name) > MAX_NAME_CHARS:
        return {
            "error": (
                f"name is too long: {len(display_name)} chars (max {MAX_NAME_CHARS}; "
                "the media library column holds 255)"
            )
        }
    if "/" in display_name or "\\" in display_name:
        return {"error": "name must be a plain file name (no path separators)"}

    raw_content = content or ""
    raw_b64 = (content_base64 or "").strip()
    sources = [
        label
        for label, value in (
            ("content", raw_content),
            ("content_base64", raw_b64),
            ("from_workspace", workspace_rel),
        )
        if value
    ]
    if len(sources) > 1:
        return {
            "error": (
                f"provide only one of content / content_base64 / from_workspace "
                f"(got {', '.join(sources)})"
            )
        }
    if workspace_rel:
        data, read_error = _read_workspace_asset(workspace_rel)
        if read_error:
            return {"error": read_error}
        binary = True
    elif raw_b64:
        try:
            data = _decode_base64(raw_b64)
        except Exception:
            return {"error": "content_base64 is not valid base64"}
        binary = True
    elif raw_content:
        data = raw_content.encode("utf-8")
        binary = False
    else:
        return {"error": "one of content, content_base64 or from_workspace is required"}

    # 体积前置校验：与网关内部端点的上限一致（跑到网关再被拒等于白跑一趟）
    if len(data) > MAX_ASSET_BYTES:
        return {
            "error": (
                f"asset too large: {len(data)} bytes "
                f"(max {MAX_ASSET_BYTES // (1024 * 1024)} MB)"
            )
        }

    resolved_mime = (mime_type or "").strip() or _infer_mime(display_name, binary=binary)
    requested_type = (type or "").strip().lower()
    if requested_type and requested_type not in ASSET_TYPES:
        return {
            "error": f"unsupported type {type!r}; expected one of {', '.join(ASSET_TYPES)}"
        }
    # 留空 ⇒ 按扩展名/MIME 推断（旧行为把一切都标成 "text"，导致 .docx 在
    # 「文档」筛选里找不到）。显式传值仍然以调用方为准。
    resolved_type = requested_type or _infer_asset_type(resolved_mime, display_name)
    resolved_category = (str(category or "generated").strip() or "generated")[:MAX_CATEGORY_CHARS]
    safe_tags = _normalize_tags(tags)

    stored, reason = await persist_to_library(
        display_name,
        data,
        asset_type=resolved_type,
        category=resolved_category,
        mime_type=resolved_mime,
        tags=safe_tags,
    )
    if stored is not None:
        size = int(stored.get("size") or len(data))
        return {
            "output": (
                f"Created '{display_name}' in the media library "
                f"({size} bytes, type={resolved_type}). The user can open it from 媒体库 (Media)."
            ),
            "id": stored.get("id", ""),
            "name": stored.get("name", display_name),
            "type": stored.get("type", resolved_type),
            "category": resolved_category,
            "mime_type": resolved_mime,
            "file_url": stored.get("file_url", ""),
            "size": size,
            "stored_in": "library",
        }

    # 回退：引擎本地 store。**必须说出来** —— 否则"创建成功"是假的：用户按提示去媒体库
    # 什么也找不到，而模型一无所知（§1.3「失败要显式」）。
    asset = _store.write(
        name=display_name,
        content=data,
        asset_type=resolved_type,
        category=resolved_category,
        tags=safe_tags,
        fmt=resolved_mime,
    )
    return {
        "output": (
            f"Created '{display_name}' in the **engine-local store only** ({asset.size} bytes) — "
            f"it is NOT in the media library, so the user will not find it under 媒体库. "
            f"Reason: {reason}"
        ),
        "id": asset.id,
        "name": asset.name,
        "type": asset.type,
        "category": asset.category,
        "mime_type": resolved_mime,
        "file_url": asset.file_url,
        "size": asset.size,
        "stored_in": "engine-local",
        "warning": reason,
    }


# ── image_generate ────────────────────────────────────────────
async def image_generate(
    prompt: str = "Generated Image",
    width: int = 800,
    height: int = 600,
    category: str = "generated",
) -> dict[str, Any]:
    """真实生成图片；未配置 provider 时明确报错（S 修复：移除假的 SVG 占位，
    不再虚报生成成功）。配置 IMAGE_GEN_API_URL(+IMAGE_GEN_API_KEY) 接入生成服务。"""
    width = max(64, min(width, 4096))
    height = max(64, min(height, 4096))

    api_url = os.getenv("IMAGE_GEN_API_URL", "").strip()
    api_key = os.getenv("IMAGE_GEN_API_KEY", "").strip()
    if not api_url:
        return {
            "error": "image generation not available: IMAGE_GEN_API_URL not configured"
        }

    try:
        from app.config import settings
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=settings.http_timeout_long) as client:
            resp = await client.post(
                api_url,
                json={"prompt": prompt, "width": width, "height": height},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.content
    except Exception as e:  # noqa: BLE001 — 生成失败如实返回，不伪造成功
        return {"error": f"image generation failed: {e}"}

    name = _sanitize_filename(prompt) + ".png"

    # 优先写入 Go 媒体库；不可用时回退本地 store，但**把原因带出去**。
    stored, reason = await persist_to_library(
        name, data, asset_type="image", category=category, mime_type="image/png"
    )
    if stored is not None:
        size = int(stored.get("size") or len(data))
        return {
            "output": f"Image generated: {name} ({size} bytes)",
            "id": stored.get("id", ""),
            "name": name,
            "type": "image",
            "format": "image/png",
            "width": width,
            "height": height,
            "category": category,
            "file_url": stored.get("file_url", ""),
            "size": size,
            "stored_in": "library",
        }

    asset = _store.write(
        name=name,
        content=data,
        asset_type="image",
        category=category,
        fmt="image/png",
        width=width,
        height=height,
    )

    return {
        "output": (
            f"Image generated: {name} ({asset.size} bytes) — stored in the **engine-local store "
            f"only**, NOT in the media library. Reason: {reason}"
        ),
        "id": asset.id,
        "name": name,
        "type": "image",
        "format": "image/png",
        "width": width,
        "height": height,
        "category": category,
        "file_url": asset.file_url,
        "size": asset.size,
        "stored_in": "engine-local",
        "warning": reason,
    }


# ── vision_analyze: 图片理解 ─────────────────────────────────
async def vision_analyze(
    image_url: str,
    prompt: str = "请详细描述这张图片的内容。",
    detail: str = "auto",
) -> dict[str, Any]:
    """使用多模态模型（GPT-4V / Claude-3 Vision）分析图片内容。

    Args:
        image_url: 图片 URL（支持 http/https 或 data:image 格式）
        prompt: 分析提示词
        detail: 细节级别 "auto" | "low" | "high"

    Returns:
        包含分析结果和所用模型的字典
    """
    from app.media.analyzer import analyze_image

    result = await analyze_image(image_url, prompt=prompt, gateway=get_gateway())
    if result.get("success"):
        return result
    # analyze_image 失败时降级：用 llm 客户端直接调用
    try:
        from app.config import settings

        # SSRF 防护：跳过 data: URL
        if not image_url.startswith("data:"):
            assert_safe_url(image_url)

        async with httpx.AsyncClient(timeout=settings.http_timeout_web) as client:
            resp = await client.head(image_url, follow_redirects=False)
            content_type = resp.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                return {"error": f"URL does not point to an image: {content_type}"}

        # 降级路径复用同一个网关 —— 历史写法 `get_llm_client()` 在仓库里不存在，
        # 且 app/llm/client.py 的 LLMClient 只有 embed()，永远拿不到 chat()。
        gateway = get_gateway()
        if gateway is None:
            return {"success": False, "error": "no LLM gateway bound in tool context"}
        model = os.getenv("VISION_MODEL", "gpt-4o")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url, "detail": detail},
                    },
                ],
            }
        ]
        response = await gateway.chat(messages, model=model)
        return {
            "success": True,
            "analysis": (
                response.content if hasattr(response, "content") else str(response)
            ),
            "model": model,
            "analyzed_at": datetime.now(UTC).isoformat(),
        }
    except Exception as e:
        logger.error(f"Vision analysis failed: {e}")
        return {"success": False, "error": str(e)}


# ── speech_to_text: 语音转文字 ────────────────────────────────
async def speech_to_text(
    audio_url: str,
    language: str = "zh",
    prompt: str = "",
) -> dict[str, Any]:
    """将音频文件转为文字（Whisper API）。

    Args:
        audio_url: 音频文件 URL（支持 http/https 和 data:audio 格式）
        language: 音频语言代码（zh/en/ja 等）
        prompt: 可选的提示词，帮助模型理解上下文

    Returns:
        包含识别文本的字典
    """
    api_key = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not api_key:
        # 尝试从 settings 获取
        from app.config import settings

        api_key = settings.openai_api_key or settings.llm_api_key or ""
    if not api_key:
        return {
            "error": "speech_to_text not available: no OpenAI API key configured (set OPENAI_API_KEY)"
        }

    # SSRF 防护
    assert_safe_url(audio_url)

    try:
        from app.config import settings as s

        async with httpx.AsyncClient(timeout=s.http_timeout_long, follow_redirects=False) as client:
            resp = await fetch_url_safe(client, audio_url)
            resp.raise_for_status()
            audio_data = resp.content
            if len(audio_data) > MAX_DOWNLOAD_SIZE:
                return {"error": f"audio file too large: {len(audio_data)} bytes (max {MAX_DOWNLOAD_SIZE})"}

        # 调用 Whisper API
        whisper_url = f"{base_url.rstrip('/')}/audio/transcriptions"
        assert_safe_url(whisper_url)
        files = {"file": ("audio.wav", audio_data, resp.headers.get("content-type", "audio/wav"))}
        data = {"model": "whisper-1", "language": language, "response_format": "json"}
        if prompt:
            data["prompt"] = prompt

        async with httpx.AsyncClient(timeout=s.http_timeout_long) as client:
            whisper_resp = await client.post(
                whisper_url,
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
                data=data,
            )
            whisper_resp.raise_for_status()
            result = whisper_resp.json()

        return {
            "success": True,
            "text": result.get("text", ""),
            "language": language,
            "duration_seconds": result.get("duration", 0),
        }
    except Exception as e:
        logger.error(f"Speech to text failed: {e}")
        return {"error": f"speech_to_text failed: {e}"}


# ── text_to_speech: 文字转语音 ────────────────────────────────
async def text_to_speech(
    text: str,
    voice: str = "alloy",
    model: str = "tts-1",
    speed: float = 1.0,
    output_format: str = "mp3",
) -> dict[str, Any]:
    """将文字转为语音音频（OpenAI TTS API）。

    Args:
        text: 要转为语音的文字
        voice: 音色（alloy/echo/fable/onyx/nova/shimmer）
        model: TTS 模型（tts-1 / tts-1-hd）
        speed: 语速（0.25 ~ 4.0）
        output_format: 输出格式（mp3 / opus / aac / flac / wav）

    Returns:
        包含音频 data URL 的字典
    """
    api_key = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not api_key:
        from app.config import settings

        api_key = settings.openai_api_key or settings.llm_api_key or ""
    if not api_key:
        return {
            "error": "text_to_speech not available: no OpenAI API key configured (set OPENAI_API_KEY)"
        }

    from app.config import settings as s

    speed = max(0.25, min(speed, 4.0))
    valid_voices = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
    if voice not in valid_voices:
        voice = "alloy"
    valid_formats = {"mp3", "opus", "aac", "flac", "wav"}
    if output_format not in valid_formats:
        output_format = "mp3"

    try:
        # SSRF 防护：校验 OPENAI_BASE_URL 不指向内网
        tts_url = f"{base_url.rstrip('/')}/audio/speech"
        assert_safe_url(tts_url)
        async with httpx.AsyncClient(timeout=s.http_timeout_long) as client:
            resp = await client.post(
                tts_url,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "input": text,
                    "voice": voice,
                    "speed": speed,
                    "response_format": output_format,
                },
            )
            resp.raise_for_status()
            audio_data = resp.content

        mime_map = {"mp3": "audio/mpeg", "opus": "audio/opus", "aac": "audio/aac", "flac": "audio/flac", "wav": "audio/wav"}
        data_url = f"data:{mime_map.get(output_format, 'audio/mpeg')};base64,{base64.b64encode(audio_data).decode('ascii')}"

        return {
            "success": True,
            "data_url": data_url,
            "format": output_format,
            "bytes": len(audio_data),
            "voice": voice,
            "text_length": len(text),
        }
    except Exception as e:
        logger.error(f"Text to speech failed: {e}")
        return {"error": f"text_to_speech failed: {e}"}


# ── file_analyzer: 文件分析 ───────────────────────────────────
async def file_analyzer(
    file_url: str,
    analysis_type: str = "auto",
    custom_prompt: str = "",
) -> dict[str, Any]:
    """分析文件内容（PDF/CSV/Excel/图片/文本等）。

    Args:
        file_url: 文件 URL
        analysis_type: 分析类型（auto/pdf/csv/image/text）
        custom_prompt: 自定义分析提示词

    Returns:
        包含分析结果的字典
    """
    # SSRF 防护
    assert_safe_url(file_url)

    from app.config import settings as s

    try:
        # 下载文件
        async with httpx.AsyncClient(timeout=s.http_timeout_long, follow_redirects=False) as client:
            resp = await fetch_url_safe(client, file_url)
            resp.raise_for_status()
            file_data = resp.content
            content_type = resp.headers.get("content-type", "")

        # 文件大小限制（100MB）
        if len(file_data) > MAX_DOWNLOAD_SIZE:
            return {"error": f"file too large: {len(file_data)} bytes (max {MAX_DOWNLOAD_SIZE})"}

        # 根据类型决定分析策略
        is_image = content_type.startswith("image/") or analysis_type == "image"
        is_pdf = "pdf" in content_type or analysis_type == "pdf" or file_url.lower().endswith(".pdf")
        is_csv = "csv" in content_type or analysis_type == "csv" or file_url.lower().endswith(".csv")
        is_excel = "spreadsheet" in content_type or analysis_type in ("excel", "xlsx") or file_url.lower().endswith((".xlsx", ".xls"))

        if is_image and analysis_type != "text":
            b64 = base64.b64encode(file_data).decode("ascii")
            data_url = f"data:{content_type};base64,{b64}"
            return await vision_analyze(
                image_url=data_url,
                prompt=custom_prompt or "请详细分析这张图片的内容，包括主要对象、场景、文字（如有）。",
            )

        # 文本类文件 → 提取文本后用 LLM 分析
        text = ""
        if is_pdf:
            import pymupdf

            doc = pymupdf.open(stream=file_data, filetype="pdf")
            # pymupdf 自带 py.typed，但 Document 的 stub 缺 __iter__（运行期按页可迭代），
            # 且 open/close 无注解（后者由 pyproject 对该文件豁免 untyped-calls）。
            text = "\n".join(
                page.get_text() for page in cast("list[Any]", doc)
            )
            doc.close()
        elif is_csv:
            text = file_data.decode("utf-8", errors="replace")
            lines = text.splitlines()
            if len(lines) > 50:
                text = "\n".join(lines[:50]) + f"\n\n... (共 {len(lines)} 行，仅显示前 50 行)"
        elif is_excel:
            import io

            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(file_data), read_only=True, data_only=True)
            parts = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = list(ws.iter_rows(values_only=True))
                preview = rows[:20]
                parts.append(f"=== Sheet: {sheet_name} ({len(rows)} rows) ===")
                for row in preview:
                    parts.append(", ".join(str(c) if c is not None else "" for c in row))
            text = "\n".join(parts)
            wb.close()
        else:
            # 文本文件
            text = file_data.decode("utf-8", errors="replace")[:50000]

        if not text.strip():
            return {"error": "无法提取文件内容，不支持的格式或空文件"}

        # 用 LLM 分析
        gateway = get_gateway()
        if gateway is None:
            return {"error": "no LLM gateway bound in tool context"}
        model = os.getenv("VISION_MODEL", "gpt-4o")
        prompt_text = custom_prompt or f"""请分析以下文件内容：

{text[:30000]}

请提供：
1. 文件类型和概览
2. 主要内容摘要
3. 关键数据/发现
4. 可能的用途或建议"""
        response = await gateway.chat([{"role": "user", "content": prompt_text}], model=model)
        return {
            "success": True,
            "analysis": response.content if hasattr(response, "content") else str(response),
            "file_type": content_type,
            "text_length": len(text),
            "analyzed_at": datetime.now(UTC).isoformat(),
        }
    except Exception as e:
        logger.error(f"File analysis failed: {e}")
        return {"error": f"file_analyzer failed: {e}"}


# ── 注册 ──────────────────────────────────────────────────────
registry.register(
    name="media_create",
    description=(
        "Create a downloadable asset in the media library — this is THE way to produce a file "
        "for the user: Markdown/CSV/JSON/code as plain text, images / Office documents as "
        "base64, or **a file you already generated in the workspace** by naming its path. "
        "The asset shows up under 媒体库 (Media) and can be downloaded. "
        "Do NOT write files to host paths via shell/python: that is sandboxed and the user cannot "
        "reach the result."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "File name WITH extension, e.g. 'report.docx', 'data.csv', 'summary.md'.",
            },
            "content": {
                "type": "string",
                "description": "Text content. Omit when passing content_base64 or from_workspace.",
            },
            "content_base64": {
                "type": "string",
                "description": (
                    "Base64 of the raw bytes, for binary files (docx/xlsx/pptx/pdf/png…). "
                    "Line breaks and missing padding are tolerated."
                ),
            },
            "from_workspace": {
                "type": "string",
                "description": (
                    "Workspace-relative path of an existing file to publish, e.g. "
                    "'reports/q3.docx'. Use this after generating a file inside the workspace "
                    "(python-docx / openpyxl / shell) instead of base64-encoding its bytes — "
                    "the sandbox workspace is the only place the tool can read from. "
                    "Exactly one of content / content_base64 / from_workspace must be given."
                ),
            },
            "mime_type": {
                "type": "string",
                "description": "Optional MIME type; inferred from the file name when omitted.",
            },
            "type": {
                "type": "string",
                "enum": list(ASSET_TYPES),
                "description": "Asset kind; inferred from the file name / MIME when omitted.",
            },
            "category": {"type": "string", "default": "generated"},
            "tags": {"type": "array", "items": {"type": "string"}, "default": []},
        },
        "required": ["name"],
    },
    handler=media_create,
)

registry.register(
    name="image_generate",
    description="Generate an image from a text prompt (requires IMAGE_GEN_API_URL; fails loudly when unconfigured).",
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "default": "Generated Image"},
            "width": {"type": "integer", "default": 800},
            "height": {"type": "integer", "default": 600},
            "category": {"type": "string", "default": "generated"},
        },
    },
    handler=image_generate,
)

registry.register(
    name="vision_analyze",
    description="Analyze an image using a vision-capable model (GPT-4V / Claude-3 Vision). Provide an image URL and optional prompt.",
    parameters={
        "type": "object",
        "properties": {
            "image_url": {"type": "string", "description": "Image URL (http/https or data:image)"},
            "prompt": {"type": "string", "default": "请详细描述这张图片的内容。"},
            "detail": {"type": "string", "enum": ["auto", "low", "high"], "default": "auto"},
        },
        "required": ["image_url"],
    },
    handler=vision_analyze,
)

registry.register(
    name="speech_to_text",
    description="Transcribe audio to text using Whisper API. Provide an audio URL and optional language code.",
    parameters={
        "type": "object",
        "properties": {
            "audio_url": {"type": "string", "description": "Audio file URL (http/https)"},
            "language": {"type": "string", "default": "zh", "description": "Audio language code (zh/en/ja)"},
            "prompt": {"type": "string", "default": "", "description": "Optional prompt to guide the model"},
        },
        "required": ["audio_url"],
    },
    handler=speech_to_text,
)

registry.register(
    name="text_to_speech",
    description="Convert text to speech audio using OpenAI TTS API. Returns a data URL with the audio content.",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to convert to speech"},
            "voice": {"type": "string", "enum": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"], "default": "alloy"},
            "model": {"type": "string", "default": "tts-1"},
            "speed": {"type": "number", "default": 1.0, "description": "Speech speed (0.25-4.0)"},
            "output_format": {"type": "string", "enum": ["mp3", "opus", "aac", "flac", "wav"], "default": "mp3"},
        },
        "required": ["text"],
    },
    handler=text_to_speech,
)

registry.register(
    name="file_analyzer",
    description="Analyze a file (PDF, CSV, Excel, image, text) and extract its content and insights.",
    parameters={
        "type": "object",
        "properties": {
            "file_url": {"type": "string", "description": "File URL to analyze"},
            "analysis_type": {
                "type": "string",
                "enum": ["auto", "pdf", "csv", "image", "text", "excel"],
                "default": "auto",
                "description": "Force a specific analysis type",
            },
            "custom_prompt": {"type": "string", "default": "", "description": "Custom analysis prompt"},
        },
        "required": ["file_url"],
    },
    handler=file_analyzer,
)
