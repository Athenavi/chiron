"""组件进程监督：崩溃自动拉起、主动停止不拉起、崩溃循环达上限后放弃。

为什么单独测：监督是**兜底**逻辑，跑在后台线程里，出错时表现为"组件莫名又活了"或
"组件死了没人管"——两种都不容易在手工测试里发现，必须钉住策略本身。

测试直接调 ``supervise_once()``（同步方法），不等后台线程的节拍，因此既确定又快。
退避延迟用 monkeypatch 压到 0，避免每个用例真等 1+2+4 秒。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from app.components import ComponentManager
from app.components import manager as manager_module

from tests.test_components import SLEEP_SCRIPT, build_source, make_manager

#: 起来就退出，模拟组件崩溃
CRASH_SCRIPT = """\
import sys
sys.exit(3)
"""


@pytest.fixture(autouse=True)
def _fast_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """把退避压到 0：测的是"该不该重启"，不是"等了几秒"。"""
    monkeypatch.setattr(manager_module, "RESTART_BACKOFF", (0.0, 0.0, 0.0))


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "source"
    path.mkdir()
    return path


def _wait_exit(manager: ComponentManager, name: str) -> None:
    """等子进程真的退出（``start()`` 返回时它可能还没退）。"""
    record = manager._records.get(name)
    if record is not None:
        record.child.wait(timeout=10)


def _item(manager: ComponentManager, name: str) -> dict:
    return next(item for item in manager.list() if item["name"] == name)


def _install_crashing(tmp_path: Path, source: Path) -> ComponentManager:
    build_source(source, "pageindex", {"app.py": CRASH_SCRIPT.encode("utf-8")})
    manager = make_manager(tmp_path, source)
    manager.install("pageindex")
    return manager


def test_crashed_component_is_restarted(tmp_path: Path) -> None:
    """组件崩了 → 监督拉起，且重启次数如实上报。"""
    manager = _install_crashing(tmp_path, _source(tmp_path))
    try:
        manager.start("pageindex")
        first_pid = _item(manager, "pageindex")["pid"]
        _wait_exit(manager, "pageindex")

        restarted = manager.supervise_once()

        assert restarted == ["pageindex"], "崩溃的组件必须被自动拉起"
        assert _item(manager, "pageindex")["pid"] != first_pid, "重启必须是新进程"
        assert _item(manager, "pageindex")["restarts"] == 1
    finally:
        manager.stop_all()


def test_stopped_component_is_not_restarted(tmp_path: Path) -> None:
    """用户点"停止"后，监督线程不能把它拉回来。"""
    source = _source(tmp_path)
    build_source(source, "pageindex", {"app.py": SLEEP_SCRIPT.encode("utf-8")})
    manager = make_manager(tmp_path, source)
    manager.install("pageindex")
    try:
        manager.start("pageindex")
        assert manager.supervise_once() == [], "活着就不该重启"

        manager.stop("pageindex")
        assert manager.supervise_once() == [], "主动停止不能被监督打回"
        assert manager.is_running("pageindex") is False
    finally:
        manager.stop_all()


def test_gives_up_after_crash_loop(tmp_path: Path) -> None:
    """连续崩溃达上限后停止拉起，并把原因记下来（不静默）。"""
    manager = _install_crashing(tmp_path, _source(tmp_path))
    try:
        manager.start("pageindex")
        for _ in range(manager_module.MAX_CRASH_RESTARTS + 2):
            _wait_exit(manager, "pageindex")
            manager.supervise_once()

        assert _item(manager, "pageindex")["supervisionGaveUp"], "达上限必须给出放弃原因"
        assert "pageindex" not in manager._supervised, "放弃后不再期望它运行"
    finally:
        manager.stop_all()


def test_stable_uptime_resets_crash_count(tmp_path: Path) -> None:
    """跑够 STABLE_UPTIME 说明是"偶发崩溃"而非"崩溃循环"，计数清零。"""
    source = _source(tmp_path)
    build_source(source, "pageindex", {"app.py": SLEEP_SCRIPT.encode("utf-8")})
    manager = make_manager(tmp_path, source)
    manager.install("pageindex")
    try:
        manager.start("pageindex")
        manager._crash_counts["pageindex"] = 2
        manager._records["pageindex"].started_mono = time.monotonic() - manager_module.STABLE_UPTIME - 1

        manager.supervise_once()

        assert manager._crash_counts.get("pageindex") is None, "稳定运行后计数应清零"
    finally:
        manager.stop_all()
