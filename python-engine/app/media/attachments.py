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

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 单个附件提取出的文本上限（与 `main._resolve_attachments` 同值，避免出现两套口径）。
MAX_ATTACHMENT_CHARS = 8000
#: **本轮直接进上下文**的单附件字符数。与上面的区别是"给模型看多少"和"最多能提取多少"：
#: 预览只是让模型知道这个文件大致是什么，细节应由 `attachment_read` 分片取。取 2000 是因为
#: 它约等于一屏正文，足够判断相关性；再大的话一个 50MB 的 PDF 光预览就能吃掉半窗。
MAX_ATTACHMENT_PREVIEW_CHARS = 2000
#: 单个附件的下载上限：超过就不再下载，直接给提示（别为一个附件把内存拉满）。
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

#: 单轮附件**数量**上限。前端只限制单文件 50MB、不限个数，而每个附件都会真正进入上下文
#: （文本 8000 字符 / 图片按像素计价），不设上限时"一次拖 20 个文件"就等于一轮烧掉整窗。
MAX_ATTACHMENTS_PER_TURN = 8
#: 单轮附件**提取文本**的总字符预算。按剩余附件数**均分**（见 `TurnBudget.text_allowance`）：
#: 第一个大文件吃掉全部预算、后面全变占位符，比统一上限更糟 —— 用户看到的是"只读了第一个"。
MAX_TURN_ATTACHMENT_CHARS = 20_000
#: 单轮图片张数上限。一张 1568px 的图约 850 token（`model_window.image_tokens`），
#: 但这个数字是按"一次看几张"权衡的：再多，模型对单张的注意力就摊薄了。
MAX_IMAGES_PER_TURN = 4

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


async def _fetch_attachment(
    att: dict[str, Any], *, tenant_id: int = 0, user_id: str = ""
) -> tuple[bytes, str] | None:
    """取回附件字节；取不回返回 ``None``。

    取回顺序有意固定成这样：

    1. **内部通道**（有 `id` + 服务端身份）—— 唯一在真实部署里可靠的一条。前端给的
       `url` 是**相对路径**（`/media/s/...`，见 Go 的 `signMediaURL`），主机名取决于用户
       从哪个域名进来，引擎无从得知；S3 直链又常指向内网端点。两条都会被引擎出站统一
       的 SSRF 防护拒绝（只收 http/https、拒内网地址），于是附件稳定退化成"本次未能取回"。
    2. **data URL** —— 内联形态（测试与前端小文件内联上传），零网络。
    3. **旧 URL 回退** —— 只在拿不到 `id`/身份时使用，仍走 `fetch_url_safe`。

    第 1 条**不**走 SSRF 校验：URL 与主机都来自部署配置（`gateway_internal_url`），
    没有一个字节来自用户输入；套上"拒绝内网地址"反而会把唯一的可用通道挡掉。
    """
    asset_id = str(att.get("id") or "").strip()
    if asset_id and tenant_id and user_id:
        fetched = await _fetch_via_gateway(asset_id, tenant_id, user_id, att)
        if fetched is not None:
            return fetched

    url = str(att.get("url") or "")
    if not url:
        return None
    if url.startswith("data:"):
        return _decode_data_url(url)
    return await _fetch_via_url(url, att)


async def _fetch_via_gateway(
    asset_id: str, tenant_id: int, user_id: str, att: dict[str, Any]
) -> tuple[bytes, str] | None:
    """经网关内部端点按 `asset_id` 取字节（`GET /v1/internal/media/{id}/content`）。

    归属由**查询谓词**在网关侧保证：`tenant_id`/`user_id` 与资产行不匹配时一律 404。
    这里传的身份来自已鉴权的任务（`AgentTask.tenant_id/user_id`），不是附件里的字段。
    """
    got = await _fetch_asset_bytes(asset_id, tenant_id, user_id, fallback_mime=str(att.get("mime_type") or ""))
    if got is None:
        return None
    data, mime, _name = got
    return data, mime


async def _fetch_asset_bytes(
    asset_id: str,
    tenant_id: int,
    user_id: str,
    *,
    fallback_mime: str = "",
) -> tuple[bytes, str, str] | None:
    """按 `asset_id` 从网关取字节，返回 ``(data, mime, name)``。

    与 `_fetch_via_gateway` 同一端点，区别只在**多返回文件名**：`attachment_read` 只拿得到
    一个 `asset_id`，没有文件名就无法判断该按 pdf/docx/文本解析（`_extract_body` 靠扩展名）。
    文件名来自网关的 `X-Media-Name`（服务端已按上传名净化），不是客户端现编的。
    """
    try:
        from app.config import settings
        from app.http_client import shared_client

        if not settings.internal_token:
            return None
        base = settings.gateway_internal_url.rstrip("/")
        url = f"{base}/v1/internal/media/{asset_id}/content"
        params = {"tenant_id": str(tenant_id), "user_id": user_id}
        async with shared_client(timeout=settings.http_timeout_web) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"X-Internal-Token": settings.internal_token},
            )
            if resp.status_code != 200:
                logger.info(
                    "attachment fetch via gateway: %s -> HTTP %s", asset_id, resp.status_code
                )
                return None
            data = resp.content[: MAX_ATTACHMENT_BYTES + 1]
            if len(data) > MAX_ATTACHMENT_BYTES:
                logger.info(
                    "attachment fetch via gateway: %s exceeds %d bytes",
                    asset_id,
                    MAX_ATTACHMENT_BYTES,
                )
                return None
            if not data:
                return None
            # 优先用网关按 magic-bytes 定的类型：附件里声明的 mime 是客户端说法，
            # 只作兜底（否则一个改名的 .png 会被当文本拿去解码）。
            mime = (resp.headers.get("content-type", "") or "").split(";")[0].strip()
            return data, mime or fallback_mime, resp.headers.get("x-media-name", "") or ""
    except Exception as exc:  # noqa: BLE001 — 取不回只是"这个附件这次用不了"
        logger.info("attachment fetch via gateway failed (%s): %s", asset_id, exc)
        return None


async def _asset_still_exists(asset_id: str, tenant_id: int, user_id: str) -> bool:
    """附件是否还存在（用 Range 只取 1 字节，不下载正文）。

    索引是按 ``asset_id`` 长期复用的：资产被删掉后，索引里的内容仍然能被问答命中。
    读取前校验一次归属与存在性，是"删除即不可达"的唯一可靠落点 —— 引擎删不到网关的
    ``media_assets``，也不该让两侧的删除时序互相依赖。
    """
    try:
        from app.config import settings
        from app.http_client import shared_client

        if not settings.internal_token:
            return False
        base = settings.gateway_internal_url.rstrip("/")
        url = f"{base}/v1/internal/media/{asset_id}/content"
        async with shared_client(timeout=settings.http_timeout_web) as client:
            resp = await client.get(
                url,
                params={"tenant_id": str(tenant_id), "user_id": user_id},
                headers={"X-Internal-Token": settings.internal_token, "Range": "bytes=0-0"},
            )
            return resp.status_code in (200, 206)
    except Exception as exc:  # noqa: BLE001 — 判不了就按"不存在"处理（fail-closed）
        logger.info("attachment exists check failed (%s): %s", asset_id, exc)
        return False


async def _fetch_via_url(url: str, att: dict[str, Any]) -> tuple[bytes, str] | None:
    """旧路径：按 URL 取回（走 SSRF 防护）。相对路径在这里必然失败，只留作兼容。"""
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
            # 走 `pages()` 而不是 `for page in doc`：Document 没有 `__iter__`，靠的是
            # `__getitem__` 的旧式迭代协议（越界靠 IndexError 收尾）—— 能跑，但既不是
            # 官方口径、类型上也只能报"not iterable"。
            return "\n".join(page.get_text() for page in doc.pages())
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


def _extract_body(data: bytes, mime: str, name: str) -> str:
    """按类型取出**全文**（不截断）；提取不出返回空串。

    与 `_extract_text` 分开是为了 `attachment_read`：那个工具要按 offset/limit 分片，
    必须先拿到全文再切；而进上下文的预览只该取前面一小段。两处若各写一套解析，
    pdf/docx 的判定迟早漂移。
    """
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    lowered = mime.lower()
    if lowered == "application/pdf" or ext == "pdf":
        return _pdf_text(data)
    if ext == "docx" or "wordprocessingml" in lowered:
        return _docx_text(data)
    if lowered.startswith("text/") or ext in _TEXT_EXTS:
        return data.decode("utf-8", "replace")
    return ""


def _extract_text(
    data: bytes,
    mime: str,
    name: str,
    *,
    budget: int = MAX_ATTACHMENT_CHARS,
    asset_id: str = "",
) -> str:
    """按类型提取文本；提取不出时返回**提示**（不是乱码、也不是静默丢弃）。

    `budget` 是本附件可分到的字符数（由 `_text_budget_for` 按分母均分），不超过
    `MAX_ATTACHMENT_CHARS`。截断必须**说出来**：否则模型会把"只给了前几千字"
    当成"全文就这么短"，进而基于缺失的上下文下结论。给了 `asset_id` 时同时指出
    续读方式 —— 只说"已截断"而不给出口，模型只能猜或者放弃。
    """
    text = _extract_body(data, mime, name)
    if not text.strip():
        return _unsupported_note(name, mime)
    limit = max(0, min(budget, MAX_ATTACHMENT_CHARS))
    snippet = text[:limit]
    if len(text) > limit:
        # 截断必须给出**续读出口**：只说"已截断"，模型除了猜就只能让用户重发文件。
        # 明确到工具与参数，它才知道下一步能做什么。
        more = (
            f"\n... (已截断，仅显示前 {limit} 字符，原文共 {len(text)} 字符；"
            f"需要后续内容请调用 attachment_read(asset_id=\"{asset_id}\", offset={limit}, "
            f"limit={MAX_ATTACHMENT_CHARS}))"
            if asset_id
            else f"\n... (已截断，仅显示前 {limit} 字符，原文共 {len(text)} 字符)"
        )
    else:
        more = ""
    return f"===== 附件「{name}」内容 =====\n{snippet}{more}\n===== 附件结束 ====="


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


def _too_many_images_note(name: str) -> str:
    return (
        f"[图片附件「{name}」超出本轮 {MAX_IMAGES_PER_TURN} 张图片上限，未交给模型。"
        "如需理解它，请调用 vision_analyze。]"
    )


def _over_limit_note(names: list[str]) -> str:
    """超出数量上限的附件：**一条汇总提示**，不是每条一句 —— 否则提示本身就会撑大上下文。

    总数**总要写出来**（哪怕名单已经被完整列出）：用户需要知道"还有几个没被处理"，
    否则他会以为列出来的就是全部。
    """
    shown = "、".join(names[:5])
    suffix = " 等" if len(names) > 5 else ""
    return (
        f"[本轮附件超过 {MAX_ATTACHMENTS_PER_TURN} 个上限，以下共 {len(names)} 个未处理："
        f"{shown}{suffix}。请分轮发送，或把需要的文件单独再发一次。]"
    )


def _xml_attr(value: str) -> str:
    """引用块里的属性值转义：文件名来自用户，含引号会破坏标签结构。"""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\n", " ")
    )


def attachment_reference_block(
    attachments: list[Any] | None, *, over: list[dict[str, Any]] | None = None
) -> str:
    """把本轮附件折成**引用块**——写历史时用，只留定位信息，不留内容。

    为什么必须折：会话缓存是整表覆盖写（见 `SessionStore._redis_set`：一次
    `json.dumps(整个 messages)` 再 `setex`），内联图片与提取文本会被**每一轮重发**，
    实测单个 20MB 文件 10 轮累积约 280MB。引用块只有几十字节，且 `asset_id` 足以
    在需要时经内部通道重新取字节（`attachment_read`）。

    折不出任何引用（附件没有 `id`，例如纯 data URL 的内联形态）时返回空串：
    那种情况内容本来就不在媒体库里，写了引用也取不回来。
    """
    kept, extra = _take_turn_attachments(attachments)
    lines: list[str] = []
    for att in kept:
        asset_id = str(att.get("id") or "").strip()
        if not asset_id:
            continue
        size = att.get("size")
        lines.append(
            f'<attachment id="{_xml_attr(asset_id)}" name="{_xml_attr(_display_name(att))}"'
            f' mime="{_xml_attr(str(att.get("mime_type") or ""))}"'
            f' bytes="{size if isinstance(size, int) else 0}" />'
        )
    if over:
        extra = [*extra, *over]
    if not lines:
        return ""
    body = "\n".join(lines)
    # 提示必须给出口：只说"附件在历史里是引用"，模型下一轮会以为内容丢了。
    note = "（附件内容未随历史保留；需要时调用 attachment_read(asset_id=..., offset=0) 读取。）"
    if extra:
        note += f"\n另有 {len(extra)} 个附件未处理。"
    return f"[本轮附件引用]\n{body}\n{note}"


# ── 单轮预算（两个产物共用同一套取舍）────────────────────────────────────
def _take_turn_attachments(
    attachments: list[Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按数量上限切分：返回（本轮要处理的，超出上限的）。

    前端不限个数（只限单文件 50MB），所以上限必须在**服务端**这一处设 —— 两条链路都经过
    这里，天然一致。
    """
    valid = [a for a in (attachments or []) if isinstance(a, dict)]
    return valid[:MAX_ATTACHMENTS_PER_TURN], valid[MAX_ATTACHMENTS_PER_TURN:]


def _text_budget_for(kept: list[dict[str, Any]]) -> int:
    """每个文本附件可分到的字符数：总预算按**非图片**附件数均分，且不超过预览上限。

    均分而不是"先到先得"：否则第一个大文件吃掉全部预算，其余全变成一句"已截断"，
    用户的观感是"只读了第一个"。分母只数非图片附件 —— 图片不吃字符预算（按像素计），
    把它们算进分母会让文本额度凭空变少。

    **为什么还要压到 `MAX_ATTACHMENT_PREVIEW_CHARS`**：均分只保证总额不超
    `MAX_TURN_ATTACHMENT_CHARS`，单附件仍可拿到 8000 字符；而本轮给模型的只是预览，
    正文细节该由 `attachment_read` 按需分片取。不压的话，"每轮都带一份几千字的文件开头"
    会随轮次累加，正是附件驻留问题的另一半。
    """
    text_count = sum(1 for a in kept if not is_image_attachment(a))
    if text_count <= 0:
        return 0
    share = MAX_TURN_ATTACHMENT_CHARS // text_count
    return max(1, min(share, MAX_ATTACHMENT_PREVIEW_CHARS))


# ── 两种产物 ────────────────────────────────────────────────────────────

#: 后台索引任务的强引用。`asyncio.create_task` 只持弱引用，不存下来任务可能在
#: 跑完前被 GC 掉（Python 文档明确要求调用方保留引用）。
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()

#: 可能被结构化索引的扩展名。先按扩展名挡一道，避免文本/图片每轮都去连一次
#: 数据库和 Redis 判断"能不能索引"——它们本来就走分片读取。
_MAYBE_INDEXABLE = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}


def _maybe_indexable(name: str, mime: str) -> bool:
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in (name or "") else ""
    if suffix in _MAYBE_INDEXABLE:
        return True
    return any(k in (mime or "") for k in ("pdf", "msword", "officedocument"))


def _register_for_indexing(
    raw: Any, data: bytes, mime: str, name: str, *, tenant_id: int, user_id: str
) -> None:
    """把附件登记进个人附件库的索引（后台，不阻塞本轮）。

    只对 provider 支持的类型真正建索引（selfhosted 下即 PDF）；其余类型
    ``index_attachment`` 直接返回 None，读取走本地分片。

    失败一律吞掉：索引是"读得更准"的增强，不能因为它让用户发不出文件。
    归属取自**调用方传进来的任务归属**（服务端已鉴权），不是工具上下文 ——
    注入路径不保证运行在工具调用栈里。
    """
    asset_id = str((raw or {}).get("id") or "")
    if not asset_id or not data or not tenant_id or not user_id:
        return
    if not _maybe_indexable(name, mime):
        return

    async def _run() -> None:
        from app.knowledge.attachment_index import index_attachment

        try:
            await index_attachment(
                tenant_id=tenant_id,
                user_id=user_id,
                asset_id=asset_id,
                name=name,
                data=data,
                # 不传 mime：`knowledge_documents.file_type` 与知识库上传路径一致地存扩展名
                # （mime 只用于这里的 `_maybe_indexable` 粗筛）。
                file_type="",
            )
        except Exception as exc:  # noqa: BLE001 — 见函数文档：不影响本轮
            logger.warning("attachment index skipped asset=%s: %s", asset_id, exc)

    try:
        task = asyncio.create_task(_run())
    except RuntimeError:  # 没有运行中的事件循环（同步调用场景）：退化为不索引
        logger.debug("attachment index: no running loop, asset=%s not indexed", asset_id)
        return
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)



async def to_content_parts(
    attachments: list[Any] | None, *, model: str = "", tenant_id: int = 0, user_id: str = ""
) -> list[dict[str, Any]]:
    """附件 → OpenAI 兼容 content parts（图片块 + 文本块）。

    `model` 用于判定"这个模型能不能读图"；传空串时按可读处理（由调用方决定）。
    `tenant_id` / `user_id` 是**服务端已鉴权的任务归属**，用于走网关内部通道取字节；
    不传时退化为按附件自带的 URL 取回（仅 data URL 可靠，见 `_fetch_attachment`）。

    预算三轴都在这里落地（数量 / 文本字符 / 图片张数），见各自常量的注释：前端不限个数、
    单个附件又可达 8000 字符，不设上限时一轮附件就能吃满整个窗口。
    """
    if not attachments:
        return []
    from app.agent.model_capabilities import supports_vision

    vision = await supports_vision(model) if model else True
    kept, over = _take_turn_attachments(attachments)
    text_budget = _text_budget_for(kept)
    parts: list[dict[str, Any]] = []
    images = 0
    for raw in kept:
        name = _display_name(raw)
        if is_image_attachment(raw):
            if not vision:
                parts.append({"type": "text", "text": _visionless_note(name)})
                continue
            if images >= MAX_IMAGES_PER_TURN:
                parts.append({"type": "text", "text": _too_many_images_note(name)})
                continue
            fetched = await _fetch_attachment(raw, tenant_id=tenant_id, user_id=user_id)
            if fetched is None:
                parts.append({"type": "text", "text": _unavailable_note(name)})
                continue
            data, mime = fetched
            data_url = _image_data_url(data, mime, name)
            if data_url is None:
                parts.append({"type": "text", "text": _unavailable_note(name)})
                continue
            images += 1
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
            parts.append({"type": "text", "text": f"[图片附件：{name}]"})
            continue

        fetched = await _fetch_attachment(raw, tenant_id=tenant_id, user_id=user_id)
        if fetched is None:
            parts.append({"type": "text", "text": _unavailable_note(name)})
            continue
        data, mime = fetched
        _register_for_indexing(raw, data, mime, name, tenant_id=tenant_id, user_id=user_id)
        parts.append(
            {
                "type": "text",
                "text": _extract_text(
                    data,
                    mime,
                    name,
                    budget=text_budget,
                    asset_id=str(raw.get("id") or ""),
                ),
            }
        )

    if over:
        parts.append({"type": "text", "text": _over_limit_note([_display_name(a) for a in over])})
    return parts


async def to_text(
    attachments: list[Any] | None, *, tenant_id: int = 0, user_id: str = ""
) -> str:
    """附件 → 一段文本（给只吃字符串的执行路径，如统一链路的 SubAgent）。

    与 `to_content_parts` 共用同一套预算判定 —— 两条链路的观感不能因入口不同而不同。
    """
    if not attachments:
        return ""
    kept, over = _take_turn_attachments(attachments)
    text_budget = _text_budget_for(kept)
    blocks: list[str] = []
    images = 0
    for raw in kept:
        name = _display_name(raw)
        if is_image_attachment(raw):
            # 这条路径只能传文本：明确说出"图没交给模型"，并指出该用哪个工具。
            if images >= MAX_IMAGES_PER_TURN:
                blocks.append(_too_many_images_note(name))
                continue
            images += 1
            blocks.append(
                f"[图片附件：{name}]（本入口只能传文本，未交给模型；"
                "如需理解图片内容请调用 vision_analyze。）"
            )
            continue
        fetched = await _fetch_attachment(raw, tenant_id=tenant_id, user_id=user_id)
        if fetched is None:
            blocks.append(_unavailable_note(name))
            continue
        data, mime = fetched
        _register_for_indexing(raw, data, mime, name, tenant_id=tenant_id, user_id=user_id)
        blocks.append(
            _extract_text(
                data, mime, name, budget=text_budget, asset_id=str(raw.get("id") or "")
            )
        )
    if over:
        blocks.append(_over_limit_note([_display_name(a) for a in over]))
    return "\n\n".join(blocks)
