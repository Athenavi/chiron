"""A1 回归：`skill_discover` 与 `skill_install` 必须共用同一取回入口。

修复前 `skill_discover(url)` 直接 `httpx.get(url)` —— 既没有 SSRF 校验也没有体积
上限，而 `skill_install(url)` 两者都有。同一个 `url` 参数在两个工具上判定不同，
等于留了一条无校验的出网通道（可打云元数据 / 内网服务）。
"""
from __future__ import annotations

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
