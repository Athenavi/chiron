"""内置技能源（`market/skills`）的接线与「缺失必须可见」。

两条独立的坑，各自钉一条用例：

1. **接线**：`SkillStore` 的搜索路径末尾必须真的挂上内置源，且 `market/skills/` 下每个
   目录型技能都要被加载 —— 有一个 SKILL.md 非法就会被 `SkillStore` 跳过（只留一条
   `_warnings`），模型于是悄悄少一个技能。
2. **缺失可见**：源不存在时 `builtin_skills_root()` 返回 `None`（不能抛，内置源缺失不该
   让技能列表报错），但**必须告警**。此前它是静默的，正好掩盖了部署缺陷：引擎镜像只
   `COPY app/`，`market/skills/` 进不了镜像 ⇒ 容器里恒无内置技能而无人察觉
   （见 vendor/规划.md §1.3 与 docs/development-roadmap.md）。
"""
from __future__ import annotations

import logging

from app.skill import skillmd
from app.skill.store import SCOPE_BUILTIN, SkillStore


def _builtin_names() -> set[str]:
    return {s.name for s in SkillStore(tenant_id="t1", user_id="u1").list() if s.scope == SCOPE_BUILTIN}


def test_repo_checkout_resolves_builtin_source():
    root = skillmd.builtin_skills_root()
    assert root is not None, "仓库检出下必须能解析出 market/skills（否则内置技能全丢）"
    assert root.is_dir() and root.name == "skills"


def test_every_market_skill_dir_is_loaded():
    """`market/skills/` 里每个含 SKILL.md 的目录都必须出现在技能列表里。

    这条比"至少有内置技能"更精确：它能抓到「某个 SKILL.md 写坏了被静默跳过」。
    """
    root = skillmd.builtin_skills_root()
    assert root is not None
    dirs = skillmd.iter_skill_dirs(root)
    assert dirs, "market/skills 下应有目录型技能（SKILL.md）"

    loaded = _builtin_names()
    missing = sorted(d.name for d in dirs if d.name not in loaded)
    assert not missing, f"这些内置技能目录没有被加载（SKILL.md 非法？）：{missing}"


def test_env_override_replaces_builtin_source(tmp_path, monkeypatch):
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: a demo skill for tests\n---\n\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(skillmd.BUILTIN_SKILLS_ENV, str(tmp_path))
    monkeypatch.setattr(skillmd, "_MISSING_WARNED", set())

    assert skillmd.builtin_skills_root() == tmp_path
    assert _builtin_names() == {"demo-skill"}


def test_missing_builtin_source_warns_once(tmp_path, monkeypatch, caplog):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv(skillmd.BUILTIN_SKILLS_ENV, str(missing))
    monkeypatch.setattr(skillmd, "_MISSING_WARNED", set())

    with caplog.at_level(logging.WARNING, logger="app.skill.skillmd"):
        assert skillmd.builtin_skills_root() is None
        assert skillmd.builtin_skills_root() is None  # 第二次不该再刷屏

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"缺失应恰好告警一次，实际 {len(warnings)}"
    assert skillmd.BUILTIN_SKILLS_ENV in caplog.text


def test_default_source_missing_warns(tmp_path, monkeypatch, caplog):
    """复现容器里的真实情形：没有环境变量，`parents[3]/market/skills` 也不存在。

    引擎镜像只 `COPY app/`，`__file__` 是 `/app/app/skill/skillmd.py`，于是反推出的
    `parents[3]` 是镜像根 `/`，`/market/skills` 不存在。这里用同样的相对位移模拟。
    """
    fake_module = tmp_path / "app" / "skill" / "skillmd.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("", encoding="utf-8")

    monkeypatch.delenv(skillmd.BUILTIN_SKILLS_ENV, raising=False)
    monkeypatch.setattr(skillmd, "__file__", str(fake_module))
    monkeypatch.setattr(skillmd, "_MISSING_WARNED", set())

    with caplog.at_level(logging.WARNING, logger="app.skill.skillmd"):
        assert skillmd.builtin_skills_root() is None
    assert "market" in caplog.text

