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
