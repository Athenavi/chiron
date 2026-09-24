"""Tests for agent modes — ModeConfig 配置表与回退行为。"""
from __future__ import annotations

import dataclasses
import json

import pytest

from app.agent.modes import (
    CORE_TOOL_NAMES,
    AgentMode,
    ModeConfig,
    get_mode_config,
)


class TestGetModeConfig:
    def test_unknown_mode_falls_back_to_normal(self):
        cfg = get_mode_config("bogus-mode")
        assert cfg.mode is AgentMode.NORMAL
        assert cfg.include_context is True
        assert cfg.enable_compaction is True

    def test_empty_mode_falls_back_to_normal(self):
        cfg = get_mode_config(None)
        assert cfg.mode is AgentMode.NORMAL

    def test_normal_mode_defaults(self):
        cfg = get_mode_config("normal")
        assert cfg.persona is None
        assert cfg.include_context is True
        assert cfg.include_tools == CORE_TOOL_NAMES
        assert cfg.extra_tools == frozenset()
        assert cfg.enable_compaction is True

    def test_minimal_mode(self):
        cfg = get_mode_config("minimal")
        assert cfg.persona == "You are a helpful software engineer assistant."
        assert cfg.include_context is False
        assert cfg.enable_compaction is False
        # 极简：仅 3 个工具，无 extra
        assert cfg.include_tools == frozenset({"read_file", "edit_file", "shell_exec"})
        assert cfg.extra_tools == frozenset()

    def test_ptc_mode_has_run_code(self):
        cfg = get_mode_config("ptc")
        assert "run_code" in cfg.extra_tools
        assert cfg.include_context is True

    def test_creative_mode_has_authoring_tools(self):
        cfg = get_mode_config("creative")
        assert "mode_list" in cfg.extra_tools
        assert "mode_edit" in cfg.extra_tools
        assert cfg.persona and "mode_edit" in cfg.persona

    def test_overrides_are_applied(self, tmp_path, monkeypatch):
        overrides = {
            "minimal": {
                "include_tools": ["read_file", "edit_file", "shell_exec", "grep_files"],
                "enable_compaction": True,
            }
        }
        p = tmp_path / "mode_overrides.json"
        p.write_text(json.dumps(overrides), encoding="utf-8")
        monkeypatch.setattr("app.agent.modes._overrides_path", lambda: p)

        cfg = get_mode_config("minimal")
        assert "grep_files" in cfg.include_tools
        assert cfg.enable_compaction is True
        # 未覆盖字段保持基值
        assert cfg.persona == "You are a helpful software engineer assistant."

    def test_broken_overrides_ignore(self, tmp_path, monkeypatch):
        p = tmp_path / "mode_overrides.json"
        p.write_text("{invalid json", encoding="utf-8")
        monkeypatch.setattr("app.agent.modes._overrides_path", lambda: p)

        cfg = get_mode_config("minimal")
        assert cfg.enable_compaction is False  # 回退基值

    def test_configs_are_frozen(self):
        cfg = get_mode_config("normal")
        assert isinstance(cfg, ModeConfig)
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.include_tools = frozenset()  # frozen dataclass 禁止赋值
