"""`verify_sandbox_backend` 的降级放行语义。

背景：单机模式下 `DEGRADED_MODE=true` 本应允许引擎在进程内执行（`main.py` 的
`_apply_sandbox_service_branch` 明确支持），但门禁在它**之前**跑，且只看
`os.getenv("SANDBOX_SERVICE_URL")` —— 于是"设了 DEGRADED_MODE 仍拒绝启动"，
表现为本地模式下引擎根本起不来。

这里锁定四条，缺一条都会让单机模式重新变成起不来 / 静默关隔离：
  1. 生产口径（DEGRADED_MODE 未设）：仍 fail-closed，拒绝启动；
  2. DEGRADED_MODE=true 且没服务地址：放行（回退进程内），并打 warning；
  3. DEGRADED_MODE=true 但给了服务地址：仍走服务，不放行成进程内；
  4. 非法取值：无论是否降级都拒绝（拼错一个字母等于关掉隔离）。
"""

from __future__ import annotations

import logging

import pytest

from app.deps import _degraded_mode, verify_sandbox_backend


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """每个用例从"什么都没设"开始，避免宿主机环境影响判定。"""
    for key in ("DEGRADED_MODE", "SANDBOX_BACKEND", "SANDBOX_SERVICE_URL"):
        monkeypatch.delenv(key, raising=False)


class TestDegradedModeReader:
    def test_env_true_variants(self, monkeypatch: pytest.MonkeyPatch):
        for value in ("true", "TRUE", "1", "yes", "on", " true "):
            monkeypatch.setenv("DEGRADED_MODE", value)
            assert _degraded_mode() is True, value

    def test_env_false_variants(self, monkeypatch: pytest.MonkeyPatch):
        for value in ("false", "0", "no", "off", ""):
            monkeypatch.setenv("DEGRADED_MODE", value)
            assert _degraded_mode() is False, value

    def test_falls_back_to_settings_when_env_missing(self, monkeypatch: pytest.MonkeyPatch):
        """环境变量没写时读 settings —— 覆盖 `.env` 里配了 DEGRADED_MODE 的情形。"""
        import app.config

        monkeypatch.setattr(app.config.settings, "degraded_mode", True, raising=False)
        assert _degraded_mode() is True


class TestSandboxBackendGate:
    def test_production_still_fails_closed(self):
        """未设 DEGRADED_MODE：保持 fail-closed，这是隔离没被静默关掉的保证。"""
        with pytest.raises(RuntimeError, match="SANDBOX_SERVICE_URL"):
            verify_sandbox_backend()

    def test_degraded_mode_allows_start_without_service_url(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("DEGRADED_MODE", "true")
        verify_sandbox_backend()  # 不抛即放行

    def test_degraded_mode_warns_about_in_process(self, monkeypatch: pytest.MonkeyPatch, caplog):
        monkeypatch.setenv("DEGRADED_MODE", "true")
        with caplog.at_level(logging.WARNING, logger="app.deps"):
            verify_sandbox_backend()
        text = caplog.text
        assert "degraded mode" in text
        # 必须说清"不是安全边界"，否则用户会以为隔离仍然生效
        assert "IN-PROCESS" in text

    def test_degraded_with_service_url_keeps_isolation(self, monkeypatch: pytest.MonkeyPatch):
        """降级不等于强制本地：给了服务地址仍应走隔离服务。"""
        monkeypatch.setenv("DEGRADED_MODE", "true")
        monkeypatch.setenv("SANDBOX_SERVICE_URL", "http://sandbox-lb:8001")
        verify_sandbox_backend()

    def test_invalid_backend_rejected_even_when_degraded(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("DEGRADED_MODE", "true")
        monkeypatch.setenv("SANDBOX_BACKEND", "serve")  # 少一个字母
        with pytest.raises(RuntimeError, match="invalid"):
            verify_sandbox_backend()

    def test_explicit_local_still_allowed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SANDBOX_BACKEND", "local")
        verify_sandbox_backend()
