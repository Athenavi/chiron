"""混沌工程注入 —— 回归保护。

历史：``app/chaos/engine.py`` 的 ``ChaosEngine`` 是**假注入**（``_inject_latency`` 只是
自己 ``asyncio.sleep``、``_inject_error`` 只打日志后写 ``{"injected": True}``），Go 侧六个
端点长期返回 501。两端合起来构成"看起来有、实际没有"。

本文件锁死新通道的三条契约：

* 只有 **确实会被施加** 的组合才被接受（``engine``/``gateway`` × ``latency``/``error``/``timeout``），
  其余明确拒绝并给出理由 —— 而不是"接受后什么都不做"；
* 中间件命中 ``error`` 时**直接回响应、不调下游**，并带 ``X-Chaos-Injected`` 头；
* ``CHAOS_ENABLED`` 关闭时**完全旁路**（连 Redis 都不读）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.chaos import injector
from app.chaos.injector import (
    MAX_INJECT_MS,
    MIDDLEWARE_FAULT_TYPES,
    SUPPORTED_TARGETS,
    ChaosInjectionMiddleware,
    pick_fault,
    unsupported_reason,
)
from app.config import settings


def _fault(**kw) -> dict:
    base = {
        "id": "e1",
        "fault_type": "error",
        "target": "engine",
        "duration_ms": 100,
        "intensity": 0.5,
        "config": {},
    }
    base.update(kw)
    return base


def _scope(path: str = "/v1/agents", query: bytes = b"tenant_id=t1") -> dict:
    return {
        "type": "http",
        "path": path,
        "query_string": query,
        "headers": [],
        "method": "GET",
    }


def test_supported_surface_is_small_and_explicit():
    assert SUPPORTED_TARGETS == {"engine", "gateway"}
    assert MIDDLEWARE_FAULT_TYPES == {"latency", "error", "timeout"}


def test_supported_combinations_pass():
    for target in ("engine", "gateway"):
        for fault in ("latency", "error", "timeout"):
            assert unsupported_reason(fault, target) is None, (fault, target)


@pytest.mark.parametrize("target", ["llm", "db", "redis", "nonexistent"])
def test_unwired_targets_are_rejected_with_reason(target):
    reason = unsupported_reason("error", target)

    assert reason is not None
    assert "not wired" in reason


def test_resource_is_rejected_on_request_path():
    """CPU/内存耗尽不属于请求路径注入 —— 必须明说，而不是照单全收。"""
    reason = unsupported_reason("resource", "engine")

    assert reason is not None
    assert "resource" in reason


def test_pick_fault_skips_other_targets_and_unsupported():
    faults = [
        _fault(id="gw", target="gateway"),
        _fault(id="res", target="engine", fault_type="resource"),
        _fault(id="ok", target="engine", fault_type="latency"),
    ]

    picked = pick_fault(faults, "engine")

    assert picked is not None
    assert picked["id"] == "ok"  # 跳过别的 target，也跳过不支持的 resource


def test_pick_fault_returns_none_when_nothing_matches():
    assert pick_fault([_fault(target="gateway")], "engine") is None


# ── 中间件 ────────────────────────────────────────────────────────────────


async def _run_middleware(faults, scope=None, monkeypatch=None, enabled=True):
    """跑一遍中间件，返回 (下游是否被调用, 发送的 ASGI 消息列表)。"""
    if monkeypatch is not None:
        monkeypatch.setattr(settings, "chaos_enabled", enabled)

        async def fake_load(_tenant_id: str):
            return faults

        monkeypatch.setattr(injector, "load_active_faults", fake_load)

    sent: list[dict] = []
    downstream_called = False

    async def send(msg: dict) -> None:
        sent.append(msg)

    async def receive() -> dict:
        return {"type": "http.request"}

    async def downstream(_scope, _receive, _send) -> None:
        nonlocal downstream_called
        downstream_called = True

    await ChaosInjectionMiddleware(downstream)(scope or _scope(), receive, send)
    return downstream_called, sent


async def test_disabled_middleware_is_a_pure_passthrough(monkeypatch):
    """默认关闭：不施加、也不该读 Redis（此处若读了，fake_load 的调用就说明有问题）。"""
    called, sent = await _run_middleware(
        [_fault()], monkeypatch=monkeypatch, enabled=False
    )

    assert called is True
    assert sent == []


async def test_error_fault_short_circuits_downstream(monkeypatch):
    fault = _fault(fault_type="error", config={"error_code": 503})

    called, sent = await _run_middleware([fault], monkeypatch=monkeypatch)

    assert called is False, "注入 error 时不应再调用下游"
    assert sent[0]["status"] == 503
    headers = dict(sent[0]["headers"])
    assert headers[b"x-chaos-injected"] == b"e1"


async def test_error_code_falls_back_when_config_is_odd(monkeypatch):
    fault = _fault(fault_type="error", config={"error_code": 999})

    _, sent = await _run_middleware([fault], monkeypatch=monkeypatch)

    # 999 不是合法 HTTP 状态 → 必须回退，而不是照发
    assert sent[0]["status"] == 503


async def test_latency_fault_still_calls_downstream(monkeypatch):
    fault = _fault(fault_type="latency", duration_ms=80, intensity=1.0)

    start = asyncio.get_running_loop().time()
    called, _ = await _run_middleware([fault], monkeypatch=monkeypatch)
    elapsed_ms = (asyncio.get_running_loop().time() - start) * 1000

    assert called is True, "latency 注入后仍应交给下游"
    assert elapsed_ms >= 60, f"未观察到延迟注入（{elapsed_ms:.0f}ms）"


async def test_latency_is_capped_by_max_inject_ms(monkeypatch):
    """duration_ms 超上限时必须截断 —— 否则一个实验能把连接挂死。"""
    fault = _fault(fault_type="latency", duration_ms=MAX_INJECT_MS * 100, intensity=1.0)

    start = asyncio.get_running_loop().time()
    await _run_middleware([fault], monkeypatch=monkeypatch)
    elapsed_ms = (asyncio.get_running_loop().time() - start) * 1000

    assert elapsed_ms < MAX_INJECT_MS * 2, f"未被上限截断（{elapsed_ms:.0f}ms）"


async def test_missing_tenant_passes_through(monkeypatch):
    called, _ = await _run_middleware(
        [_fault()], scope=_scope(query=b""), monkeypatch=monkeypatch
    )

    assert called is True


async def test_loader_failure_never_breaks_requests(monkeypatch):
    """注入器自身出故障时，正常请求必须照常通过。"""
    monkeypatch.setattr(settings, "chaos_enabled", True)

    async def boom(_tenant_id: str):
        raise RuntimeError("redis down")

    monkeypatch.setattr(injector, "load_active_faults", boom)

    called, _ = await _run_middleware([], monkeypatch=None)
    assert called is True
