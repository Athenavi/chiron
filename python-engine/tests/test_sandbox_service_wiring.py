"""`SANDBOX_BACKEND=service` 的**接线**：配置 → 运行期真的生效。

背景：这段"配了 service 就把执行分流到独立服务"的逻辑此前内联在 `lifespan` 里、**零用例** ——
而"配置里写了却没人用"是这个仓库反复踩的坑（`vendor/规划.md` §4）。抽成
`_apply_sandbox_service_branch()` 之后，四条分支都能精确断言：

1. 默认（`local` / 没配 URL）⇒ **恒等**，一行行为都不变；
2. `service` + URL + 租户白名单 ⇒ 包装成 `RemoteExecBackend`，且**只有白名单租户**走服务；
3. `service` + URL + **空白名单** ⇒ 包装了但**谁都不走**（刻意的安全默认）；
4. `service` + URL + 不支持执行的默认后端 ⇒ 记 warning 并**保持恒等**（配置错误不静默）。
"""

import pytest

from app.backends.remote_exec import RemoteExecBackend
from app.config import settings
from app.main import _apply_sandbox_service_branch


class _FakeExecBackend:
    """最小可执行后端替身：只提供本接线用到的 `supports_execution()`。"""

    def __init__(self, executable: bool = True) -> None:
        self._executable = executable

    def supports_execution(self) -> bool:
        return self._executable


@pytest.fixture
def cfg(monkeypatch):
    def _set(*, backend: str, url: str = "", tenants: str = "", executable: bool = True):
        monkeypatch.setattr(settings, "sandbox_backend", backend, raising=False)
        monkeypatch.setattr(settings, "sandbox_service_url", url, raising=False)
        monkeypatch.setattr(settings, "sandbox_service_tenants", tenants, raising=False)
        monkeypatch.setattr(settings, "internal_token", "tk", raising=False)
        return _FakeExecBackend(executable)

    return _set


def test_default_local_backend_is_returned_as_is(cfg):
    base = cfg(backend="local", url="http://sandbox:9000")
    assert _apply_sandbox_service_branch(base) is base, "默认 local 必须恒等（零行为变化）"


def test_service_without_url_stays_local(cfg):
    """没配 URL 就无从分流 —— 必须保持本地，而不是包一个"每次都失败"的后端。"""
    base = cfg(backend="service", url="")
    assert _apply_sandbox_service_branch(base) is base


def test_service_routes_only_whitelisted_tenants(cfg):
    base = cfg(backend="service", url="http://sandbox:9000", tenants="t1, t2")
    wrapped = _apply_sandbox_service_branch(base)
    assert isinstance(wrapped, RemoteExecBackend)
    assert wrapped.should_use_service(tenant="t1") is True
    assert wrapped.should_use_service(tenant="t2") is True
    assert wrapped.should_use_service(tenant="t3") is False, "白名单外必须留在本地"
    assert wrapped.should_use_service(tenant="") is False, "无租户身份不得走服务"


def test_service_with_empty_tenant_whitelist_routes_nobody(cfg):
    """空白名单 = 谁都不走服务（刻意的安全默认，不是"没配就全走"）。"""
    base = cfg(backend="service", url="http://sandbox:9000", tenants="")
    wrapped = _apply_sandbox_service_branch(base)
    assert isinstance(wrapped, RemoteExecBackend)
    assert wrapped.should_use_service(tenant="t1") is False


def test_service_with_non_executable_backend_stays_local(cfg):
    """默认后端不支持执行时配 service 是**配置错误**：记 warning、不包装（否则包出一个永远失败的东西）。"""
    base = cfg(backend="service", url="http://sandbox:9000", tenants="t1", executable=False)
    assert _apply_sandbox_service_branch(base) is base
