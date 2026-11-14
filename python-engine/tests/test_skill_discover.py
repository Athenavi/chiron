"""`app.skill.discover` 的合并口径测试。

这层抽象存在的理由：工具链 `skill_discover` 与 HTTP `GET /v1/skills/discover`
此前各写一套发现逻辑，且 HTTP 侧把远端结果塞进 ``local`` 桶、``remote`` 恒为空 ——
同名能力两个入口两种真相。把规则收到一处后，这里钉住规则本身。

用鸭子类型的假 store（只实现 `list()`）：本模块的职责是**合并与校验**，
不涉及目录解析，没必要为每条断言准备真实文件树。
"""
from __future__ import annotations

from typing import Any

from app.skill.discover import (
    SKILL_NAME_RE,
    local_rows,
    merge_registry,
    render_lines,
    row_from_registry,
)
from app.skill.store import SkillDef


class _FakeStore:
    """只满足 `local_rows` 需要的最小接口。"""

    def __init__(self, skills: list[SkillDef]) -> None:
        self._skills = skills

    def list(self) -> list[SkillDef]:
        return list(self._skills)


def _store(*names: str) -> _FakeStore:
    return _FakeStore(
        [
            SkillDef(name=n, description=f"desc-{n}", version="1.0.0", author="me")
            for n in names
        ]
    )


def test_skill_name_regex_matches_install_rules():
    """技能名会进文件路径（`/skills/<name>/SKILL.md`），必须与 install 同口径。"""
    for ok in ("pdf", "a1", "summarize-text", "x" * 64):
        assert SKILL_NAME_RE.match(ok), ok
    for bad in ("", "-lead", "Upper", "has space", "../evil", "a/b", "x" * 65):
        assert not SKILL_NAME_RE.match(bad), bad


def test_local_rows_marks_installed_and_keeps_scope():
    rows = local_rows(_store("alpha"))
    assert rows["alpha"]["installed"] is True
    assert rows["alpha"]["description"] == "desc-alpha"
    # scope 用于前端"团队共享"徽标，不能被 source（exec 配置里的来源）顶掉
    assert "scope" in rows["alpha"]


def test_row_from_registry_rejects_bad_entries():
    """索引里混进坏数据是常态：逐条丢弃，不让整次发现失败。"""
    assert row_from_registry("not-a-dict", set()) is None
    assert row_from_registry({"name": "../evil"}, set()) is None
    assert row_from_registry({"name": ""}, set()) is None
    assert row_from_registry({"description": "no name"}, set()) is None


def test_row_from_registry_falls_back_to_origin():
    row = row_from_registry({"name": "remote-skill"}, set(), origin="https://idx/x.json")
    assert row is not None
    assert row["source"] == "https://idx/x.json"
    assert row["installed"] is False

    # 条目自带 source 时优先于索引地址（前端据此回溯真实来源）
    own = row_from_registry({"name": "r2", "source": "github:owner/repo"}, set(), origin="x")
    assert own is not None and own["source"] == "github:owner/repo"


def test_merge_puts_local_first_and_dedupes_by_name():
    """同名时保留本地那条（installed=True）—— 远端不能"覆盖"掉已装状态。"""
    store = _store("shared-name", "only-local")
    payload: list[Any] = [
        {"name": "shared-name", "description": "remote version"},
        {"name": "only-remote", "description": "new"},
    ]

    result = merge_registry(store, payload, limit=50)

    names = [r["name"] for r in result.rows]
    assert names == ["shared-name", "only-local", "only-remote"], names
    by_name = {r["name"]: r for r in result.rows}
    assert by_name["shared-name"]["installed"] is True
    assert by_name["shared-name"]["description"] == "desc-shared-name"
    assert by_name["only-remote"]["installed"] is False
    assert result.local_count == 2
    assert result.warnings == []


def test_merge_truncates_at_limit_and_reports_omitted():
    """远端索引可能几千条：截断必须留痕，否则模型以为那就是全部。"""
    store = _store("a", "b")
    payload = [{"name": f"r{i}"} for i in range(10)]

    result = merge_registry(store, payload, limit=5)

    assert len(result.rows) == 5
    # 本地 2 条先占位，远端只能进 3 条
    assert [r["name"] for r in result.rows[:2]] == ["a", "b"]
    assert result.warnings and "omitted" in result.warnings[0]


def test_merge_without_remote_is_pure_local():
    result = merge_registry(_store("solo"), [], limit=50)
    assert [r["name"] for r in result.rows] == ["solo"]
    assert result.warnings == []


def test_render_lines_shows_installed_marker():
    """工具返回值给模型看：已装的打勾，未装留空。"""
    rows = [
        {"name": "on", "description": "d1", "version": "1.0", "installed": True},
        {"name": "off", "description": "d2", "version": "2.0", "installed": False},
    ]
    text = render_lines(rows)
    assert "[✓] on" in text
    assert "[ ] off" in text
