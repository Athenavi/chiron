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
import sysconfig
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


def _version_dll() -> str:
    """Windows 版本 DLL 名：`python314.dll`（按运行本脚本的解释器算）。

    不能硬编码 `python314.dll`：CI 的 setup-python 给的未必是 3.14，写死会导致
    在 3.13 runner 上这个文件根本不存在 —— 而下面的拷贝循环对"缺失"是静默跳过的，
    于是打出**起不来的解释器**，直到用户装上才发现。所以按版本算，并且缺失即报错。
    """
    return f"python{sys.version_info.major}{sys.version_info.minor}.dll"


# 解释器本体：exe + 版本 DLL + C 运行时。缺任何一项目标机都起不来。
# 前两项是**硬必需**（缺失直接失败）；vcruntime 允许缺失（系统可能自带 VS 运行时）。
ROOT_FILES_REQUIRED_WIN = ("python.exe", "python3.dll")
ROOT_FILES_OPTIONAL_WIN = ("vcruntime140.dll", "vcruntime140_1.dll")
# 自检用的"探针模块"：**不在构建机上先把它们 import 一遍**，只把名字交给目标解释器。
# 为什么不在这里 import：本脚本跑在构建机的解释器上，那里的依赖是完整的，
# 提前 import 只能证明"构建机装好了"，证明不了"随包运行时能起来"。
#
# 关键约束：**不要手工维护第三方包清单**。清单一定会漂移 —— 曾经漏了 httpx，
# 于是 site-packages 里没有它、自检却照样通过，安装包内 core 一启动就
# `ModuleNotFoundError: No module named 'httpx'`。所以这里改成真实 import
# app 的全部模块：依赖闭包由 Python 自己算，漏一个就报错。
CHECK_ENTRY = "app.main:create_app"
# 兜底断言：即使将来 app 改了入口，这几条也必须在（存储与 Web 层的地基）。
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
    "httpx",
    "embedded_postgres",
)


def _exe_name() -> str:
    return "python.exe" if os.name == "nt" else "python"


def _target_exe(root: Path) -> Path:
    """运行时里解释器所在位置：Windows 是根目录，POSIX 是 ``bin/``。"""
    return root / _exe_name() if os.name == "nt" else root / "bin" / _python_name_posix()


def _python_name_posix() -> str:
    return f"python{sys.version_info.major}.{sys.version_info.minor}"


def copy_libpython(src: Path, dst_lib: Path) -> None:
    """POSIX 下把共享库 libpython 一起带上（Windows 不需要：版本 DLL 已在必需清单里）。

    ``bin/pythonX.Y`` 只是个薄壳，真正的解释器逻辑在 ``libpython3.X.so.1.0`` /
    ``libpython3.X.dylib`` 里。只拷可执行文件的话，目标机上是
    ``error while loading shared libraries: libpython3.12.so.1.0``（Linux）或 dyld 找不到
    Python（macOS）—— 而构建机上因为系统装了同名库，测不出来。

    ``LDLIBRARY`` 为空表示这份 Python 是静态链接的，无需共享库。
    拷完后 ``self_check`` 会真实执行一次解释器，rpath 没写对也会在那里失败，不会静默出包。
    """
    if os.name == "nt":
        return
    dst_lib.mkdir(parents=True, exist_ok=True)
    libname = sysconfig.get_config_var("LDLIBRARY")
    libdir = sysconfig.get_config_var("LIBDIR")
    if not libname:
        # 静态链接的解释器没有共享库，无需携带
        print("  · 解释器未使用共享库（静态链接），跳过 libpython", file=sys.stderr)
        return
    if not libdir:
        raise SystemExit("拿不到 sysconfig LIBDIR，无法定位 libpython")
    # macOS 的 python.org 安装包是 **framework 构建**：LDLIBRARY 形如
    # `Python.framework/Versions/3.12/Python`，不是 libpython*.dylib，且 bin/python3.12
    # 通过 @rpath 找 framework 里的那份。拷单个文件不够，得整份 framework 一起带 ——
    # 这条路径没法在 Windows/Linux 上实测，所以**显式失败**而不是猜着拷：
    # 出残包（用户装上 dyld: Library not loaded）比 CI 红掉严重得多。
    # 放在"文件是否存在"之前判断：否则 framework 构建会先报"找不到共享库"，
    # 把真正的原因（布局不支持）盖掉。
    if not str(libname).startswith("libpython"):
        raise SystemExit(
            f"POSIX 共享库布局未支持：LDLIBRARY={libname!r}。\n"
            "  这是 macOS framework 构建的情形，需要把 Python.framework 整体带进随包运行时。\n"
            "  在修好之前不要发 macOS 包 —— 否则用户机上 dyld 找不到 Python，本地模式全崩。"
        )
    source = Path(str(libdir)) / str(libname)
    if not source.is_file():
        raise SystemExit(f"找不到共享库：{source}")
    shutil.copy2(source, dst_lib / str(libname))
    print(f"  · 已带共享库 {libname}")


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
        # 版本 DLL 按当前解释器算（python314.dll / python313.dll …），不写死
        required = (*ROOT_FILES_REQUIRED_WIN, _version_dll())
        for name in required:
            candidate = src / name
            if not candidate.is_file():
                # 缺必需文件就停：继续打包只会产出一个"装了也起不来"的解释器
                raise SystemExit(f"解释器缺少必需文件：{candidate}")
            shutil.copy2(candidate, dst / name)
        for name in ROOT_FILES_OPTIONAL_WIN:
            candidate = src / name
            if candidate.is_file():
                shutil.copy2(candidate, dst / name)
            else:
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
        if not real.is_file():
            raise SystemExit(f"解释器缺少必需文件：{real}")
        (dst / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(real, dst / "bin" / _python_name_posix())
        (dst / "bin" / "python3").symlink_to(_python_name_posix())
        lib = dst / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        src_lib = src / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        # libpython：只有拷了 bin/pythonX.Y 而没带共享库时，Linux 上是
        # `error while loading shared libraries: libpython3.12.so.1.0`，macOS 上是
        # dyld 找不到 @rpath 里的 Python。用 sysconfig 定位（不同发行版布局不同），
        # 拷到 lib/ 并让下面的自检**真实执行**一次解释器来兜底 ——
        # 拷错不会静默通过，因为 self_check 是硬门禁。
        copy_libpython(src, dst / "lib")

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
    """在干净环境里验证运行时：解释器可执行 + 真实 import 整个 ``app`` 包。

    返回 0 表示可用。**这是"能不能随包发布"的唯一判据**，不看文件是否存在。

    为什么不是"import 一串手写模块名"：手写清单只覆盖作者想到的东西。曾经的清单里有
    fastapi/asyncpg 却没有 httpx，于是自检全绿、安装包里的 core 却一启动就
    `ModuleNotFoundError: No module named 'httpx'`。现在改为遍历 ``app`` 包下**每一个**
    模块并逐个 import，依赖闭包由 Python 自己算，少装一个包就必然在这里红掉。
    """
    exe = _target_exe(root)
    if not exe.is_file():
        print(f"FAIL 运行时里没有解释器：{exe}", file=sys.stderr)
        return 2

    code = (
        "import importlib, pkgutil, sys\n"
        "missing = []\n"
        f"names = {list(CHECK_IMPORTS)!r}\n"
        "for n in names:\n"
        "    try:\n"
        "        importlib.import_module(n)\n"
        "    except Exception as e:\n"
        "        missing.append(f'{n}: {type(e).__name__}: {e}')\n"
        "import app\n"
        "# walk_packages 默认**静默吞掉**子包 import 失败的异常（onerror=None），\n"
        "# 那样失败的子包及其子孙都不会进 mods，自检会假通过。必须显式收集。\n"
        "def _onerror(name):\n"
        "    missing.append(f'{name}: {type(sys.exc_info()[1]).__name__}: {sys.exc_info()[1]}')\n"
        "mods = sorted(m.name for m in pkgutil.walk_packages(app.__path__, 'app.', onerror=_onerror))\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception as e:\n"
        "        missing.append(f'{m}: {type(e).__name__}: {e}')\n"
        "print('MODULES', len(mods))\n"
        "print('MISSING', len(missing))\n"
        "for x in missing:\n"
        "    print('  MISS', x)\n"
        "sys.exit(1 if missing else 0)"
    )
    try:
        proc = subprocess.run(
            [str(exe), "-c", code],
            capture_output=True,
            text=True,
            timeout=180,
            # cwd 必须在 desktop-local 下：`-c` 模式的 sys.path[0] 是当前目录，
            # 这样 `import app` 找的才是**将被随包发布的那份源码**，而不是构建机
            # site-packages 里可能存在的同名包。
            cwd=str(DESKTOP_LOCAL),
            env=_clean_env(),
        )
    except subprocess.TimeoutExpired:
        print("FAIL 自检超时（180s）", file=sys.stderr)
        return 1

    if proc.returncode != 0:
        print("FAIL 干净环境下导入依赖失败：", file=sys.stderr)
        print(proc.stderr.strip()[-2000:], file=sys.stderr)
        print(proc.stdout.strip()[-4000:], file=sys.stderr)
        return 1

    if verbose:
        modules = ""
        for line in proc.stdout.splitlines():
            if line.startswith("MODULES "):
                modules = line.split()[1]
        print(f"  · OK {proc.stdout.splitlines()[0] if proc.stdout else ''}".strip())
        print(f"  · 已导入 {modules} 个 app 模块 + {len(CHECK_IMPORTS)} 个关键包")
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
