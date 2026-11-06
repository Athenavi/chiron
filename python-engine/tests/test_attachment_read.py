"""`attachment_read` —— 按 asset_id 分段取回附件正文。

**守的是什么**：附件不再随历史驻留内容（历史里只有 `<attachment id="…" />`），
所以这个工具是"读过的文件还能再读"的**唯一出口**。两条不变量：

1. **分段**：单次返回不超过调用方给的 limit（封顶 `MAX_LIMIT`），否则"取回"就成了
   换个名字的驻留 —— 一次把全文灌进上下文，下一轮它又整段被重发；
2. **归属 fail-closed**：身份取自工具上下文而不是参数，没有已鉴权的租户/用户时
   **不发请求**。模型不能通过传参以别人的身份取字节。
"""

from __future__ import annotations

import pytest

from app.tools import attachment_read as mod
from app.tools.context import set_tool_context


@pytest.fixture(autouse=True)
def _identity() -> None:
    set_tool_context(tenant_id=7, user_id="u-1")


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, payload: tuple[bytes, str, str] | None) -> list[str]:
    calls: list[str] = []

    async def fake(asset_id: str, tenant_id: int, user_id: str, **_kw: object):  # type: ignore[no-untyped-def]
        calls.append(asset_id)
        return payload

    monkeypatch.setattr("app.media.attachments._fetch_asset_bytes", fake)
    return calls


TEXT = "".join(str(i % 10) for i in range(100))
FILE = (TEXT.encode(), "text/plain", "notes.txt")


async def test_pages_through_the_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch(monkeypatch, FILE)

    first = await mod.attachment_read("a1", offset=0, limit=10)

    assert first["content"] == TEXT[:10]
    assert first["total_chars"] == len(TEXT)
    assert first["returned_chars"] == 10
    assert "offset=10" in first["note"], "必须给出续读位置，否则模型只能从头再来"

    second = await mod.attachment_read("a1", offset=10, limit=10)
    assert second["content"] == TEXT[10:20]


async def test_last_page_says_it_is_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetch(monkeypatch, FILE)

    out = await mod.attachment_read("a1", offset=90, limit=50)

    assert out["content"] == TEXT[90:]
    assert "结尾" in out["note"]


async def test_limit_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过上限的 limit 要被夹住 —— 否则调用方一个笔误就把全文灌进上下文。"""
    _patch_fetch(monkeypatch, (b"x" * (mod.MAX_LIMIT + 5000), "text/plain", "big.txt"))

    out = await mod.attachment_read("a1", limit=10**9)

    assert out["returned_chars"] == mod.MAX_LIMIT


async def test_requires_authenticated_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_fetch(monkeypatch, FILE)
    set_tool_context(tenant_id=0, user_id="")

    out = await mod.attachment_read("a1")

    assert "error" in out
    assert calls == [], "没有身份就不能发请求（fail-closed），而不是让网关拒一次"


async def test_reports_unavailable_without_pretending_it_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fetch(monkeypatch, None)

    out = await mod.attachment_read("missing")

    assert "error" in out
    assert "missing" in out["error"]


async def test_binary_without_text_points_at_the_right_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """图片取不回文本不是故障，但要让模型知道该换哪个工具，而不是以为文件是空的。"""
    _patch_fetch(monkeypatch, (b"\x89PNG\r\n\x1a\ndata", "image/png", "shot.png"))

    out = await mod.attachment_read("img")

    assert "vision_analyze" in out["error"]


async def test_rejects_empty_asset_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_fetch(monkeypatch, FILE)

    out = await mod.attachment_read("  ")

    assert "error" in out
    assert calls == []
