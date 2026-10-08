"""Tests for fs_guard — read-before-write 观测策略。"""
from __future__ import annotations

from app.tools.fs_guard import _observed, check_before_write, observe


class TestFsGuard:
    def setup_method(self):
        _observed.clear()

    def test_unread_file_allowed(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        assert check_before_write(f) is None

    def test_read_then_unchanged_allowed(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        observe(f)
        assert check_before_write(f) is None

    def test_read_then_modified_rejected(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        observe(f)
        f.write_text("v2", encoding="utf-8")
        err = check_before_write(f)
        assert err is not None and "changed since" in err

    def test_read_then_deleted_rejected(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        observe(f)
        f.unlink()
        err = check_before_write(f)
        assert err is not None and "no longer exists" in err

    def test_observe_refreshes_version(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        observe(f)
        f.write_text("v2", encoding="utf-8")
        observe(f)  # 重新读取后版本刷新
        assert check_before_write(f) is None


class TestStrictMode:
    """严格模式（`fs_require_observation=True`）—— 开关**默认关**，故这些用例显式打开。

    对应 `vendor/规划.md` §3.3 的待决问题：是否要求"写已存在但从未读过的文件"也先读。
    机制先就绪（默认零行为变化），要不要开由产品决定。
    """

    def setup_method(self):
        _observed.clear()

    @staticmethod
    def _enable(monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "fs_require_observation", True, raising=False)

    def test_existing_unread_file_rejected(self, tmp_path, monkeypatch):
        self._enable(monkeypatch)
        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        err = check_before_write(f)
        assert err is not None and "never read" in err

    def test_creation_of_new_file_still_allowed(self, tmp_path, monkeypatch):
        """不存在的路径两种模式都放行：没有可过期的内容，新建本就没有"读"可做。"""
        self._enable(monkeypatch)
        assert check_before_write(tmp_path / "brand-new.txt") is None

    def test_read_then_unchanged_allowed(self, tmp_path, monkeypatch):
        self._enable(monkeypatch)
        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        observe(f)
        assert check_before_write(f) is None

    def test_read_then_modified_still_rejected(self, tmp_path, monkeypatch):
        """严格模式不放松原有语义：读过之后被改动仍然拒绝。"""
        self._enable(monkeypatch)
        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        observe(f)
        f.write_text("v2", encoding="utf-8")
        err = check_before_write(f)
        assert err is not None and "changed since" in err

    def test_default_mode_is_unchanged(self, tmp_path, monkeypatch):
        """开关默认关 ⇒ 行为与旧版**逐字一致**（未读过的文件放行）。"""
        from app.config import settings

        monkeypatch.setattr(settings, "fs_require_observation", False, raising=False)
        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")
        assert check_before_write(f) is None
