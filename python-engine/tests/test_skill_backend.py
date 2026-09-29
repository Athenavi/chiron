"""`/skills/` 只读后端（D2 的读通道）。

挂载它的理由：技能目录在 `data/skills/**`，**不在** agent 沙箱里 —— 不给一条读通道，
目录注入里那句"用 read_file 读正文"就是空话。这里钉住三件事：**能读**、**越界会被拒**、
**写一律拒绝**（技能目录是部署资产，agent 改它等于绕过技能管理面）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.backends.skill import SkillBackend
from app.skill.store import SkillStore

SKILL_MD = """---
name: foo
description: 一个测试技能
---

# Foo 技能正文

按步骤做某事。
"""


@pytest.fixture
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SkillBackend:
    skill_dir = tmp_path / "foo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (skill_dir / "extra.md").write_text("附件", encoding="utf-8")
    (tmp_path / "outside.md").write_text("不该被读到", encoding="utf-8")
    monkeypatch.setattr("app.backends.skill._store", lambda: SkillStore(root=tmp_path))
    return SkillBackend()


# ── 能读 ────────────────────────────────────────────────────────────────


async def test_reads_skill_body(backend: SkillBackend):
    result = await backend.read("/skills/foo/SKILL.md")

    assert result.error is None
    assert "Foo 技能正文" in result.content


async def test_reads_attachment(backend: SkillBackend):
    result = await backend.read("/skills/foo/extra.md")
    assert result.error is None and "附件" in result.content


async def test_read_full_text_with_limit_zero(backend: SkillBackend):
    """`limit <= 0` = 读全文原文（与 workspace 后端同一语义）。"""
    result = await backend.read("/skills/foo/SKILL.md", limit=0)
    assert result.error is None and "name: foo" in result.content


async def test_lists_skill_names_and_files(backend: SkillBackend):
    root = await backend.ls("/skills/")
    assert root.error is None and "foo/" in root.entries

    inner = await backend.ls("/skills/foo/")
    assert inner.error is None
    assert "SKILL.md" in inner.entries and "extra.md" in inner.entries


async def test_stat_reports_size(backend: SkillBackend):
    info = await backend.stat("/skills/foo/SKILL.md")
    assert info is not None and info.size > 0 and info.is_file is True


async def test_read_bytes(backend: SkillBackend):
    result = await backend.read_bytes("/skills/foo/SKILL.md")
    assert result.error is None and b"foo" in result.data


# ── 越界与不存在 ────────────────────────────────────────────────────────


async def test_rejects_traversal(backend: SkillBackend):
    """`..` 逃出技能目录必须被拒（技能目录与工作区沙箱同一精神）。"""
    result = await backend.read("/skills/foo/../../outside.md")
    assert result.error is not None
    assert "escape" in result.error or "not found" in result.error


async def test_rejects_unknown_skill(backend: SkillBackend):
    result = await backend.read("/skills/ghost/SKILL.md")
    assert result.error is not None and "not found" in result.error


async def test_rejects_directory_path(backend: SkillBackend):
    """指向目录（没有 inner 文件）要明确报错，而不是读到空内容。"""
    result = await backend.read("/skills/foo")
    assert result.error is not None


# ── 只读（硬约束） ──────────────────────────────────────────────────────


async def test_write_is_refused(backend: SkillBackend):
    result = await backend.write("/skills/foo/SKILL.md", "hacked")
    assert result.error is not None and "read-only" in result.error


async def test_edit_is_refused(backend: SkillBackend):
    result = await backend.edit("/skills/foo/SKILL.md", "Foo", "Bar")
    assert result.error is not None and "read-only" in result.error


async def test_glob_and_grep_are_explicitly_unsupported(backend: SkillBackend):
    """明确"不支持"而不是静默空结果 —— 空结果会让模型以为"技能目录是空的"。"""
    glob_result = await backend.glob("**/*.md")
    grep_result = await backend.grep("Foo")
    assert glob_result.error and "not supported" in glob_result.error
    assert grep_result.error and "not supported" in grep_result.error


def test_does_not_support_execution(backend: SkillBackend):
    assert backend.supports_execution() is False
