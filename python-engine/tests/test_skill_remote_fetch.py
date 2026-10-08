"""A1 回归：`skill_discover` 与 `skill_install` 必须共用同一取回入口。

修复前 `skill_discover(url)` 直接 `httpx.get(url)` —— 既没有 SSRF 校验也没有体积
上限，而 `skill_install(url)` 两者都有。同一个 `url` 参数在两个工具上判定不同，
等于留了一条无校验的出网通道（可打云元数据 / 内网服务）。
"""
from __future__ import annotations

import httpx
import pytest

from app.tools.skill import _fetch_skill_json, skill_discover, skill_install

#: 三种典型的"内网/保留地址"目标：云元数据、回环、私网
BLOCKED_URLS = [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8080/skills.json",
    "http://10.0.0.5/skills.json",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BLOCKED_URLS)
async def test_discover_rejects_internal_url(url: str):
    """discover 此前无校验 —— 这是 A1 的主要缺口。"""
    out = await skill_discover(url=url)
    assert "error" in out
    assert "blocked" in out["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BLOCKED_URLS)
async def test_install_rejects_internal_url(url: str):
    """install 的既有行为不得因收敛到同一入口而回退。"""
    out = await skill_install(url=url)
    assert "error" in out
    assert "blocked" in out["error"]


@pytest.mark.asyncio
async def test_fetch_entry_raises_on_blocked_url():
    """唯一入口对非法 url 抛 ValueError（两个工具据此转成 error 字段）。"""
    with pytest.raises(ValueError):
        await _fetch_skill_json(BLOCKED_URLS[0])


# ── 体积上限：**流式**中止（N3）──
#
# 为什么要有这两条：原先的上限是"整段读完之后再判长度" —— 一个 1GB 的响应照样会被完整
# 收进内存，上限只拦住了"解析"。改成流式后必须**证明它真的提前停了**，否则等于没改。


class _CountingStream(httpx.AsyncByteStream):
    """记录"被真正拉取了几个块"，用来区分"提前停"与"读完了才判"。"""

    def __init__(self, chunk: bytes, total: int) -> None:
        self._chunk = chunk
        self._total = total
        self.pulled = 0

    async def __aiter__(self):
        for _ in range(self._total):
            self.pulled += 1
            yield self._chunk


def _patch_client(monkeypatch: pytest.MonkeyPatch, stream: httpx.AsyncByteStream) -> None:
    """让 `_fetch_skill_json` 内部的 AsyncClient 走 MockTransport（不改生产签名）。"""
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))


@pytest.mark.asyncio
async def test_fetch_aborts_streaming_once_over_cap(monkeypatch: pytest.MonkeyPatch):
    """越过上限必须**立即中止**：只拉了一小部分块，而不是读完 200 块。"""
    chunk = b"x" * 65_536  # 64 KiB/块 ⇒ 1 MiB 上限在第 17 块越界
    stream = _CountingStream(chunk, total=200)  # 总量 ~12.8 MiB，远超上限
    _patch_client(monkeypatch, stream)

    with pytest.raises(RuntimeError, match="too large"):
        await _fetch_skill_json("http://93.184.216.34/skills.json")

    assert stream.pulled < 30, (
        f"应在越界后立刻停止，实际拉取了 {stream.pulled}/200 块 —— 说明上限又变成"
        "“读完再判”了（下行流量与内存都没被限制）"
    )


@pytest.mark.asyncio
async def test_fetch_parses_within_cap(monkeypatch: pytest.MonkeyPatch):
    """上限之内照常解析出 JSON，且只拉一块。"""
    payload = b'{"name": "demo", "description": "ok"}'
    stream = _CountingStream(payload, total=1)
    _patch_client(monkeypatch, stream)

    out = await _fetch_skill_json("http://93.184.216.34/skills.json")
    assert out == {"name": "demo", "description": "ok"}
    assert stream.pulled == 1
