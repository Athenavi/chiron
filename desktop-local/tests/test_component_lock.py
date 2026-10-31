"""跨进程组件目录锁（``app.components.lockfile``）的单元测试。

锁的意义在于**跨进程**：两个 core 实例同时安装/卸载同一组件时不能互相踩。
这里用两个独立的 ``ComponentDirLock`` 实例（各自 open 一次锁文件）模拟两个进程 ——
两个平台的底层锁都挂在 open file description 上，不同 fd 之间同样互斥，
所以这个近似能真实复现"拿不到锁"的分支，而不需要真的 fork 一个进程。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from app.components.lockfile import ComponentDirLock, LockTimeoutError


def test_second_holder_times_out_while_first_holds_the_lock(tmp_path: Path) -> None:
    """A 持锁期间 B 拿不到，且按超时如实报错（不无限等）。"""
    path = tmp_path / "components" / ".lock"
    first = ComponentDirLock(path)
    first.acquire()
    try:
        second = ComponentDirLock(path, timeout=0.2)
        started = time.monotonic()
        with pytest.raises(LockTimeoutError, match="没有拿到组件目录锁"):
            second.acquire()
        assert time.monotonic() - started >= 0.2, "应该真的等到超时，而不是立刻失败"
    finally:
        first.release()


def test_lock_is_reusable_after_release(tmp_path: Path) -> None:
    """释放后下一个持有者立刻拿得到（不会因为锁文件残留就永久占用）。"""
    path = tmp_path / ".lock"
    with ComponentDirLock(path):
        pass
    with ComponentDirLock(path, timeout=1.0) as second:
        assert second.path == path


def test_acquire_creates_parent_dir_and_non_empty_lock_file(tmp_path: Path) -> None:
    """锁文件由我们自己创建：父目录不存在也要能建，且文件非空（msvcrt 需要可锁区域）。"""
    path = tmp_path / "nested" / "dir" / ".lock"
    with ComponentDirLock(path):
        assert path.is_file()
        assert path.stat().st_size >= 1


def test_release_without_acquire_is_noop(tmp_path: Path) -> None:
    """``release`` 幂等：没拿到锁时调用不该炸（``stop_all`` 一类的清理路径会用到）。"""
    ComponentDirLock(tmp_path / ".lock").release()
