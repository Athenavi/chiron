"""Tests for mode_admin — 创造模式的模式定制能力。"""
from __future__ import annotations

import json

import pytest

from app.tools.mode_admin import mode_edit, mode_list


@pytest.mark.asyncio
async def test_mode_list_lists_all_modes():
    out = await mode_list()
    assert set(out["modes"].keys()) == {"normal", "minimal", "ptc", "creative"}
    minimal = out["modes"]["minimal"]
    assert minimal["enable_compaction"] is False
    assert minimal["include_tools"] == sorted({"read_file", "edit_file", "shell_exec"})
    assert "run_code" in out["modes"]["ptc"]["extra_tools"]


@pytest.mark.asyncio
async def test_mode_edit_merges_and_persists(tmp_path, monkeypatch):
    target = tmp_path / "mode_overrides.json"
    # 写入走 mode_admin._overrides_path，读取走 modes._overrides_path（生产同文件）
    monkeypatch.setattr("app.tools.mode_admin._overrides_path", lambda: target)
    monkeypatch.setattr("app.agent.modes._overrides_path", lambda: target)

    out = await mode_edit("minimal", {"include_tools": ["read_file", "edit_file", "shell_exec", "grep_files"]})
    assert out["updated"] is True
    assert "grep_files" in out["effective"]["include_tools"]

    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["minimal"]["include_tools"] == ["read_file", "edit_file", "shell_exec", "grep_files"]

    # 第二次编辑合并而非覆盖 persona
    await mode_edit("minimal", {"persona": "custom persona"})
    saved2 = json.loads(target.read_text(encoding="utf-8"))
    assert saved2["minimal"]["persona"] == "custom persona"
    assert "grep_files" in saved2["minimal"]["include_tools"]


@pytest.mark.asyncio
async def test_mode_edit_rejects_unknown_mode(tmp_path, monkeypatch):
    target = tmp_path / "mode_overrides.json"
    monkeypatch.setattr("app.tools.mode_admin._overrides_path", lambda: target)
    out = await mode_edit("bogus", {"persona": "x"})
    assert "error" in out and "unknown mode" in out["error"]


@pytest.mark.asyncio
async def test_mode_edit_rejects_unsupported_field(tmp_path, monkeypatch):
    target = tmp_path / "mode_overrides.json"
    monkeypatch.setattr("app.tools.mode_admin._overrides_path", lambda: target)
    out = await mode_edit("normal", {"temperature": 0.9})
    assert "error" in out and "unsupported" in out["error"]


@pytest.mark.asyncio
async def test_mode_edit_rejects_bad_tool_list(tmp_path, monkeypatch):
    target = tmp_path / "mode_overrides.json"
    monkeypatch.setattr("app.tools.mode_admin._overrides_path", lambda: target)
    out = await mode_edit("normal", {"include_tools": ["read_file", 42]})
    assert "error" in out
