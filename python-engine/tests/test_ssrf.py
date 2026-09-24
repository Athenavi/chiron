"""Tests for SSRF 防护（S4）与 skill_install 校验（S5）。"""
from __future__ import annotations

import json

import pytest

from app.skill.store import SkillStore
from app.tools.skill import skill_install
from app.tools.ssrf import assert_safe_url


class TestSSRF:
    def test_public_url_allowed(self):
        assert_safe_url("https://example.com/page")  # 不抛错

    def test_private_ip_blocked(self):
        with pytest.raises(ValueError, match="blocked"):
            assert_safe_url("http://127.0.0.1:8000/admin")

    def test_link_local_blocked(self):
        with pytest.raises(ValueError, match="blocked"):
            assert_safe_url("http://169.254.169.254/latest/meta-data/")

    def test_localhost_hostname_blocked(self):
        with pytest.raises(ValueError, match="blocked"):
            assert_safe_url("http://localhost:5432")

    def test_private_hostname_blocked(self):
        with pytest.raises(ValueError, match="blocked"):
            assert_safe_url("http://10.0.0.5/internal")

    def test_invalid_url_rejected(self):
        with pytest.raises(ValueError):
            assert_safe_url("not-a-url")


class TestSkillInstallValidation:
    @pytest.fixture(autouse=True)
    def _isolate_store(self, tmp_path, monkeypatch):
        from app.tools import skill as skill_mod
        monkeypatch.setattr(skill_mod, "_store", SkillStore(str(tmp_path)))
        monkeypatch.setattr(skill_mod, "_skill_root", str(tmp_path))
        yield

    @pytest.mark.asyncio
    async def test_valid_inline_installs(self):
        out = await skill_install(inline=json.dumps({
            "name": "git-helper", "description": "Git 辅助", "exec": {"type": "prompt", "source": "help {cmd}"},
        }))
        assert out.get("skill") == "git-helper"

    @pytest.mark.asyncio
    async def test_missing_name_rejected(self):
        out = await skill_install(inline=json.dumps({"description": "no name"}))
        assert "error" in out and "name" in out["error"]

    @pytest.mark.asyncio
    async def test_invalid_name_rejected(self):
        out = await skill_install(inline=json.dumps({"name": "../evil", "description": "x"}))
        assert "error" in out and "name" in out["error"]

    @pytest.mark.asyncio
    async def test_missing_description_rejected(self):
        out = await skill_install(inline=json.dumps({"name": "ok-name"}))
        assert "error" in out and "description" in out["error"]

    @pytest.mark.asyncio
    async def test_oversize_inline_rejected(self):
        big = json.dumps({"name": "big", "description": "x" * 2_000_000})
        out = await skill_install(inline=big)
        assert "error" in out and "too large" in out["error"]

    @pytest.mark.asyncio
    async def test_ssrf_blocked_url(self):
        out = await skill_install(url="http://169.254.169.254/latest/meta-data/")
        assert "error" in out and "blocked" in out["error"]
