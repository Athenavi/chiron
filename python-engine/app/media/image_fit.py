"""图片进入模型请求前的**唯一裁决**（对齐 Reasonix 的 ``internal/model/visionimage``）。

**为什么要有这一层**：host（provider）会把超限图片当作 *unsupported format* 拒绝，
而那条报错会让该会话**之后每一轮**都失败。所以"装不下就不发"必须是**一处**判断，
而不是每个调用点各自 try/except。

两类调用点走这里：

* ``read_image``（工作区里的图片文件）；
* 附件与 ``media_offload``（用户上传 / @引用 / 历史内联 data URL）。

与 Reasonix 一致的取舍：

* **只看字节，不看扩展名** —— 扩展名是声明，声明错了会到 provider 那里变成
  media_type 与 payload 不符；
* **限内不重编码**（省 CPU、也保真："传更大只浪费字节和 token"）；
* PNG/GIF 保无损（截图、文字、透明），JPEG/WebP 转 JPEG；
* **装不下就拒**（``ImageUnfit``），绝不原样转发。
"""

from __future__ import annotations

import io

from PIL import Image

#: 最长边上限。OpenAI / Anthropic 服务端本来也会降到这个量级，
#: 传更大只浪费请求字节与图片 token，不增加保真度。
MAX_DIM = 1568
#: 单张图片的字节上限（与 Reasonix 的 MaxImageAttachmentBytes 同值）。
MAX_IMAGE_BYTES = 10 * 1024 * 1024
#: 解码像素上限：防解压炸弹（小文件可以声明极大尺寸，缩放它才是分配炸弹的那一步）。
MAX_DECODE_PIXELS = 50_000_000
#: JPEG 重编码质量（与 Reasonix 同值）。
JPEG_QUALITY = 85

#: 被接受的四类 —— 每个 vision host 都认（顺序即展示顺序）。
ACCEPTED: tuple[tuple[str, str, str], ...] = (
    ("image/png", ".png", "PNG"),
    ("image/jpeg", ".jpg", "JPEG"),
    ("image/gif", ".gif", "GIF"),
    ("image/webp", ".webp", "WebP"),
)

_MIME_TO_EXT = {mime: ext for mime, ext, _ in ACCEPTED}
_MIME_TO_LABEL = {mime: label for mime, _, label in ACCEPTED}
_LABELS = "/".join(label for _, _, label in ACCEPTED)


class ImageUnfit(Exception):  # noqa: N818 — 名字跟随 Reasonix 的 `ErrUnfit` 语义（"装不下就拒"）
    """图片**无法证明**装得下 —— 因此永不上线。"""


def sniff_mime(raw: bytes) -> str:
    """按 magic bytes 判定类型；不是被接受的四类时返回 ``""``。

    与 Reasonix 的 ``imageKind`` 同一组前缀：PNG / JPEG / GIF / WebP。
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] == b"GIF8":
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def sniff_label(raw: bytes) -> str:
    """报告"看起来是什么"（拒绝文案用），不回答"是否被接受"。"""
    return _MIME_TO_LABEL.get(sniff_mime(raw), "")


def ext_for(mime: str) -> str:
    """被接受的 media type 对应扩展名；否则 ``""``。"""
    return _MIME_TO_EXT.get((mime or "").lower().strip(), "")


def is_accepted(mime: str) -> bool:
    return ext_for(mime) != ""


def fit(raw: bytes, mime: str) -> tuple[bytes, str]:
    """返回可以上线的一份 ``(bytes, mime)``。

    限内**原样返回**（不重编码）；超限则等比缩放到 :data:`MAX_DIM` 内并重编码：
    PNG/GIF 保无损转 PNG，JPEG/WebP 转 JPEG。任何"无法证明装得下"的情况
    （没有解码器、字节解不开、像素数超上限）都抛 :class:`ImageUnfit`。
    """
    if not is_accepted(mime):
        raise ImageUnfit(f"no decoder for {mime or 'unknown'}; accepted: {_LABELS}")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if width < 1 or height < 1:
                raise ImageUnfit("image declares no pixels")
            if width <= MAX_DIM and height <= MAX_DIM:
                return raw, mime
            if width * height > MAX_DECODE_PIXELS:
                raise ImageUnfit(
                    f"{width}x{height} is past the {MAX_DECODE_PIXELS}-pixel decode limit, "
                    "so it cannot be downscaled - shrink it first"
                )
            keep_lossless = mime in ("image/png", "image/gif")
            target = _scaled(width, height)
            frame = image.convert("RGBA" if keep_lossless else "RGB")
            frame = frame.resize(target, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            if keep_lossless:
                frame.save(out, format="PNG", optimize=True)
                return out.getvalue(), "image/png"
            frame.save(out, format="JPEG", quality=JPEG_QUALITY)
            return out.getvalue(), "image/jpeg"
    except ImageUnfit:
        raise
    except Exception as exc:  # noqa: BLE001 - 解不开 = 无法证明尺寸，一律拒
        raise ImageUnfit(f"the bytes do not decode as {mime}: {exc}") from exc


def _scaled(width: int, height: int) -> tuple[int, int]:
    """最长边夹到 :data:`MAX_DIM`，保持宽高比（每边至少 1px）。"""
    if width >= height:
        return MAX_DIM, max(height * MAX_DIM // width, 1)
    return max(width * MAX_DIM // height, 1), MAX_DIM
