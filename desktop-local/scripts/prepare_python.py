#!/usr/bin/env python
"""把**受管 CPython 解释器**就位到 ``frontend-vue/assets/python-runtime/<platform>/``。

**为什么解释器也要随包**（这是本脚本存在的唯一理由）
    只随包 ``uv`` 是**不够的**。``uv venv --python 3.12 --python-preference only-managed``
    在本地没有受管 CPython 时，会去 GitHub Releases 下载 python-build-standalone。
    而不少网络环境下 ``github.com`` 连 TCP 都不通（实测报
    ``invalid peer certificate: UnknownIssuer``）。于是"包里带了 uv"依然装不出解释器，
    用户看到的是首次启动永远卡在创建环境、所有页面报服务繁忙。

    把解释器随包、运行时用它的**绝对路径**建 venv，uv 就不会联网找解释器；
    依赖仍由 ``pyproject.toml`` 在首次启动时现装（那一步走 PyPI）。

    这与已废弃的旧形态 ``resources/python`` 不同：那份是"解释器 + 已装依赖"的整包，
    会冻结构建机状态、漏一个包就整体失效；这里**只有解释器**，依赖永远按清单现装。

**校验值从哪来**
    解释器由上一步就位的 ``uv`` 下载，uv 自带官方清单并逐文件校验 SHA256；
    而 uv 本体的版本与 SHA256 已固化在 ``uv_manifest.json``。因此这条链的信任根
    仍然是仓库里写死的那份清单，不是运行时取 latest。本脚本不再重复维护一份哈希，
    避免"两份清单漂移"。

用法::

    # 为当前平台就位解释器（需要联网；CI 与本地打包都走这条）
    python desktop-local/scripts/prepare_python.py

    # 指定平台
    python desktop-local/scripts/prepare_python.py --platform darwin-arm64

    # 用镜像源下载（github.com 不可达时）
    python desktop-local/scripts/prepare_python.py --mirror https://example.invalid/pbs

    # 用一个已经就位好的解释器目录（离线 / 内网），跳过下载
    python desktop-local/scripts/prepare_python.py --from D:/cache/cpython-3.12.16-windows-x86_64-none

    # 只自检，不下载
    python desktop-local/scripts/prepare_python.py --check

    # 删掉已就位的解释器
    python desktop-local/scripts/prepare_python.py --clean

自检内容：解释器能跑起来、版本前两位与 ``RUNTIME_PYTHON_VERSION`` 一致。
任何一条不过都以非零退出 —— 打一个跑不起来的解释器，用户首次启动必然失败。
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DESKTOP_LOCAL = Path(__file__).resolve().parents[1]
REPO_ROOT = DESKTOP_LOCAL.parent
FRONTEND_VUE = REPO_ROOT / "frontend-vue"
# 与 electron-builder.yml 的 extraResources 对应（assets/python-runtime → resources/python-runtime）
PYTHON_ASSETS_DIR = FRONTEND_VUE / "assets" / "python-runtime"
# uv 由 prepare_uv.py 就位，这里复用它，不再单独固化一份下载逻辑
UV_ASSETS_DIR = FRONTEND_VUE / "assets" / "uv"

# 必须与 electron/runtime-install.cjs::RUNTIME_PYTHON_VERSION 保持一致。
# 两处各自写死是有意的：构建脚本不该去解析 JS，而运行时不读本文件。
RUNTIME_PYTHON_VERSION = "3.12"

# 单步超时（秒）。解释器约 20MB，慢网络留足余量
INSTALL_TIMEOUT = 600


def current_platform_key() -> str:
    """把 ``platform.system()/machine()`` 归一成 ``<platform>-<arch>``。

    与 ``prepare_uv.py`` 的归一规则相同：Python 报 ``AMD64``／``x86_64``，
    而壳层按 Node 的 ``process.platform`` / ``process.arch`` 找目录，两边必须对上。
    """
    system = platform.system().lower()
    if system == "windows":
        os_key = "win32"
    elif system == "darwin":
        os_key = "darwin"
    elif system == "linux":
        os_key = "linux"
    else:
        raise SystemExit(f"不支持的平台：{platform.system()}")

    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        raise SystemExit(f"不支持的 CPU 架构：{platform.machine()}")
    return f"{os_key}-{arch}"


def target_dir(platform_key: str) -> Path:
    return PYTHON_ASSETS_DIR / platform_key


def uv_binary(platform_key: str) -> Path:
    """取 prepare_uv.py 就位好的 uv。缺失时给出可照做的指引。"""
    exe = "uv.exe" if platform_key.startswith("win32") else "uv"
    candidate = UV_ASSETS_DIR / platform_key / exe
    if not candidate.is_file():
        raise SystemExit(
            f"找不到 uv：{candidate}\n"
            "请先执行：python desktop-local/scripts/prepare_uv.py "
            f"--platform {platform_key}"
        )
    return candidate


def interpreter_in(directory: Path) -> Path | None:
    """在 uv 的安装目录里找解释器。

    布局分两种：Windows 是 ``python.exe``；POSIX 是 ``bin/python3``。

    **POSIX 上 ``bin/python`` 与 ``bin/python3`` 只是相对符号链接**（实测
    python-build-standalone 3.12.16 的 tar 清单：``lrwxrwxrwx python/bin/python3 -> python3.12``），
    真正的实体是 ``bin/python3.12``。所以这里必须**同时认版本化的实体文件** ——
    否则链接一旦在复制/打包环节丢掉，就会出现"uv 明明报装好了，构建脚本却说找不到解释器"。
    """
    for name in ("python.exe", "bin/python3", "bin/python"):
        candidate = directory / name
        if candidate.is_file():
            return candidate

    # 兜底：bin/python3.12 这类版本化实体。按版本号倒序取最高的一份，
    # 排除 python3.12-config 之类（suffix 不是纯数字）。
    bin_dir = directory / "bin"
    if bin_dir.is_dir():
        versioned = [p for p in bin_dir.glob("python3.*") if p.is_file() and _is_version_suffix(p)]
        if versioned:
            return sorted(versioned, key=_version_key, reverse=True)[0]
    return None


def _is_version_suffix(path: Path) -> bool:
    """``python3.12`` → True；``python3.12-config`` → False。"""
    parts = path.name.split(".")[1:]
    return bool(parts) and all(part.isdigit() for part in parts)


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in path.name.split(".")[1:] if part.isdigit())


def find_installed_layout(root: Path) -> Path | None:
    """在 ``--install-dir`` 产物里挑出真正的版本目录。

    uv 会在安装目录里放一组管理文件（``.temp`` / ``.lock`` / ``.gitignore``）以及
    一个 ``cpython-3.12-<platform>-<arch>-none`` 形式的**符号链接**指向具体版本。
    符号链接在打包时不可移植，所以这里只认"带解释器的实体目录"。
    """
    if interpreter_in(root):
        return root
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name.startswith("."):
            continue
        if interpreter_in(entry):
            return entry
    return None


def self_check(platform_key: str, version: str) -> int:
    """跑一遍解释器并核对版本。"""
    directory = target_dir(platform_key)
    layout = find_installed_layout(directory) if directory.is_dir() else None
    exe = interpreter_in(layout) if layout is not None else None
    if exe is None:
        print(
            f"[FAIL] {platform_key} 的解释器不存在：{directory}\n"
            "       打包前必须就位，否则用户首次启动无法创建环境。",
            file=sys.stderr,
        )
        return 1

    try:
        out = subprocess.run(
            [str(exe), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[FAIL] {exe} 无法运行：{exc}", file=sys.stderr)
        return 1

    if not out.startswith(f"{version}."):
        print(
            f"[FAIL] 解释器版本是 {out}，期望 {version}.x。"
            "请改 RUNTIME_PYTHON_VERSION 或重新就位。",
            file=sys.stderr,
        )
        return 1

    print(f"[ OK ] {platform_key}: Python {out} @ {exe}")
    return 0


def install_with_uv(platform_key: str, uv: Path, mirror: str | None) -> Path | None:
    """用 uv 下载解释器，返回临时安装目录（调用方负责复制与清理）。"""
    tmp = Path(tempfile.mkdtemp(prefix="chiron-py-"))
    cmd = [
        str(uv),
        "python",
        "install",
        RUNTIME_PYTHON_VERSION,
        "--install-dir",
        str(tmp),
        # 不往构建机的 ~/.local/bin 放 shim，也不写 Windows 注册表：
        # 构建脚本不该在 CI/开发机上留下全局副作用
        "--no-bin",
        "--no-registry",
    ]
    if mirror:
        cmd += ["--mirror", mirror]

    print(f"  · 执行：{' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(
            f"下载解释器超时（{INSTALL_TIMEOUT}s）。"
            "github.com 不可达时请加 --mirror，或用 --from 指定已下载的解释器目录。"
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(
            f"uv 安装解释器失败（退出码 {exc.returncode}）。\n"
            "常见原因：github.com 不可达 —— 此时加 --mirror，或用 --from 指定本地目录。"
        )
    return tmp


def copy_interpreter(source: Path, platform_key: str) -> Path:
    """把解释器目录整份复制到 assets，并清掉 uv 的管理文件。

    **保留 ``source`` 这一层目录名**（如 ``cpython-3.12.16-windows-x86_64-none``）：
    壳层 ``bundledPython()`` 是在 ``<platform>/`` 下逐个找子目录、再找解释器，
    这样版本号升级时目录名变了也不用改 JS。
    """
    directory = target_dir(platform_key)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # 用 resolve() 解开 uv 放的 junction/符号链接，落到真实版本目录名
    # （否则会复制成 `cpython-3.12-windows-x86_64-none` 这种不带小数版本的名字）
    destination = directory / source.resolve().name
    # POSIX 上必须保留符号链接：python-build-standalone 里
    # `bin/python` / `bin/python3` 都是指向 `python3.12` 的**相对**链接。
    # `symlinks=False` + `ignore_dangling_symlinks=True` 会因 shutil 用裸相对路径
    # 判 dangling（shutil.py:526 `os.path.exists(linkto)`，相对 CWD）而**静默丢掉**
    # 这两个链接 —— 结果就是"uv 报装好了，构建脚本却说找不到解释器"。
    # 相对链接与目标同目录，原样保留即可移植。
    # Windows 则相反：要解引用（uv 放的是 junction，且建符号链接要管理员权限）。
    keep_symlinks = os.name != "nt"
    shutil.copytree(
        source,
        destination,
        symlinks=keep_symlinks,
        # 只在解引用时需要它兜底；保留链接时该参数不生效，设为 False 以求失败可见
        ignore_dangling_symlinks=not keep_symlinks,
    )

    # .temp 是下载缓存、.lock 是锁文件、BUILD 是 uv 的记录：都不该进安装包
    for junk in (".temp", ".lock", ".gitignore", "BUILD"):
        path = destination / junk
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="就位受管 CPython 解释器（构建期）")
    parser.add_argument("--platform", help="目标平台键，如 win32-x64 / darwin-arm64")
    parser.add_argument("--from", dest="source", help="已就位的解释器目录（离线时用）")
    parser.add_argument("--mirror", help="python-build-standalone 镜像地址（github 不可达时）")
    parser.add_argument("--check", action="store_true", help="只自检，不下载")
    parser.add_argument("--clean", action="store_true", help="删除已就位的解释器")
    args = parser.parse_args(argv)

    platform_key = args.platform or current_platform_key()
    target = target_dir(platform_key)

    if args.clean:
        if target.exists():
            shutil.rmtree(target)
            print(f"已删除 {target}")
        else:
            print(f"无需删除（{target} 不存在）")
        return 0

    if args.check:
        return self_check(platform_key, RUNTIME_PYTHON_VERSION)

    if args.source:
        source = Path(args.source).resolve()
        layout = find_installed_layout(source)
        if layout is None:
            raise SystemExit(f"--from 目录里没找到解释器：{source}")
        print(f"  · 使用本地解释器目录 {layout}")
        # 若 source 本身就是安装根（含 .temp 等），复制后再清管理文件
        copy_interpreter(layout, platform_key)
        tmp = None
    else:
        uv = uv_binary(platform_key)
        tmp = install_with_uv(platform_key, uv, args.mirror)
        layout = find_installed_layout(tmp)
        if layout is None:
            shutil.rmtree(tmp, ignore_errors=True)
            raise SystemExit(
                "uv 报告装好了，但下载目录里找不到解释器。请把上面的输出一起反馈。"
            )
        copy_interpreter(layout, platform_key)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nPython {RUNTIME_PYTHON_VERSION} 已就位于 {target}")
    print("   打包时由 electron-builder 的 extraResources 拷到 resources/python-runtime/")
    return self_check(platform_key, RUNTIME_PYTHON_VERSION)


if __name__ == "__main__":
    raise SystemExit(main())
