"""D4：`skill_generate` 接真 LLM（方案 02 §5）。

四条约定：

* **无 LLM 时明确报错** —— 不降级为"模板生成"（那会把"模型没配好"伪装成"技能已生成"）；
* **生成物先校验、后落盘** —— 复用 D1 的规则（frontmatter 的 `name` 必须等于目录名、
  description 有上限、正文非空）；
* **落盘后复查 + 失败回滚** —— 宁可什么都不留，也不留半个坏技能；
* **两条路径同源**：工具 `skill_generate` 与 HTTP `/v1/skills/generate` 都走 `generate_skill_md`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.gateway.provider import ChatResponse
from app.skill import generate as gen
from app.skill.generate import extract_markdown, generate_skill_md, slugify
from app.skill.store import SkillStore

VALID_MD = """---
name: summarize-text
description: 总结长文本
---

1. 读输入
2. 输出要点
"""


class _FakeGateway:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict[str, Any]] = []

    async def chat(self, *, messages: Any, model: str = "", max_tokens: int = 0) -> ChatResponse:
        self.calls.append({"messages": messages, "model": model})
        return ChatResponse(content=self._text)


def _patch_gateway(monkeypatch: pytest.MonkeyPatch, text: str) -> _FakeGateway:
    gateway = _FakeGateway(text)

    async def _get_gateway() -> _FakeGateway:
        return gateway

    monkeypatch.setattr("app.main.get_gateway", _get_gateway)
    return gateway


def _patch_no_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _get_gateway() -> None:
        return None

    monkeypatch.setattr("app.main.get_gateway", _get_gateway)


# ── 名字与抽取 ──────────────────────────────────────────────────────────


def test_slugify_keeps_safe_chars():
    assert slugify("Summarize Text!") == "summarize_text"
    assert slugify("  a  b  ") == "a_b"
    assert slugify("中文描述") == "generated_skill", "全非法字符 ⇒ 回落到默认名"
    assert len(slugify("x" * 100)) <= gen.SLUG_MAX_CHARS


def test_extract_markdown_strips_fences():
    assert extract_markdown("```markdown\n---\nname: x\n---\nbody\n```").startswith("---")
    assert extract_markdown("```\nbody only\n```") == "body only"
    assert extract_markdown("no fence") == "no fence"


# ── 无 LLM：明确报错，不假生成 ──────────────────────────────────────────


async def test_without_gateway_reports_error(monkeypatch):
    _patch_no_gateway(monkeypatch)

    outcome = await generate_skill_md(description="summarize text")

    assert outcome.ok is False
    assert "no content" in outcome.error or "LLM" in outcome.error


async def test_empty_description_is_rejected(monkeypatch):
    _patch_no_gateway(monkeypatch)
    outcome = await generate_skill_md(description="   ")
    assert outcome.ok is False and "description" in outcome.error


# ── 生成成功 ────────────────────────────────────────────────────────────


async def test_generates_and_parses(monkeypatch):
    gateway = _patch_gateway(monkeypatch, VALID_MD)

    outcome = await generate_skill_md(description="summarize long text", name="summarize-text")

    assert outcome.ok is True
    assert outcome.name == "summarize-text"
    assert outcome.skill["description"] == "总结长文本"
    assert len(gateway.calls) == 1, "应当只调一次 LLM"
    # 提示词里必须带上名字（否则模型会自己编一个，name 校验必然失败）
    prompt = str(gateway.calls[0]["messages"])
    assert "summarize-text" in prompt


async def test_name_derived_from_description(monkeypatch):
    md = VALID_MD.replace("summarize-text", "summarize_text")
    _patch_gateway(monkeypatch, md)

    outcome = await generate_skill_md(description="Summarize Text")

    assert outcome.ok is True and outcome.name == "summarize_text"


# ── 非法输出被拒（附原因） ──────────────────────────────────────────────


async def test_name_mismatch_is_rejected(monkeypatch):
    """与 `load_skill_dir` 同一条硬规则：frontmatter 的 name 必须等于请求的名字。"""
    _patch_gateway(monkeypatch, VALID_MD)

    outcome = await generate_skill_md(description="x", name="other-name")

    assert outcome.ok is False
    assert "!=" in outcome.error and "other-name" in outcome.error


async def test_missing_frontmatter_is_rejected(monkeypatch):
    _patch_gateway(monkeypatch, "just a body without frontmatter")

    outcome = await generate_skill_md(description="x", name="n")

    assert outcome.ok is False and "frontmatter" in outcome.error


async def test_missing_description_is_rejected(monkeypatch):
    _patch_gateway(monkeypatch, "---\nname: n\n---\nbody\n")

    outcome = await generate_skill_md(description="x", name="n")

    assert outcome.ok is False and "description" in outcome.error


async def test_empty_body_is_rejected(monkeypatch):
    _patch_gateway(monkeypatch, "---\nname: n\ndescription: d\n---\n\n")

    outcome = await generate_skill_md(description="x", name="n")

    assert outcome.ok is False and "body" in outcome.error


async def test_overlong_description_is_rejected(monkeypatch):
    from app.skill.skillmd import MAX_DESCRIPTION_CHARS

    long_desc = "d" * (MAX_DESCRIPTION_CHARS + 1)
    _patch_gateway(monkeypatch, f"---\nname: n\ndescription: {long_desc}\n---\nbody\n")

    outcome = await generate_skill_md(description="x", name="n")

    assert outcome.ok is False and "too long" in outcome.error


async def test_invalid_name_is_rejected_before_calling_llm(monkeypatch):
    gateway = _patch_gateway(monkeypatch, VALID_MD)

    outcome = await generate_skill_md(description="x", name="../escape")

    assert outcome.ok is False and "invalid skill name" in outcome.error
    assert gateway.calls == [], "名字非法时不该白白调一次模型"


# ── 落盘 + 复查 + 回滚 ──────────────────────────────────────────────────


async def test_install_writes_and_is_loadable(monkeypatch, tmp_path: Path):
    """D4 验收：生成的技能**可立即被列出并通过校验**。"""
    _patch_gateway(monkeypatch, VALID_MD)
    store = SkillStore(root=tmp_path)

    outcome = await generate_skill_md(
        description="summarize long text", name="summarize-text", install=True, store=store
    )

    assert outcome.ok is True
    assert (tmp_path / "summarize-text" / "SKILL.md").is_file()
    names = [s.name for s in store.list()]
    assert "summarize-text" in names, "落盘后必须能被列出（走的是 D1 的目录型读取）"


async def test_install_rolls_back_on_post_write_failure(monkeypatch, tmp_path: Path):
    """复查失败 ⇒ 回滚：**不能留下半个坏技能**。"""
    _patch_gateway(monkeypatch, VALID_MD)
    store = SkillStore(root=tmp_path)

    def _boom(_path: Path) -> Any:
        raise ValueError("simulated post-write validation failure")

    monkeypatch.setattr("app.skill.skillmd.load_skill_dir", _boom)

    outcome = await generate_skill_md(
        description="x", name="summarize-text", install=True, store=store
    )

    assert outcome.ok is False and "validation on load" in outcome.error
    assert not (tmp_path / "summarize-text").exists(), "复查失败必须回滚"


async def test_invalid_output_never_touches_disk(monkeypatch, tmp_path: Path):
    _patch_gateway(monkeypatch, "no frontmatter here")
    store = SkillStore(root=tmp_path)

    outcome = await generate_skill_md(description="x", name="n", install=True, store=store)

    assert outcome.ok is False
    assert not (tmp_path / "n").exists(), "校验不通过 ⇒ 一个字节都不该写"
