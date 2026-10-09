#!/usr/bin/env python3
"""ORM 生成物新鲜度守卫（路线图 L4-3）。

**判据**：把生成器重跑到**临时目录**，与已提交的 `shared/models/` **逐文件比对**；
**只忽略「生成时间：」那一行** —— 模板每次运行都写当前时间，第 68 轮就是它造成
**69 个文件的纯时间戳噪声**（当时只能靠字节级备份回退）。

**为什么要它**：生成物与 `configs/orm/V1/models.yaml` 漂移过一次 —— `subagent_run.py`
缺 `inherited_messages` / `retryable` / `output_bytes` / `validator_*` / `error_code`
共 7 列，而迁移 `0004`/`0006`/`0007` 里都有；当时**没有任何机械化检查**，是人肉核对才发现。
`shared/` 既不在 ruff/mypy 门禁内，也没有任何用例提到生成器（全仓只有生成器自己提它）。

**为什么猴子补丁 `OUTPUT_DIR` 而不是原地重跑**：原地重跑会改写仓库里 71 个文件
（含那行时间戳），守卫自己就成了"会弄脏工作区"的东西。指向临时目录后，仓库**只读**。

退出码：0 = 与 yaml 一致；1 = 有漂移（打印差异文件名与首个不同行）。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import re
import sys
import tempfile

#: 同 `generate_orm_models.py`：报告里含 `⇒` / emoji，Windows 控制台默认 GBK ⇒ 打印时崩
#: （实测：**发现漂移的那一刻**脚本自己 `UnicodeEncodeError`，于是"修法"提示根本看不到）。
if hasattr(sys.stdout, "reconfigure"):  # pragma: no cover - 环境相关
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "scripts" / "generate_orm_models.py"
COMMITTED = ROOT / "shared" / "models"

#: **手写**、不由生成器产出的文件（生成器只产 `models.yaml` 里的模型 + `__init__.py`）。
#: 实测：当前只有 `base.py`（`Base` 声明，`__init__.py` 的 `from .base import Base` 指向它）。
#: 显式列白名单而不是"忽略所有多出来的文件" —— 否则"生成器不再产某个模型"这类漂移会被静默吞掉。
HANDWRITTEN = {"base.py"}

#: 模板每次运行都会写当前时间 ⇒ 比对时必须归一，否则永远"有漂移"。
TIME_LINE = re.compile(r"^生成时间：.*$", re.MULTILINE)


def load_generator():
    """按文件路径导入生成器（它不在包内，且 `__main__` 会直接跑）。"""
    spec = importlib.util.spec_from_file_location("_generate_orm_models", GENERATOR)
    if spec is None or spec.loader is None:  # pragma: no cover - 环境异常
        raise RuntimeError(f"无法加载生成器：{GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalized(text: str) -> str:
    return TIME_LINE.sub("生成时间：<ignored>", text)


def first_diff(expected: str, actual: str) -> str:
    # strict=False 是**有意**的：只比对公共前缀，行数不同由下面的分支报告
    # （strict=True 会在这里抛 ValueError，把"行数不同"变成崩溃）。
    for i, (a, b) in enumerate(
        zip(expected.splitlines(), actual.splitlines(), strict=False), start=1
    ):
        if a != b:
            return f"首个不同在第 {i} 行：\n      生成器: {a[:100]}\n      已提交: {b[:100]}"
    return "行数不同（前面逐行相同）"


def main() -> int:
    if not GENERATOR.exists() or not COMMITTED.is_dir():
        print(f"守卫自身失效：找不到 {GENERATOR} 或 {COMMITTED}")
        return 1

    module = load_generator()

    with tempfile.TemporaryDirectory(prefix="orm-freshness-") as tmp:
        tmpdir = pathlib.Path(tmp)
        module.OUTPUT_DIR = tmpdir  # 关键：绝不写仓库
        # 生成器自己会 print 一堆进度，守卫只关心差异 ⇒ 吞掉
        with contextlib.redirect_stdout(io.StringIO()):
            module.generate_all()

        generated = sorted(p for p in tmpdir.glob("*.py"))
        committed = sorted(p for p in COMMITTED.glob("*.py"))

        problems: list[str] = []

        gen_names = {p.name for p in generated}
        com_names = {p.name for p in committed}

        for name in sorted(com_names - gen_names - HANDWRITTEN):
            problems.append(f"[X] {name}：在仓库里存在，但生成器不再产出（yaml 里删了？）")
        for name in sorted(gen_names - com_names):
            problems.append(f"[X] {name}：生成器会产出，但仓库里没有（漏提交？）")

        for name in sorted(gen_names & com_names):
            exp = normalized((tmpdir / name).read_text(encoding="utf-8"))
            act = normalized((COMMITTED / name).read_text(encoding="utf-8"))
            if exp != act:
                problems.append(f"[X] {name}：与 models.yaml 漂移 —— {first_diff(exp, act)}")

        if problems:
            print(f"ORM 生成物与 configs/orm/V1/models.yaml 不一致：{len(problems)} 处")
            for line in problems:
                print(f"  {line}")
            print("\n修法：改 `configs/orm/V1/models.yaml`，再跑 `python scripts/generate_orm_models.py`")
            print("（生成器会给**全部**模型刷新「生成时间」表头 ⇒ 提交前只保留真实变化的文件）")
            return 1

        print(
            f"ORM 生成物与 yaml 一致：{len(committed)} 个文件"
            f"（比对时忽略「生成时间」行；生成器输出到临时目录，仓库未被改写）"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
