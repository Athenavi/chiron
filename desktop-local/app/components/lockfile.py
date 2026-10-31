"""跨进程文件锁：保护 ``<data-dir>/components`` 的目录写操作。

**为什么需要**：:class:`~app.components.manager.ComponentManager` 里的
``threading.Lock`` 只保护本进程的进程表。两个 core 实例（用户手动再起一个、
或 Electron 重启时旧的还没退干净）会同时 ``install`` / ``remove`` 同一个组件目录，
互相覆盖版本目录、删掉对方正在写的文件。这一层锁是**跨进程**的。

**只用在哪里**：只包住会改动组件目录的操作（安装落位、卸载）。
启动 / 停止只动本进程的进程表，不需要跨进程互斥 —— 也不该拿它，
否则健康检查那 20s 会一直占着锁。

**实现**：Windows 用 ``msvcrt.locking``（区域锁，需锁 1 字节），
其它平台用 ``fcntl.flock``。两者都是非阻塞尝试 + 轮询，带超时，
不无限等待（拿不到锁要报错，不能把 HTTP 请求挂死）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

if os.name == "nt":  # pragma: no cover —— 平台分支，两边各在一套 CI 上跑
    import msvcrt
else:  # pragma: no cover
    import fcntl

#: 锁文件里的占位字节数。``msvcrt.locking`` 锁的是字节区域，文件不能是空的。
_LOCK_BYTES = 1

DEFAULT_LOCK_TIMEOUT = 30.0
_POLL_INTERVAL = 0.05


class LockTimeoutError(TimeoutError):
    """在超时内没拿到跨进程锁。"""


class ComponentDirLock:
    """``<data-dir>/components/.lock`` 上的排他锁（可重入由调用方保证，本身不可重入）。"""

    def __init__(self, path: Path, *, timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            # 保证锁区域有字节可锁；内容本身无意义。
            if os.fstat(fd).st_size < _LOCK_BYTES:
                os.write(fd, b"\0" * _LOCK_BYTES)
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self._try_lock(fd)
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LockTimeoutError(
                            f"在 {self.timeout}s 内没有拿到组件目录锁 {self.path}"
                            "（可能另一个 core 实例正在安装/卸载组件）"
                        ) from None
                    time.sleep(_POLL_INTERVAL)
                else:
                    self._fd = fd
                    return
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            self._unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> ComponentDirLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    # --- 平台分支 ---
    @staticmethod
    def _try_lock(fd: int) -> None:
        if os.name == "nt":  # pragma: no cover
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_BYTES)
        else:  # pragma: no cover
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(fd: int) -> None:
        if os.name == "nt":  # pragma: no cover
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_BYTES)
        else:  # pragma: no cover
            fcntl.flock(fd, fcntl.LOCK_UN)
