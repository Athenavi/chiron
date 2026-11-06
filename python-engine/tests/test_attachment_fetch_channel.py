"""附件取字节的**通道选择**：内部通道优先，URL 只在拿不到 `id`/身份时兜底。

为什么单独立一个文件：这条链路的三个分支在真实部署里只有一个能跑通 ——

* 前端给的 `url` 是相对路径（`/media/s/...`），引擎不知道主机名；
* S3 直链常指向内网端点，被引擎出站的 SSRF 防护拒绝；
* 只有 `GET /v1/internal/media/{id}/content`（带服务端身份）是可靠的。

而这三者的差别**只在真实部署里显现**：单测若全用 data URL，看起来一切正常，
上线后所有图片附件都变成"本次未能取回"。所以这里必须钉住"先走内部通道"。
"""

from __future__ import annotations

import httpx
import pytest

from app.media.attachments import to_text

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\x00\x00\x03"
    b"\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _Recorded:
    """记录引擎真正发出的请求 —— 断言"走哪条通道"只能靠它。"""

    def __init__(self, response: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response


def _patch_transport(monkeypatch: pytest.MonkeyPatch, recorded: _Recorded) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(recorded.handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
    )


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch: pytest.MonkeyPatch):
    """只改这三项配置，**不替换整个 `settings` 对象**。

    整替换会连带把 `redis_key_prefix` 等字段一起抹掉 —— 之前这么写，导入
    `app.tools` 时就在 `redis_keys.rkey` 里崩了。取字节函数内部用的是运行时
    `from app.config import settings`，所以必须改那个**对象本身**的属性。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "internal_token", "test-token")
    monkeypatch.setattr(settings, "gateway_internal_url", "http://gateway.internal:8080")


async def test_internal_channel_is_preferred_over_the_frontend_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """附件带 `id` + 服务端身份时，**只**打内部端点，不去碰前端给的 URL。"""
    recorded = _Recorded(
        httpx.Response(
            200,
            content="来源是内部通道".encode(),
            headers={"content-type": "text/plain; charset=utf-8"},
        )
    )
    _patch_transport(monkeypatch, recorded)

    # 用文本附件而不是图片：`to_text` 对图片只写"本入口只能传文本"的提示、
    # **不取字节**（那条分支的语义就是"不交给模型"），拿它测通道会永远 0 请求。
    out = await to_text(
        [{"id": "asset-1", "name": "n.txt", "mime_type": "text/plain",
          "url": "https://user-host.example/n.txt"}],
        tenant_id=7,
        user_id="u-1",
    )

    assert len(recorded.requests) == 1, "应当只发一次请求"
    req = recorded.requests[0]
    assert req.url.path == "/v1/internal/media/asset-1/content"
    assert req.url.params["tenant_id"] == "7"
    assert req.url.params["user_id"] == "u-1"
    assert req.headers["X-Internal-Token"] == "test-token"
    assert "来源是内部通道" in out
    assert "未能取回" not in out


async def test_internal_channel_failure_falls_back_to_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内部通道 404（资产不属于该用户）时，仍按 URL 兜底 —— 而不是直接判死。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(404, json={"error": "media not found"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    async def fake_fetch_safe(client, url, **_kwargs):
        # 回退路径必须仍经由 `fetch_url_safe`（SSRF 校验在它里面）。
        # 这里替换实现而不是替换 URL：真要解析公网域名就得联网，单元测试不允许。
        seen.append(httpx.URL(url).path)
        return httpx.Response(
            200, content=b"fallback text", headers={"content-type": "text/plain"}
        )

    monkeypatch.setattr("app.tools.ssrf.fetch_url_safe", fake_fetch_safe)

    out = await to_text(
        [{"id": "asset-2", "name": "n.txt", "mime_type": "text/plain",
          "url": "https://cdn.example.com/n.txt"}],
        tenant_id=7,
        user_id="u-1",
    )

    assert seen[:2] == ["/v1/internal/media/asset-2/content", "/n.txt"], seen
    assert "fallback text" in out


async def test_no_identity_skips_internal_channel_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有服务端身份时**不发**内部请求：网关侧三字段缺一即 400，
    发了也只是把一次必然失败的请求算进延迟（大文件尤其明显）。"""
    recorded = _Recorded(httpx.Response(200, content=b"x"))
    _patch_transport(monkeypatch, recorded)

    async def fake_fetch_safe(client, url, **_kwargs):
        return httpx.Response(200, content=b"n.txt body", headers={"content-type": "text/plain"})

    monkeypatch.setattr("app.tools.ssrf.fetch_url_safe", fake_fetch_safe)

    await to_text([{"id": "asset-3", "name": "n.txt", "mime_type": "text/plain",
                    "url": "https://cdn.example.com/n.txt"}])

    assert all("/internal/" not in r.url.path for r in recorded.requests)
