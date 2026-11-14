"""`GET /v1/skills/discover` 的分桶语义。

修复前该端点把远端索引的结果**也塞进 ``local`` 桶**、``remote`` 恒为 ``[]`` ——
"发现"从 API 侧看等于不存在。这里钉住分桶规则，以及"失败语义与工具链一致"。

不启真实网络：`shared_client` 与 `fetch_url_safe` 都被替换成假实现，
本测试只关心**合并与分桶**，那是 `app.skill.discover` 的职责。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.api.skills import discover_skills
from app.skill.store import SkillDef, SkillStore

RemotePayload = list[dict[str, Any]]


class _FakeClient:
    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeResp:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def _patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, payload: Any) -> SkillStore:
    """装好一个含本地技能的 store，并把远端取回替换成固定 payload。"""
    store = SkillStore(root=tmp_path)
    store.save(SkillDef(name="local-one", description="已装的", version="1.0.0"))

    monkeypatch.setattr("app.api.skills._store_for", lambda *a, **k: store)
    monkeypatch.setattr("app.http_client.shared_client", lambda **kw: _FakeClient())

    async def _fake_fetch(client: Any, url: str, **kwargs: Any) -> _FakeResp:
        return _FakeResp(payload)

    monkeypatch.setattr("app.tools.ssrf.fetch_url_safe", _fake_fetch)
    return store


@pytest.mark.asyncio
async def test_discover_without_url_returns_local_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """不带 url 且未配置默认源 ⇒ 只列本地，且**不出网**。"""
    _patch(monkeypatch, tmp_path, [])

    out = await discover_skills(user_id="u1", tenant_id=1)

    assert [r["name"] for r in out["local"]] == ["local-one"]
    assert out["remote"] == []


@pytest.mark.asyncio
async def test_discover_puts_remote_entries_in_remote_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """修复的核心：远端条目必须进 ``remote``，不能混进 ``local``。

    混桶的后果是前端无法区分"已装"与"可装"，一键安装按钮失去判断依据。
    """
    payload: RemotePayload = [
        {"name": "remote-two", "description": "可装", "version": "2.0.0"},
        {"name": "local-one", "description": "远端的同名条目"},
    ]
    _patch(monkeypatch, tmp_path, payload)

    out = await discover_skills(url="https://index.example/skills.json", user_id="u1", tenant_id=1)

    assert [r["name"] for r in out["local"]] == ["local-one"]
    assert [r["name"] for r in out["remote"]] == ["remote-two"]
    # 同名时保留本地那条：远端不能"覆盖"已装状态
    assert out["local"][0]["installed"] is True
    assert out["remote"][0]["installed"] is False


@pytest.mark.asyncio
async def test_discover_drops_invalid_remote_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """远端 name 会进文件路径，非法形态必须丢弃（防路径注入）。"""
    payload: RemotePayload = [
        {"name": "../../etc/passwd"},
        {"name": "Upper-Case"},
        {"description": "no name"},
        {"name": "good-one"},
    ]
    _patch(monkeypatch, tmp_path, payload)

    out = await discover_skills(url="https://index.example/skills.json", user_id="u1", tenant_id=1)

    assert [r["name"] for r in out["remote"]] == ["good-one"]


@pytest.mark.asyncio
async def test_discover_rejects_non_array_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """显式 url 返回非数组 ⇒ 报错，而不是静默当成空索引。"""
    from fastapi import HTTPException

    _patch(monkeypatch, tmp_path, {"not": "an array"})

    with pytest.raises(HTTPException):
        await discover_skills(url="https://index.example/x.json", user_id="u1", tenant_id=1)


@pytest.mark.asyncio
async def test_discover_ssrf_block_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """SSRF 拦截必须表现为错误。降级成 warning 就成了不报错的盲探测通道。"""
    from fastapi import HTTPException

    store = SkillStore(root=tmp_path)
    monkeypatch.setattr("app.api.skills._store_for", lambda *a, **k: store)

    with pytest.raises(HTTPException) as exc:
        # 不 patch fetch：真实的 assert_safe_url 会拒绝内网地址
        await discover_skills(
            url="http://169.254.169.254/latest/meta-data/", user_id="u1", tenant_id=1
        )

    assert exc.value.status_code == 500
    assert "blocked" in str(exc.value.detail)
