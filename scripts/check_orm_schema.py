#!/usr/bin/env python3
"""ORM 模型 ↔ 迁移 DDL 的**列集合**守卫（路线图 L4-3 的补集）。

**为什么需要第二个脚本**：`scripts/check_orm_models.py` 只保证「**yaml ↔ 生成物**」；
而第 68 轮那次漂移的**方向**是「**迁移有、yaml 没有**」—— 那时没有任何检查覆盖它。
实测（2026-10-09）：补之前 `DDL 有、模型没有` = **12 列 / 5 表**（`sessions` 的 6 个分支血缘列、
`turns` 的 `model`/`cache_hit`/`cached_tokens`、`messages.source`、`subagent_runs.rerun_of`、
`unified_sessions.runtime`），补完双向为 0。

**判据**：把 `shared/models/*.py` 用 `ast` 解出 `__tablename__` 与 `Column(...)` 赋值，
再与 `alembic upgrade head --sql` 渲染出的 DDL（离线，无需数据库）比对：
  · 两边都有的表 ⇒ 列集合必须**完全一致**（两个方向都查）；
  · 只有模型、没有 DDL 的表 ⇒ 失败（模型描述了不存在的表）；
  · 只有 DDL、没有模型的表 ⇒ 必须**在下面 UNMODELED 白名单里**（显式登记，不是静默忽略）。

**为什么解析生成物而不是 yaml**：这样**不需要 PyYAML**（CI 的 schema job 只装
`requirements-migrate.txt`，里面没有 PyYAML），而且「yaml ↔ 生成物」已由另一个守卫保证
⇒ 两个守卫串起来就是「迁移 ↔ yaml」。

**为什么放在 schema job**：那一步本来就在渲染 DDL，且有 alembic。

退出码：0 = 一致；1 = 有漂移。
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "shared" / "models"

#: **有意不建 ORM 模型**的表（显式登记；新增一张却忘了建模 ⇒ 这里会红）。
#: `alembic_version` 是 Alembic 自己的记账表，不算业务表。
UNMODELED = {
    "alembic_version",
    "ent_mail_config",
    "ent_model_routes",
    "ent_sms_config",
    "ent_webhooks",
    "llm_provider_keys",
    "session_map_nodes",
    "session_map_workspaces",
    "task_idempotency",
    "user_memory_entries",
}

CONSTRAINT = re.compile(r"^\s*(CONSTRAINT|PRIMARY KEY|FOREIGN KEY|UNIQUE|CHECK|EXCLUDE)\b", re.I)
IDENT = re.compile(r"^\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s+\S")


def model_tables() -> dict[str, set[str]]:
    """从生成物里解出 `{表名: {列名}}`（只认 `Column(...)` 赋值）。"""
    tables: dict[str, set[str]] = {}
    for path in sorted(MODELS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            table = None
            columns: set[str] = set()
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                    if not isinstance(target, ast.Name):
                        continue
                    if target.id == "__tablename__" and isinstance(stmt.value, ast.Constant):
                        table = str(stmt.value.value)
                    elif (
                        isinstance(stmt.value, ast.Call)
                        and isinstance(stmt.value.func, ast.Name)
                        and stmt.value.func.id == "Column"
                    ):
                        # **列名优先取 `Column(...)` 的第一个位置参数**（若它是字符串常量）：
                        # 生成器会把 `metadata` 这类与 `Base.metadata` 冲突的属性改名为
                        # `metadata_data = Column('metadata', …)` ⇒ 用属性名比对会**假阳性**
                        # （实测 5 张表：admin_cron_jobs / knowledge_chunks / knowledge_documents /
                        #  media_assets / unified_messages）。
                        first = stmt.value.args[0] if stmt.value.args else None
                        if isinstance(first, ast.Constant) and isinstance(first.value, str):
                            columns.add(first.value)
                        else:
                            columns.add(target.id)
            if table:
                tables.setdefault(table, set()).update(columns)
    return tables


def ddl_tables() -> dict[str, set[str]]:
    """渲染迁移链的 DDL 并解出 `{表名: {列名}}`（离线，只用来定方言）。"""
    env = dict(os.environ)
    env.setdefault("DATABASE_DSN", "postgresql://chiron:chiron@localhost:5432/chiron")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head", "--sql"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    if proc.returncode != 0:
        print("守卫自身失效：alembic 渲染 DDL 失败")
        print((proc.stderr or proc.stdout)[-1500:])
        raise SystemExit(1)

    ddl = proc.stdout
    tables: dict[str, set[str]] = {}
    # CREATE TABLE（带 `public.` 前缀；列行形如 `    name character varying(36) NOT NULL,`）
    for m in re.finditer(
        r"CREATE TABLE (?:IF NOT EXISTS )?(?:public\.)?[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s*\((.*?)\n\);",
        ddl, re.S,
    ):
        cols = set()
        for line in m.group(2).splitlines():
            line = line.split("--")[0].rstrip().rstrip(",")
            if not line.strip() or CONSTRAINT.match(line):
                continue
            im = IDENT.match(line)
            if im:
                cols.add(im.group(1))
        tables.setdefault(m.group(1), set()).update(cols)
    # ALTER TABLE ... ADD COLUMN（**两个坑**：`ADD COLUMN IF NOT EXISTS`；语句**跨行** ⇒ 用 \s+）
    for m in re.finditer(
        r"ALTER TABLE (?:public\.)?[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s+"
        r"ADD COLUMN (?:IF NOT EXISTS )?[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?",
        ddl,
    ):
        tables.setdefault(m.group(1), set()).add(m.group(2))
    return tables


def escape_canary() -> list[str]:
    """**转义金丝雀**：喂一条含单引号 / 反斜杠的 description，生成物必须仍是合法 Python。

    为什么需要它：模板把 doc 渲染进单引号字符串（`doc='{{ doc }}'`）。2026-10-09 实测过一次
    —— `unified_session.py` 写成 `doc='…'{}'…'` ⇒ **语法错误的 Python**，而 yaml↔生成物守卫只比
    文本、`shared/` 不在 ruff/mypy 内、引擎也不 import 它 ⇒ 只有 `ast.parse` 看得见。
    当前 yaml 里**没有**任何 description 带单引号 ⇒ 光靠上面的比对**测不到模板回归**；
    这里主动喂一条恶意输入，把"模板转义"这件事钉住。不碰仓库（yaml 与输出都走临时目录）。
    """
    import contextlib
    import importlib.util
    import io
    import tempfile

    try:
        import yaml
    except ImportError:  # schema job 只装 requirements-migrate.txt，没有 PyYAML
        return []  # 金丝雀跳过（比对本身不依赖 PyYAML）

    problems: list[str] = []
    nasty = r"含单引号 ' 与反斜杠 \ 与 '{}' 的说明"
    with tempfile.TemporaryDirectory(prefix="orm-escape-") as tmp:
        tmpdir = pathlib.Path(tmp)
        try:
            doc = yaml.safe_load((ROOT / "configs/orm/V1/models.yaml").read_text(encoding="utf-8"))
            doc["models"]["UnifiedSession"]["properties"]["runtime"]["description"] = nasty
            evil = tmpdir / "models_evil.yaml"
            evil.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

            spec = importlib.util.spec_from_file_location(
                "_gen_canary", ROOT / "scripts" / "generate_orm_models.py"
            )
            if spec is None or spec.loader is None:
                return []
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.MODELS_YAML = evil
            mod.OUTPUT_DIR = tmpdir / "out"
            mod.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            with contextlib.redirect_stdout(io.StringIO()):
                mod.generate_all()
        except Exception as exc:  # noqa: BLE001 - 金丝雀自身出错不应掩盖主检查
            return [f"[X] 转义金丝雀自身失败：{exc!r}"]

        for p in sorted((tmpdir / "out").glob("*.py")):
            try:
                ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                problems.append(
                    f"[X] 转义金丝雀：喂入含引号的 description 后 {p.name} 不是合法 Python（{exc.msg}）"
                    " —— 模板里的 doc 转义被改坏了？"
                )
    return problems


def main() -> int:
    models = model_tables()
    ddl = ddl_tables()
    problems: list[str] = escape_canary()

    for table in sorted(set(models) - set(ddl)):
        problems.append(f"[X] {table}：模型里有，但迁移链里没有这张表")
    for table in sorted(set(ddl) - set(models) - UNMODELED):
        problems.append(f"[X] {table}：迁移链里有，但既没有模型、也不在 UNMODELED 白名单里")

    for table in sorted(set(models) & set(ddl)):
        miss = sorted(ddl[table] - models[table])
        extra = sorted(models[table] - ddl[table])
        if miss:
            problems.append(f"[X] {table}：迁移有、模型没有 -> {miss}")
        if extra:
            problems.append(f"[X] {table}：模型有、迁移没有 -> {extra}")

    if problems:
        print(f"ORM 模型与迁移 DDL 不一致：{len(problems)} 处")
        for line in problems:
            print(f"  {line}")
        print("\n修法：把缺的列按迁移的**权威类型**补进 `configs/orm/V1/models.yaml`，"
              "再跑 `python scripts/generate_orm_models.py`（只提交真实变化的文件）")
        return 1

    print(
        f"ORM 模型与迁移 DDL 一致：{len(models)} 张表逐列对齐"
        f"（另 {len(UNMODELED)} 张表显式登记为不建模：{', '.join(sorted(UNMODELED))}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
