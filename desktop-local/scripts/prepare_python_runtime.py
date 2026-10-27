#!/usr/bin/env python
"""把一份**可独立运行**的 Python 运行时（解释器 + desktop-local 的全部依赖）就位到
``frontend-vue/python-runtime/``，供 electron-builder 随安装包发布。

**为什么必须有这一步**：``electron-builder.yml`` 的 ``extraResources`` 原本只带
``desktop-local`` 源码与 ``migrations/``，既不带解释器也不带 site-packages。于是用户机上
``components.cjs::bundledPython()`` 找不到 ``resources/python/python.exe``，退回 PATH 上的
``python``（多数 Windows 机器没有，或有商店占位符）；即便有解释器，也因为没装 fastapi /
asyncpg / embedded-postgres 而 ``ModuleNotFoundError``。本地模式因此起不来。

**为什么是"精简拷贝"而不是 python-build-standalone**：实测本源解释器剔除
``test``/``idlelib``/``ensurepip``/``tkinter`` 等后仅 39.9MB，清空 ``PYTHONPATH`` /
``PYTHONHOME`` 后仍能 import sqlite3 / ssl / asyncio —— 即可重定位。这样构建机**不需要**
额外下载 200MB+ 的 standalone 包，也就不会让构建依赖外网（``vendor/规划.md`` 明确
"不自动下载工具"）。依赖树用宿主 pip ``--target`` 装到目标目录。

**为什么依赖清单从 pyproject.toml 读**：手抄一份依赖列表迟早与
``desktop-local/pyproject.toml`` 漂移，而漂移的表现是用户机上的 ``ModuleNotFoundError``。

用法::

    # 构建运行时（默认输出 frontend-vue/python-runtime/）
    python desktop-local/scripts/prepare_python_runtime.py

    # 只自检：验证现有运行时能否在"干净环境"里跑起来（CI/打包前用）
    python desktop-local/scripts/prepare_python_runtime.py --check

    # 删掉已生成的运行时
    python desktop-local/scripts/prepare_python_runtime.py --clean

自检会清空所有 ``PYTHON*`` 环境变量、把 ``PATH`` 收到系统目录，再验证
``import fastapi, uvicorn, asyncpg, alembic, sqlalchemy, psycopg2, embedded_postgres``——
这正是用户机上的条件。任何一步失败都以非零退出。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

DESKTOP_LOCAL = Path(__file__).resolve().parents[1]
REPO_ROOT = DESKTOP_LOCAL.parent
# 输出到 frontend-vue/ 下：electron-builder.yml 的 extraResources 用相对 frontend-vue 的路径，
# 且该目录不进 git（构建产物，140MB 量级）。
DEFAULT_OUT = REPO_ROOT / "frontend-vue" / "python-runtime"

# 精简时剔除的 stdlib 大件。逐个说明为什么可以不要：
#   test      36MB，CPython 自带测试套件
#   idlelib   2MB，IDLE 编辑器
#   ensurepip 1.8MB，随包运行时不需要自带 pip（依赖由构建机 --target 装入）
#   tkinter   需要 Tcl/Tk 运行时，本项目无 GUI 依赖
#   turtledemo / lib2to3 / pydoc_data / venv  与本地 core 无关
DROP_DIRS = {
    "test",
    "idlelib",
    "ensurepip",
    "tkinter",
    "turtledemo",
    "lib2to3",
    "pydoc_data",
    "venv",
    "site-packages",  # 重新由 pip --target 生成，避免把构建机的杂包一起带走
}
# 解释器本体：exe + 版本 DLL + C 运行时。缺任何一项目标机都起不来。
ROOT_FILES_WIN = [
    "python.exe",
    "python3.dll",
    "python314.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
]
# 自检要 import 的模块：覆盖 Web 层、两个存储驱动、迁移链、以及 PG 二进制来源
CHECK_IMPORTS = (
    "fastapi",
    "uvicorn",
    "pydantic",
    "pydantic_settings",
    "asyncpg",
    "redis",
    "alembic",
    "sqlalchemy",
    "psycopg2",
    "dotenv",
    "embedded_postgres",
)


def _exe_name() -> str:
    return "python.exe" if os.name == "nt" else "python"


def _target_exe(root: Path) -> Path:
    """运行时里解释器所在位置：Windows 是根目录，POSIX 是 ``bin/``。"""
    return root / _exe_name() if os.name == "nt" else root / "bin" / _python_name_posix()


def _python_name_posix() -> str:
    return f"python{sys.version_info.major}.{sys.version_info.minor}"


def _clean_env() -> dict[str, str]:
    """模拟用户机：去掉所有 ``PYTHON*`` 变量，PATH 只留系统目录。

    不这么做的话，构建机上 ``PYTHONPATH`` / ``PYTHONHOME`` 可能替目标运行时找到依赖，
    自检就会"假通过"，而用户机上照样 import 失败。
    """
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    if os.name == "nt":
        env["PATH"] = r"C:\Windows\System32;C:\Windows"
    else:
        env["PATH"] = "/usr/bin:/bin"
    return env


def read_runtime_dependencies(pyproject: Path = DESKTOP_LOCAL / "pyproject.toml") -> list[str]:
    """从 ``pyproject.toml`` 读运行时依赖，避免与源码声明漂移。"""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies")
    if not deps:
        raise SystemExit(f"{pyproject} 里没有 [project].dependencies，无法确定要装什么")
    return list(deps)


def source_prefix(explicit: str) -> Path:
    """构建来源解释器：``--from`` 优先，否则用当前正在跑这份脚本的解释器。"""
    root = Path(explicit) if explicit else Path(sys.prefix)
    if not _target_exe(root).is_file():
        raise SystemExit(f"{root} 下没有 {_target_exe(root)}，不是有效的 Python 目录")
    return root


def copy_interpreter(src: Path, dst: Path) -> None:
    """把解释器最小集拷到 ``dst``（不含 site-packages）。"""
    dst.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        for name in ROOT_FILES_WIN:
            candidate = src / name
            if candidate.is_file():
                shutil.copy2(candidate, dst / name)
            elif name.startswith("vcruntime"):
                # vcruntime 缺失说明系统装了 VS 运行时；不致命，但要让人知道
                print(f"  · 跳过 {name}（源目录没有）", file=sys.stderr)
        # Windows 的扩展模块在 DLLs/
        if (src / "DLLs").is_dir():
            shutil.copytree(src / "DLLs", dst / "DLLs", dirs_exist_ok=True)
        lib = dst / "Lib"
        src_lib = src / "Lib"
    else:
        # POSIX：bin/pythonX.Y 是软链，要按真实文件拷；stdlib 在 lib/pythonX.Y
        exe = src / "bin" / _python_name_posix()
        real = exe.resolve() if exe.exists() else exe
        (dst / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(real, dst / "bin" / _python_name_posix())
        (dst / "bin" / "python3").symlink_to(_python_name_posix())
        lib = dst / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        src_lib = src / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"

    lib.mkdir(parents=True, exist_ok=True)
    if not src_lib.is_dir():
        raise SystemExit(f"找不到 stdlib 目录：{src_lib}")

    for item in src_lib.iterdir():
        if item.is_dir():
            if item.name in DROP_DIRS:
                continue
            shutil.copytree(item, lib / item.name, dirs_exist_ok=True)
        elif item.suffix in {".py", ".pyd", ".dll", ".so", ".dylib"} or item.name in {
            "os.py",
            "site.py",
        }:
            shutil.copy2(item, lib / item.name)

    # site-packages 必须先存在，pip --target 才会往里写
    (lib / "site-packages").mkdir(exist_ok=True)


def install_dependencies(target: Path) -> None:
    """用**宿主** pip 把依赖装进目标 site-packages。

    为什么用宿主 pip 而不是目标解释器的 ``-m pip``：精简运行时剔除了 ``ensurepip``，
    目标解释器里根本没有 pip。``pip --target`` 本来就不要求目标解释器自带 pip。
    """
    deps = read_runtime_dependencies()
    print(f"  · 安装 {len(deps)} 项依赖到 {target}")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(target), *deps],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"pip 安装依赖失败（退出码 {proc.returncode}）：\n{proc.stderr.strip()[-2000:]}"
        )


def self_check(root: Path, *, verbose: bool = True) -> int:
    """在干净环境里验证运行时：解释器可执行 + 全部关键模块可导入。

    返回 0 表示可用。**这是"能不能随包发布"的唯一判据**，不看文件是否存在。
    """
    exe = _target_exe(root)
    if not exe.is_file():
        print(f"FAIL 运行时里没有解释器：{exe}", file=sys.stderr)
        return 2

    code = (
        "import sys;"
        f"import {', '.join(CHECK_IMPORTS)};"
        "print('OK', sys.version.split()[0], sys.executable)"
    )
    try:
        proc = subprocess.run(
            [str(exe), "-c", code],
            capture_output=True,
            text=True,
            timeout=180,
            env=_clean_env(),
        )
    except subprocess.TimeoutExpired:
        print("FAIL 自检超时（180s）", file=sys.stderr)
        return 1

    if proc.returncode != 0:
        print("FAIL 干净环境下导入依赖失败：", file=sys.stderr)
        print(proc.stderr.strip()[-2000:], file=sys.stderr)
        return 1

    if verbose:
        print(f"  · {proc.stdout.strip()}")
        print(f"  · 已导入 {len(CHECK_IMPORTS)} 个模块")
    return 0


def dir_size(root: Path) -> int:
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def clean(root: Path) -> int:
    if not root.is_dir():
        print(f"无需清理：{root} 不存在")
        return 0
    shutil.rmtree(root)
    print(f"已删除 {root}")
    return 0


def build(src: Path, out: Path) -> int:
    if out.exists():
        # 增量拷贝会把上一次的残留包留在 site-packages 里，最终产物不可复现；
        # 每次都从空目录开始。
        shutil.rmtree(out)
    print(f"来源解释器: {src}")
    print(f"输出目录  : {out}")

    print("[1/3] 拷贝精简解释器 + stdlib")
    copy_interpreter(src, out)
    print(f"  · 解释器就绪，体积 {dir_size(out) / 1024 / 1024:.1f} MB")

    print("[2/3] 安装运行时依赖")
    install_dependencies(_site_packages(out))
    print(f"  · 依赖就绪，总体积 {dir_size(out) / 1024 / 1024:.1f} MB")

    print("[3/3] 干净环境自检")
    return self_check(out)


def _site_packages(root: Path) -> Path:
    if os.name == "nt":
        return root / "Lib" / "site-packages"
    return root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--from", dest="source", default="", help="来源 Python 目录（默认当前解释器）")
    parser.add_argument("--out", dest="out", default="", help=f"输出目录（默认 {DEFAULT_OUT}）")
    parser.add_argument("--check", action="store_true", help="只自检现有运行时，不重建")
    parser.add_argument("--clean", action="store_true", help="删除已生成的运行时")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else DEFAULT_OUT

    if args.clean:
        return clean(out)
    if args.check:
        return self_check(out)

    return build(source_prefix(args.source), out)


if __name__ == "__main__":
    raise SystemExit(main())
