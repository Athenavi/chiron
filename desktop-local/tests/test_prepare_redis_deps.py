"""钉住 ``prepare_redis.py`` 的构建期依赖，别让 CI 与脚本再次脱节。

**为什么值得写**：这个脚本的自检要真起一次 redis 并跑契约命令，因此它会
``import app.config``（pydantic / pydantic-settings）以及 ``redis.asyncio``。
而 CI 里一度只装 actions/setup-python 的裸解释器 —— 注释还写着"这些脚本都只用
标准库"。结果是 macOS 与 Linux 的 job 在 ``from app.config import Settings``
处直接 ``ModuleNotFoundError``，连锁到 redis 打包步骤失败。

所以这里做**双向对齐检查**：把脚本（含它经由 ``app.*`` 触达的模块）真正 import 的
第三方顶层模块收集出来，逐个断言它们出现在 workflow 的 pip 安装行里。
新增导入却忘了改 CI 时，这条会先红。

只查 import 名，不查版本号 —— 版本由 ``desktop-local/pyproject.toml`` 管。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

DESKTOP_LOCAL = Path(__file__).resolve().parents[1]
REPO_ROOT = DESKTOP_LOCAL.parent
SCRIPT = DESKTOP_LOCAL / "scripts" / "prepare_redis.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "desktop.yml"

# 本仓库自己的包，不是第三方依赖
LOCAL_PACKAGES = {"app"}

# 函数体内的**惰性导入**。它们不会在 import 时失败，所以真正的判定依据是
# "自检会不会走到那条分支"。下面两组都不是猜的，依据是实测：
# 在只装了 pydantic / pydantic-settings / redis 的干净 venv 里跑
# `python scripts/prepare_redis.py --check`，8/8 条契约命令全 PASS。

# 1) 惰性但**必须**装：`app/storage/redis.py` 在 `_start_bundled()` 里
#    `import redis.asyncio`，而自检正是走 bundled 路径。
LAZY_REQUIRED = {
    "redis",
}

# 2) 惰性且**不需要**装：全部位于 PG 连接 / alembic 迁移路径，
#    而自检构造的是 `Settings(pg_enabled=False)`。
LAZY_NOT_NEEDED = {
    "alembic",     # app/storage/pg.py 迁移路径
    "asyncpg",     # 应用层异步访问 PG
    "dotenv",      # pg.py 读 .env
    "psycopg2",    # alembic 走 psycopg2 方言
}


def _top_level_imports(tree: ast.Module) -> set[str]:
    """只收集**模块顶层**的 import：这些一定会执行，缺一个就 import 失败。"""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _all_imports(tree: ast.AST) -> set[str]:
    """收集**所有**（含函数体内的惰性）import 的顶层模块名。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 是相对导入，没有顶层模块名
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _reachable_app_modules(script: Path) -> list[Path]:
    """沿 ``app.*`` 导入闭包找齐所有会被加载的模块文件。"""
    seen: dict[str, Path] = {}
    queue = [script]
    while queue:
        path = queue.pop()
        if not path.is_file() or path in seen.values():
            continue
        seen[str(path)] = path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] not in LOCAL_PACKAGES:
                    continue
                target = DESKTOP_LOCAL / Path(*node.module.split("."))
                for candidate in (target.with_suffix(".py"), target / "__init__.py"):
                    if candidate.is_file():
                        queue.append(candidate)
    return list(seen.values())


def _third_party_modules() -> set[str]:
    """脚本闭包里**模块顶层**出现的、非标准库也非本仓库的模块名。

    只算顶层是有意为之：顶层 import 缺包必然一进去就 ImportError；
    函数体内的惰性导入取决于运行到哪条分支，由 ``LAZY_NOT_NEEDED`` 单独管。
    """
    files = _reachable_app_modules(SCRIPT)
    assert len(files) > 1, "没解析到 app.* 依赖，说明测试本身失效了"
    names: set[str] = set()
    for path in files:
        names |= _top_level_imports(ast.parse(path.read_text(encoding="utf-8")))
    stdlib = set(sys.stdlib_module_names)
    return {n for n in names if n not in stdlib and n not in LOCAL_PACKAGES}


def _ci_pip_line() -> str:
    """取出 workflow 里"安装构建脚本依赖"那一步的 pip 命令行。"""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if "name: Install build-script dependencies" in line:
            for follow in lines[index + 1 : index + 8]:
                if "pip install" in follow:
                    return follow
            raise AssertionError("找到了步骤但没有 pip install 行")
    raise AssertionError("workflow 里没有 'Install build-script dependencies' 步骤")


def test_ci_installs_every_third_party_import_of_prepare_redis():
    required = _third_party_modules() | LAZY_REQUIRED
    # 依赖链至少真被解析到了：pydantic 经 app.config，redis 经 app.storage.redis
    assert {"pydantic", "pydantic_settings", "redis"} <= required, (
        f"依赖链解析异常：{sorted(required)}"
    )

    pip_line = _ci_pip_line()
    missing = [
        name
        for name in sorted(required)
        # 包名约定：模块名里的下划线换成连字符（pydantic_settings → pydantic-settings）
        if name.replace("_", "-") not in pip_line
    ]
    assert not missing, (
        f"prepare_redis.py 需要 {missing}，但 workflow 只装了：{pip_line.strip()}"
    )


def test_no_unclassified_lazy_import_sneaks_past_the_dependency_check():
    """惰性导入必须被显式归类，否则可能是"自检要走却没人装"的漏网之鱼。

    这条是上面那条的护栏：新增一个函数体内 import 时，作者必须想一遍
    "自检会不会走到"，要么进 LAZY_REQUIRED（→ 要求 CI 装上），
    要么进 LAZY_NOT_NEEDED（→ 说明为什么不装）。默认放行就等于回到
    mac/linux 那个 ModuleNotFoundError。
    """
    files = _reachable_app_modules(SCRIPT)
    everything: set[str] = set()
    for path in files:
        everything |= _all_imports(ast.parse(path.read_text(encoding="utf-8")))
    third_party = everything - set(sys.stdlib_module_names) - LOCAL_PACKAGES
    classified = _third_party_modules() | LAZY_REQUIRED | LAZY_NOT_NEEDED
    assert third_party <= classified, (
        f"这些惰性导入没有归类：{sorted(third_party - classified)}；"
        "自检会走到就加进 LAZY_REQUIRED，走不到就加进 LAZY_NOT_NEEDED 并写明理由"
    )
