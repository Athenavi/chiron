#!/usr/bin/env python3
"""compose 必填环境变量 ↔ `.env.example` 的守卫。

**判据（先量过假阳性率才定的）**：`docker-compose*.yml` 里用 **`${VAR:?…}`** 声明的变量
是**真正必填**的（compose 会在缺失时直接报错）；这类变量必须在 `.env.example` 里出现
（**注释形态也算** —— 模板里可选/推荐项本来就是注释的）。

**为什么不用"compose 用到的所有变量"**：实测 29 个里只有 7 个是 `:?`，其余 22 个都带
`:-默认值`（可选覆盖）；若按"全部 29 个都要有"去要求，会为 **9 个有默认值的可选变量**报假阳性
（`S3_BUCKET` / `VECTOR_DB_TYPE` / `MILVUS_ADDRESS` … —— 而 `.env.example` 自己写明
"业务/基础设施配置已迁移至后台「系统设置」"）⇒ 那种守卫会惩罚**正确的文档**。

退出码：0 = 必填项都有文档；1 = 有遗漏。
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"

#: compose 里"必填"的写法：`${VAR:?msg}`
REQUIRED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?")
#: `.env.example` 的条目（**含注释行** —— 模板里可选变量是注释形态）
ENTRY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")


def compose_files() -> list[pathlib.Path]:
    found = []
    for pattern in ("docker-compose*.yml", "docker-compose*.yaml", "*/docker-compose*.yml",
                    "*/docker-compose*.yaml"):
        for p in ROOT.glob(pattern):
            if "node_modules" in p.parts or "vendor" in p.parts or ".git" in p.parts:
                continue
            if p not in found:
                found.append(p)
    return sorted(found)


def main() -> int:
    files = compose_files()
    if not files:
        print("守卫自身失效：没找到 docker-compose 文件")
        return 1

    required: dict[str, set[str]] = {}
    for p in files:
        for m in REQUIRED.finditer(p.read_text(encoding="utf-8", errors="replace")):
            required.setdefault(m.group(1), set()).add(p.name)

    if not required:
        # 防失效：一条 `:?` 都没有 ⇒ 说明判据已经和 compose 的写法脱节了
        print("守卫自身失效：compose 里一条 `${VAR:?…}` 都没找到（判据已过期？）")
        return 1

    if not ENV_EXAMPLE.exists():
        print(f"守卫自身失效：找不到 {ENV_EXAMPLE}")
        return 1

    documented = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip().lstrip("#").strip()
        if ENTRY.match(s):
            documented.add(s.split("=", 1)[0].strip())

    missing = sorted(set(required) - documented)
    if missing:
        print(f"compose 必填但 .env.example 没写：{len(missing)} 个")
        for name in missing:
            print(f"  [X] {name}  ← {', '.join(sorted(required[name]))}")
        print("\n修法：在 .env.example 的「compose 必填」区块补一行"
              "（可写成注释形态，与既有风格一致）")
        return 1

    print(
        f"compose 必填变量全部有文档：{len(required)} 个"
        f"（{'/'.join(sorted(required))}）；.env.example 条目（含注释）{len(documented)} 个"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
