"""附件**不进历史**：会话缓存与 PG 只保存引用块。

这里的判据不是"压缩得够不够狠"，而是"内容有没有被持久化"。`SessionStore.append` 是
整表覆盖写（一次 `json.dumps(整个 messages)` 再 `setex`），所以只要内联图片或提取正文
留在了 messages 里，它就会被**每一轮**读回、重发给模型；实测单个 20MB 文件 10 轮累积
约 280MB。折成引用块后同样 10 轮约 0.0011MB。

本文件钉三件事：

1. 折叠后历史里**没有** `image_url` 块、没有 `data:` 载荷，只有 `<attachment id=… />`；
2. 折叠是**只读**的：调用后原 messages 不变，本轮送给模型的内容不受影响；
3. 没有 `asset_id` 的附件（纯 data URL 内联形态）不写引用 —— 那种内容不在媒体库里，
   写了也取不回来，但旁路标记必须被摘掉，不能漏给下游。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

from app.agent.runtime import (
    ATTACHMENT_PARTS_KEY,
    _with_attachment_parts,
    fold_attachment_parts_for_persistence,
)
from app.agent.runtime_types import AgentTask

RUNTIME = Path(__file__).resolve().parents[1] / "app" / "agent" / "runtime.py"


def _png() -> str:
    """真实可解码的 PNG —— `_image_data_url` 会 `sniff_mime` + `fit`，假字节会被判 unfit。"""
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


PAYLOAD = _png()

QUESTION = "看下这个图"


def _task(attachments: list[dict[str, object]] | None) -> AgentTask:
    return AgentTask(
        id="task-1",
        session_id="sess-1",
        content=QUESTION,
        tenant_id=7,
        user_id="u-1",
        attachments=attachments or [],
    )


def _image_att(asset_id: str) -> dict[str, object]:
    return {
        "id": asset_id,
        "name": f"{asset_id}.png",
        "mime_type": "image/png",
        "url": f"data:image/png;base64,{PAYLOAD}",
        "is_image": True,
    }


async def test_history_keeps_only_the_reference_not_the_bytes() -> None:
    att = _image_att("asset-1")
    messages = await _with_attachment_parts(
        [{"role": "user", "content": QUESTION}], _task([att]), model=""
    )
    assert any(p.get("type") == "image_url" for p in messages[-1]["content"]), (
        "前提：本轮注入后确实有图片块，否则这个测试什么都没证明"
    )

    folded = fold_attachment_parts_for_persistence(messages, [att])

    content = folded[-1]["content"]
    assert isinstance(content, list)
    assert not any(p.get("type") == "image_url" for p in content), "历史不得保留图片块"
    joined = "\n".join(str(p.get("text") or "") for p in content)
    assert "base64," not in joined, "历史不得保留内联载荷"
    assert '<attachment id="asset-1"' in joined
    assert 'name="asset-1.png"' in joined
    assert "attachment_read" in joined, "引用块必须给出续读出口"


async def test_folding_does_not_touch_the_live_messages() -> None:
    """折叠只用于写历史：本轮送给模型的内容必须原样保留。"""
    att = _image_att("asset-2")
    messages = await _with_attachment_parts(
        [{"role": "user", "content": QUESTION}], _task([att]), model=""
    )
    before = len(messages[-1]["content"])

    fold_attachment_parts_for_persistence(messages, [att])

    assert len(messages[-1]["content"]) == before
    assert any(p.get("type") == "image_url" for p in messages[-1]["content"])
    assert ATTACHMENT_PARTS_KEY in messages[-1]


async def test_messages_without_asset_id_are_left_alone() -> None:
    """内联附件（没有 id）不写引用，但旁路标记要被摘掉。"""
    att = {
        "name": "inline.png",
        "mime_type": "image/png",
        "url": f"data:image/png;base64,{PAYLOAD}",
        "is_image": True,
    }
    messages = await _with_attachment_parts(
        [{"role": "user", "content": QUESTION}], _task([att]), model=""
    )

    folded = fold_attachment_parts_for_persistence(messages, [att])

    assert ATTACHMENT_PARTS_KEY not in folded[-1]
    joined = "\n".join(str(p.get("text") or "") for p in folded[-1]["content"])
    assert "<attachment id=" not in joined, "没有 asset_id 就无从取回，不该写引用"


def test_session_cache_is_written_through_the_fold() -> None:
    """接线守卫：写缓存的那两处必须经过折叠。

    只测 `fold_attachment_parts_for_persistence` 是不够的 —— 函数正确而调用点漏掉了，
    内容照样会驻留（这正是本条要防的回归）。`code_of` 先剥注释再匹配，防止把调用
    注释掉后字面量仍在、守卫照样通过。
    """
    from tests.source_scan import code_of

    src = code_of(RUNTIME)
    appends = src.count("_session_store.append(")
    folded = src.count("fold_attachment_parts_for_persistence(messages, task.attachments)")

    assert appends == 2, f"缓存写入点应当是 2 处（正常退出 + 异常退出），实际 {appends}"
    assert folded == appends, (
        f"每处 `_session_store.append` 都必须写折叠后的副本（append={appends}, fold={folded}）"
    )
    assert "_session_store.append(\n                        task.session_id, messages)" not in src


async def test_reference_block_counts_unprocessed_attachments() -> None:
    """超出数量上限的附件要在引用块里说出来，否则用户以为全都处理了。"""
    from app.media.attachments import MAX_ATTACHMENTS_PER_TURN, attachment_reference_block

    atts = [_image_att(f"a{i}") for i in range(MAX_ATTACHMENTS_PER_TURN + 2)]
    block = attachment_reference_block(atts)

    assert block.count("<attachment id=") == MAX_ATTACHMENTS_PER_TURN
    assert "另有 2 个附件未处理" in block
