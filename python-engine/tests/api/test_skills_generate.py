"""`POST /v1/skills/generate`（D4：接真 LLM + 规则校验 + 落盘复查回滚）。

此前这个端点是**假生成**：它把 description 折成名字、拼一个 `SkillDef` 返回，从不调用模型，
却对前端回"技能已生成并安装"。本文件锁死新的四条约定：

* 校验失败 ⇒ **400 + 具体原因**（前端要拿它提示；笼统的"生成失败"没法排查）；
* 成功 + `auto_install` ⇒ 技能真落盘，且**能被列出**（走 D1 的目录型读取）；
* 与工具 `skill_generate` **同源**（都调 `app/skill/generate.py`）；
* 无 LLM ⇒ 400（**不伪装**成生成成功）。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.gateway.provider import ChatResponse
from app.main import create_app
from app.skill.store import SkillStore

TENANT = "t1"
USER = "u1"


@pytest.fixture(autouse=True)
def _isolated_skill_root(tmp_path, monkeypatch):
    """技能根目录指到临时目录 —— 绝不污染真实 data/skills。"""
    monkeypatch.setenv("SKILL_STORE_PATH", str(tmp_path))
    return tmp_path


def _patch_gateway(monkeypatch: pytest.MonkeyPatch, raw: str = "") -> None:
    """注入假模型。

    `raw` 非空 ⇒ 原样返回它（用于构造**非法**输出）；否则返回一份合法 SKILL.md，其
    `name` 固定为 `summarize-text` —— 调用方应传与之匹配的 description（`slugify` 的结果
    要等于这个名字），否则会命中"frontmatter name ≠ 请求名"的校验（那正是我们要保护的行为）。
    """

    class _Gateway:
        async def chat(
            self, *, messages: list[dict[str, str]], model: str = "", max_tokens: int = 0
        ) -> ChatResponse:
            if raw:
                return ChatResponse(content=raw)
            return ChatResponse(
                content="---\nname: summarize-text\ndescription: 总结长文本\n---\n\n1. 读输入\n"
            )

    async def _get_gateway() -> _Gateway:
        return _Gateway()

    monkeypatch.setattr("app.main.get_gateway", _get_gateway)


def _patch_no_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _get_gateway() -> None:
        return None

    monkeypatch.setattr("app.main.get_gateway", _get_gateway)


async def _generate(**body: Any) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        return await client.post(
            "/v1/skills/generate",
            json=body,
            params={"user_id": USER, "tenant_id": TENANT},
        )


async def test_invalid_output_is_400_with_reason(monkeypatch):
    _patch_gateway(monkeypatch, "no frontmatter here")

    resp = await _generate(description="x")

    assert resp.status_code == 400
    assert "frontmatter" in resp.json()["detail"], "要给出**具体**原因"


async def test_without_llm_is_400(monkeypatch):
    _patch_no_gateway(monkeypatch)

    resp = await _generate(description="summarize text")

    assert resp.status_code == 400, "没有模型就明确失败，不能假装生成成功"


def _user_skills() -> list[str]:
    """用户层技能名。

    必须排除 `scope == "builtin"` 的条目：D3 的内置技能源（`market/skills/**`）总是出现在
    列表末尾，与 `SKILL_STORE_PATH` 无关 —— 它们不是"这次写进去的技能"。
    """
    return [
        s.name
        for s in SkillStore(tenant_id=TENANT, user_id=USER).list()
        if s.scope != "builtin"
    ]


async def test_auto_install_lands_on_disk_and_is_listable(monkeypatch):
    _patch_gateway(monkeypatch)

    resp = await _generate(description="summarize-text", auto_install=True)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["skill"]["name"] == "summarize-text"
    assert payload["scope"] == "user"
    assert payload["path"].replace("\\", "/").endswith("summarize-text/SKILL.md")
    assert "summarize-text" in _user_skills(), "装上就得能列出来（D1 的目录型读取）"


async def test_without_auto_install_nothing_is_written(monkeypatch):
    _patch_gateway(monkeypatch)

    resp = await _generate(description="summarize-text")

    assert resp.status_code == 200
    assert "path" not in resp.json(), "未安装时不该有落盘路径"
    assert _user_skills() == []


async def test_empty_description_is_400(monkeypatch):
    _patch_gateway(monkeypatch)

    resp = await _generate(description="   ")

    assert resp.status_code == 400
    assert "description" in resp.json()["detail"]
