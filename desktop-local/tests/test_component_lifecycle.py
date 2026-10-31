"""core 退出时必须停掉组件子进程（``app/main.py::lifespan`` 的 finally 段）。

为什么单独测：组件是 core 的**子进程**，core 退出后它们会继续占着端口和
``CHIRON_INFERENCE_PROXY`` 指向的 core 地址 —— 孤儿进程，下次启动撞端口。
``stop_all()`` 原本只在测试里被调用，生产路径没人调，这里把它钉住。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.main import create_app
from fastapi.testclient import TestClient

from tests.conftest import make_settings


class _RecordingManager:
    """只记录 ``stop_all`` 被调用过几次的替身（不需要真的组件）。"""

    def __init__(self) -> None:
        self.calls = 0

    def stop_all(self) -> list[dict[str, Any]]:
        self.calls += 1
        return []


def test_shutdown_stops_running_components(tmp_path: Path) -> None:
    """注入过管理器 → 退出时 stop_all 必须被调用一次。"""
    manager = _RecordingManager()
    app = create_app(make_settings(tmp_path))
    app.state.component_manager = manager

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert manager.calls == 0, "运行期间不该停组件"

    assert manager.calls == 1, "core 退出时必须停掉组件，否则留孤儿进程"


def test_shutdown_without_components_does_not_create_manager(tmp_path: Path) -> None:
    """从没碰过组件端点 → 退出时不新建管理器（省掉一次无谓的目录/锁初始化）。"""
    app = create_app(make_settings(tmp_path))

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200

    assert not hasattr(app.state, "component_manager")


def test_shutdown_survives_component_stop_failure(tmp_path: Path) -> None:
    """组件停不下来也不能让退出路径中断（存储仍需被关闭）。"""

    class _Boom:
        def stop_all(self) -> list[dict[str, Any]]:
            raise RuntimeError("组件停不下来")

    app = create_app(make_settings(tmp_path))
    app.state.component_manager = _Boom()

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200

    # 错误被记进 storage_errors 而不是抛出（TestClient 退出没炸即证明）
    assert any("components" in item for item in app.state.storage_errors)
