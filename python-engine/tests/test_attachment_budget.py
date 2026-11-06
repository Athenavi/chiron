"""单轮附件的三条预算：数量 / 文本字符 / 图片张数。

**为什么需要上限**：前端只限制单文件 50MB、**不限个数**（`ChatInput.handleFiles` 逐个判
体积，没有集合上限），而每个附件都真的进入上下文 —— 文本单个可达 8000 字符、图片按像素
计价。一次拖 20 个文件就等于一轮烧掉整窗，且用户完全看不出发生了什么。

三条取舍都在这里钉住：

* **数量**：超出的部分给**一条汇总提示**（不是每条一句，否则提示本身撑大上下文）；
* **文本**：总预算按**非图片附件数均分**，不是先到先得 —— 否则第一个大文件吃掉全部额度，
  其余全变"已截断"，观感是"只读了第一个"；
* **图片**：超出张数上限的不再取字节，改指 `vision_analyze`（取回通道还在）。

两个产物（`to_content_parts` / `to_text`）必须给出同样的判定 —— 同一个文件从不同入口
进去，表现不能不一样。
"""

from __future__ import annotations

import base64
import io

from app.agent import model_capabilities
from app.media.attachments import (
    MAX_ATTACHMENT_CHARS,
    MAX_ATTACHMENT_PREVIEW_CHARS,
    MAX_ATTACHMENTS_PER_TURN,
    MAX_IMAGES_PER_TURN,
    MAX_TURN_ATTACHMENT_CHARS,
    _text_budget_for,
    to_content_parts,
    to_text,
)


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (7, 7, 7)).save(buf, format="PNG")
    return buf.getvalue()


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _text_att(name: str, body: str) -> dict:
    return {
        "id": name,
        "name": name,
        "mime_type": "text/plain",
        "url": _data_url(body.encode(), "text/plain"),
    }


def _image_att(name: str) -> dict:
    return {
        "id": name,
        "name": name,
        "mime_type": "image/png",
        "url": _data_url(_png(), "image/png"),
        "is_image": True,
    }


# ── 数量上限 ────────────────────────────────────────────────────────────


async def test_extras_collapse_into_a_single_notice():
    attachments = [_text_att(f"f{i}.txt", f"内容{i}") for i in range(MAX_ATTACHMENTS_PER_TURN + 3)]

    parts = await to_content_parts(attachments)

    notices = [p for p in parts if p["type"] == "text" and "上限" in p["text"]]
    assert len(notices) == 1, "超出数量上限应当只有一条汇总提示"
    assert "3 个" in notices[0]["text"], "要说出还有几个没处理"
    kept = [p for p in parts if p["type"] == "text" and "内容" in p["text"]]
    assert len(kept) == MAX_ATTACHMENTS_PER_TURN


async def test_same_limit_applies_to_text_entrypoint():
    attachments = [_text_att(f"f{i}.txt", f"内容{i}") for i in range(MAX_ATTACHMENTS_PER_TURN + 2)]

    out = await to_text(attachments)

    assert out.count("附件「") == MAX_ATTACHMENTS_PER_TURN
    assert "上限" in out


# ── 文本预算均分 ────────────────────────────────────────────────────────


def test_text_budget_shares_across_text_attachments_only():
    """图片不占字符预算：把它们算进分母，文本额度会凭空变少。

    额度还要被本轮**预览上限**压住：均分只保证总额不超 `MAX_TURN_ATTACHMENT_CHARS`，
    而本轮进上下文的只是预览（细节走 `attachment_read`）。
    """
    two_text = [_text_att("a.txt", "x"), _text_att("b.txt", "y")]
    with_images = [*two_text, _image_att("c.png"), _image_att("d.png")]

    assert _text_budget_for(two_text) == min(
        MAX_TURN_ATTACHMENT_CHARS // 2, MAX_ATTACHMENT_PREVIEW_CHARS
    )
    assert _text_budget_for(with_images) == _text_budget_for(two_text)


def test_text_budget_never_zero_but_capped():
    many = [_text_att(f"{i}.txt", "x") for i in range(MAX_ATTACHMENTS_PER_TURN)]
    assert _text_budget_for(many) >= 1, "额度为 0 等于静默不给内容"

    assert _text_budget_for([_text_att("one.txt", "x")]) >= 1


async def test_long_text_is_truncated_to_its_share_and_says_so():
    """两个超长附件：各自只拿一半预算，且**截断必须写出来**。"""
    body = "A" * (MAX_TURN_ATTACHMENT_CHARS)
    parts = await to_content_parts([_text_att("a.txt", body), _text_att("b.txt", body)])

    for part in parts:
        assert "已截断" in part["text"], "截断不标注 ⇒ 模型会把半篇当全文"
        assert "原文共" in part["text"]

    # 每份内容不超过它分到的额度（外加标题与截断说明的开销）
    share = MAX_TURN_ATTACHMENT_CHARS // 2
    assert all(part["text"].count("A") <= share for part in parts)


async def test_single_attachment_never_exceeds_per_file_cap():
    """预算再大也不能突破单附件上限，本轮预览还要再压到 `MAX_ATTACHMENT_PREVIEW_CHARS`。

    预览只是"这个文件大概是什么"，细节由 `attachment_read` 按需分片取 —— 本轮若给全文，
    就回到了"附件内容随轮次常驻上下文"的老问题。
    """
    body = "B" * (MAX_ATTACHMENT_CHARS * 2)
    parts = await to_content_parts([_text_att("big.txt", body)])

    assert parts[0]["text"].count("B") == MAX_ATTACHMENT_PREVIEW_CHARS
    assert MAX_ATTACHMENT_PREVIEW_CHARS <= MAX_ATTACHMENT_CHARS


# ── 图片张数上限 ────────────────────────────────────────────────────────


async def test_image_count_is_capped_before_fetching():
    model_capabilities.clear_cache()
    model_capabilities.remember_supports_vision("reader", True)
    attachments = [_image_att(f"p{i}.png") for i in range(MAX_IMAGES_PER_TURN + 2)]

    parts = await to_content_parts(attachments, model="reader")

    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == MAX_IMAGES_PER_TURN
    dropped = [p for p in parts if p["type"] == "text" and "张图片上限" in p["text"]]
    assert len(dropped) == 2
    assert all("vision_analyze" in p["text"] for p in dropped), "要给出取回通道"


async def test_text_entrypoint_caps_images_too():
    attachments = [_image_att(f"p{i}.png") for i in range(MAX_IMAGES_PER_TURN + 1)]

    out = await to_text(attachments)

    assert "张图片上限" in out
    assert out.count("本入口只能传文本") == MAX_IMAGES_PER_TURN
