"""`POST /v1/media/{id}/extract-metadata` 端点 —— 回归保护。

该端点此前是**纯占位**：`await get_auth_token(request)` 之后直接
`raise HTTPException(501, "Metadata extraction not yet implemented")`。

现在它从网关下载内容（`GET /v1/media/{id}/download`），按响应 MIME 映射成
analyzer 的 media_type 再解析。本文件用 duck-typed 的假请求/假客户端覆盖这条链路，
不需要真网关。
"""

from __future__ import annotations

import io

from app.api import media as media_api


class _FakeResponse:
    def __init__(self, content: bytes, mime: str, status: int = 200):
        self.content = content
        self.headers = {"content-type": mime}
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "boom", request=None, response=self  # type: ignore[arg-type]
            )


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        return self._response


class _FakeRequest:
    def __init__(self, token: str = "tok"):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.query_params: dict[str, str] = {}


def _patch_client(monkeypatch, response: _FakeResponse) -> None:
    async def fake_factory(_token: str) -> _FakeClient:
        return _FakeClient(response)

    monkeypatch.setattr(media_api, "create_http_client", fake_factory)


def _png(width: int = 4, height: int = 3) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height)).save(buf, format="PNG")
    return buf.getvalue()


async def test_endpoint_is_no_longer_501(monkeypatch):
    """核心回归：端点在图片上必须返回**真实解析结果**，而不是 501。"""
    _patch_client(monkeypatch, _FakeResponse(_png(width=4, height=3), "image/png"))

    out = await media_api.extract_metadata("m1", _FakeRequest())

    assert out["success"] is True
    assert out["type"] == "image"
    assert out["metadata"]["width"] == 4
    assert out["metadata"]["height"] == 3


async def test_endpoint_maps_document_mime(monkeypatch):
    _patch_client(
        monkeypatch, _FakeResponse(b"hello\nworld", "text/plain; charset=utf-8")
    )

    out = await media_api.extract_metadata("m2", _FakeRequest())

    assert out["success"] is True
    assert out["type"] == "document"
    assert out["metadata"]["format"] == "text"


async def test_endpoint_reports_av_unavailable(monkeypatch):
    """音视频 MIME 走到端点上也要如实报不可用（ffprobe 未安装）。"""
    _patch_client(monkeypatch, _FakeResponse(b"\x00", "video/mp4"))

    out = await media_api.extract_metadata("m3", _FakeRequest())

    assert out["success"] is False
    assert out["available"] is False
    assert out["type"] == "video"


def test_media_type_from_mime():
    cases = {
        "image/jpeg": "image",
        "image/png": "image",
        "video/mp4": "video",
        "audio/mpeg": "audio",
        "application/pdf": "document",
        "text/plain": "document",
        "": "document",
    }
    for mime, want in cases.items():
        assert media_api._media_type_from(mime) == want, mime
