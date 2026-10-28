#!/usr/bin/env python
"""把 PostgreSQL 官方二进制就位到 ``desktop-local/bin/pg/``，并实测 ``initdb`` 能跑。

**为什么需要这一步**：``app/storage/pg.py`` 的后端顺序是
``外部 DSN → embedded-wheel → 官方二进制 → 失败``。第三档按
``配置目录 → 随包目录 → PATH`` 找 ``initdb`` / ``pg_ctl``（见 ``_find_binary``），
其中"随包目录"就是本脚本的产出位置。仓库 ``.gitignore`` 忽略整个 ``desktop-local/bin/``
（几十 MB 的二进制不该入库），所以必须有一个可复现的"就位 + 自检"动作，
否则打包机上新装用户那一档会静默落空。

**为什么必须连同 ``lib`` 与 ``share`` 一起拷**：``initdb`` 与 ``pg_ctl`` 是官方发行版
构建，按相对路径找模板与动态库（``share/postgres.bki``、``lib/*.dll``）。
只拷 ``bin/`` 会在 initdb 阶段报找不到 ``postgres.bki`` —— 而且报错点在
"数据库起不来"，离真正的原因（少了两个目录）很远。

用法::

    # 从已安装的 embedded-postgres wheel 里取（最常见）
    python desktop-local/scripts/prepare_pg.py

    # 从某个官方 PostgreSQL 安装目录取（该目录下应有 bin/、lib/、share/）
    python desktop-local/scripts/prepare_pg.py --from "C:/Program Files/PostgreSQL/16"

    # 只自检，不拷贝（CI 里验证"当前这一档是否可用"）
    python desktop-local/scripts/prepare_pg.py --check

    # 删掉已就位的二进制
    python desktop-local/scripts/prepare_pg.py --clean
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

DESKTOP_LOCAL = Path(__file__).resolve().parents[1]
BUNDLED_PG_DIR = DESKTOP_LOCAL / "bin" / "pg"

# 这三个目录必须同级共存，缺一不可（见模块文档）
REQUIRED_DIRS = ("bin", "lib", "share")

# initdb 是第一个被调用的程序，用它做自检最有代表性
PROBE_BINARY = "initdb.exe" if sys.platform == "win32" else "initdb"


def bundled_bin_dir() -> Path:
    return BUNDLED_PG_DIR / "bin"


def find_source_dir() -> Path | None:
    """定位 ``embedded_postgres`` wheel 里的 ``pginstall`` 目录。"""
    spec = importlib.util.find_spec("embedded_postgres")
    if spec is None or not spec.submodule_search_locations:
        return None
    for root in spec.submodule_search_locations:
        candidate = Path(root) / "pginstall"
        if candidate.is_dir():
            return candidate
    return None


def is_valid_source(path: Path) -> bool:
    return all((path / name).is_dir() for name in REQUIRED_DIRS)


def probe() -> bool:
    """跑 ``initdb --version``：只验它能执行，不建任何数据目录。"""
    binary = bundled_bin_dir() / PROBE_BINARY
    if not binary.is_file():
        print(f"FAIL 随包目录里没有 {PROBE_BINARY}：{binary}")
        return False
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError as exc:
        print(f"FAIL 无法执行 {binary}：{exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        print(f"FAIL {PROBE_BINARY} --version 退出码 {result.returncode}：{detail}")
        return False
    print(f"OK   {result.stdout.strip()}")
    return True


def install(source: Path) -> int:
    if not is_valid_source(source):
        missing = [name for name in REQUIRED_DIRS if not (source / name).is_dir()]
        print(f"FAIL 源目录缺少 {', '.join(missing)}：{source}")
        print("     官方发版的解压目录里应当同时有 bin/、lib/、share/ 三个同级目录。")
        return 1

    BUNDLED_PG_DIR.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_DIRS:
        target = BUNDLED_PG_DIR / name
        if target.exists():
            shutil.rmtree(target)
        print(f"拷贝 {name}/ ...")
        shutil.copytree(source / name, target, symlinks=True)

    print(f"就位完成：{BUNDLED_PG_DIR}")
    return 0 if probe() else 1


def clean() -> int:
    if not BUNDLED_PG_DIR.exists():
        print(f"无需清理：{BUNDLED_PG_DIR} 不存在")
        return 0
    shutil.rmtree(BUNDLED_PG_DIR)
    print(f"已删除：{BUNDLED_PG_DIR}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="就位/自检随包 PostgreSQL 二进制")
    parser.add_argument("--from", dest="source", help="官方 PostgreSQL 目录（含 bin/lib/share）")
    parser.add_argument("--check", action="store_true", help="只自检，不拷贝")
    parser.add_argument("--clean", action="store_true", help="删除已就位的二进制")
    args = parser.parse_args(argv)

    if args.clean:
        return clean()
    if args.check:
        return 0 if probe() else 1

    source = Path(args.source) if args.source else find_source_dir()
    if source is None:
        print(
            "FAIL 找不到 PostgreSQL 二进制来源。\n"
            "     装一个：pip install 'embedded-postgres>=18.6.3,<19'\n"
            "     或指定官方目录：python desktop-local/scripts/prepare_pg.py --from <目录>"
        )
        return 1
    print(f"来源：{source}")
    return install(source)


if __name__ == "__main__":
    raise SystemExit(main())
