"""代码层静态/运行时安全守卫（叶子模块 — 不得 import 任何 app 内模块）。

本模块被两处复用，是单一事实来源，勿在别处复制守卫逻辑：
- app/tools/run_code.py（进程内 PTC 沙箱）
- plugin_runner.py（插件 subprocess 沙箱，按文件路径加载本模块以避开
  app.tools.__init__ 的重量级 import 链）

说明：这是 A 层（代码层）约束——提高绕过门槛；完整隔离仍需部署侧 B 层
（容器/低权限/subprocess，见 plugin_runner.py）。
"""

from __future__ import annotations

import ast
import builtins as _b
from collections.abc import Callable
from typing import Any, NoReturn

# ── 静态安全检查：禁止沙箱代码直接访问宿主 ─────────────────────────────
DANGEROUS_MODULES = {
    "os",
    "subprocess",
    "sys",
    "ctypes",
    "socket",
    "importlib",
    "pathlib",
    "shutil",
    "tempfile",
    "glob",
    "atexit",
    "signal",
    "multiprocessing",
    "pickle",
    "marshal",
    "dbm",
    "shelve",  # 防止序列化攻击
    "asyncio.tasks",
    # asyncio 是**白名单模块**（模型代码常要 `await asyncio.sleep()`），但它夹带一个
    # 能起进程的子模块：`asyncio.subprocess.create_subprocess_exec/shell` ⇒ **任意命令执行**，
    # 绕开工具白名单与 exec 审计。此前只拦 `asyncio.tasks`，而这段比较只看**根段**（见
    # `_module_blocked`），那条点分条目其实从未生效 —— 这个洞是补 `_module_blocked` 时一并修掉的。
    "asyncio.subprocess",
    "http.client",
    "urllib",
    "requests",  # 防止网络请求
    "telnetlib",
    "ftplib",
    "smtplib",
    "imaplib",
    "poplib",  # 防止协议攻击
}
DANGEROUS_CALLS = {
    "open",
    "exec",
    "eval",
    "compile",
    "__import__",
    "input",
    "breakpoint",
}
# 注意：getattr/setattr 等反射函数不在静态层封杀 —— 静态层无法识别动态构造的
# 混淆调用，这类攻击由运行时守卫（_safe_builtins stub）兜底拦截（纵深防御）。

DANGEROUS_ATTR_PATHS = (
    "os.system",
    "os.popen",
    "os.startfile",
    "os.remove",
    "os.rename",
    "os.chdir",
    "os.mkdir",
    "os.makedirs",
    "sys.exit",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "socket.socket",
    "shutil.rmtree",
    "shutil.copy",
    "shutil.move",
    "pathlib.Path",
)

# 对象内省/逃逸链属性（S 修复）：堵死 __class__.__mro__[].__subclasses__()
# 一类访问宿主模块的经典沙箱逃逸。这些是对象属性访问,运行时 __builtins__
# stub 拦不到(不经 getattr),故在静态层直接封禁属性名。沙箱代码无合法需要。
BLOCKED_SUBCLASS_ATTRS = frozenset(
    {
        "__class__",
        "__subclasses__",
        "__base__",
        "__bases__",
        "__mro__",
        "__dict__",
        "__globals__",
        "__closure__",
        "__code__",
        "__getattribute__",
        "__build_class__",
    }
)


def _module_blocked(name: str) -> bool:
    """按**完整点分名逐级前缀**判定模块是否被禁。

    为什么不能只看根段：`DANGEROUS_MODULES` 里有 `asyncio.tasks` / `http.client` 这类
    **点分条目**，而"取根段比较"（`name.split('.')[0] in DANGEROUS_MODULES`）永远匹配不到它们
    —— 于是 `import asyncio.subprocess` 这种写法**全部放行**（实测可拿到
    `create_subprocess_exec`，等于绕开沙箱起进程）。逐级前缀比较让这些条目真正生效，
    同时保留 `import os` / `import os.path` 都被拦的效果。
    """
    if not name:
        return False
    parts = name.split(".")
    return any(".".join(parts[: i + 1]) in DANGEROUS_MODULES for i in range(len(parts)))


def check_static(code: str) -> str | None:
    """AST 静态检查沙箱代码：命中危险模块导入/危险调用返回原因，否则 None。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # 语法错误由执行阶段结构化返回

    for node in ast.walk(tree):
        # import os / import pathlib / from X import Y
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_blocked(alias.name):
                    return f"module '{alias.name}' is not allowed — use tools.* instead"
        if isinstance(node, ast.ImportFrom):
            if _module_blocked(node.module or ""):
                return f"module '{node.module}' is not allowed — use tools.* instead"
            # `from asyncio import subprocess`：module 段是合法的 asyncio，危险在被导入的名字上
            for alias in node.names:
                combined = f"{node.module}.{alias.name}" if node.module else alias.name
                if _module_blocked(combined):
                    return f"name '{combined}' is not allowed — use tools.* instead"

        # open(...) / exec(...) / eval(...) / __import__(...) 等危险调用
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in DANGEROUS_CALLS:
                return f"call '{fn.id}()' is not allowed — use tools.* instead"
            # os.system / subprocess.run / sys.exit / socket.* 属性调用
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                attr_path = f"{fn.value.id}.{fn.attr}"
                if attr_path in DANGEROUS_ATTR_PATHS:
                    return f"call '{attr_path}()' is not allowed — use tools.* instead"

        # 封禁对象内省/逃逸链属性(.__class__.__mro__[].__subclasses__() 等)
        if isinstance(node, ast.Attribute):
            if node.attr in BLOCKED_SUBCLASS_ATTRS:
                return f"attribute '{node.attr}' is not allowed (object introspection)"

    # 注：混淆访问(字符串拼接/'__builtins__' 索引等)不被静态层识别,由运行时
    # _safe_builtins stub 兜底;而 __class__/__subclasses__ 等对象属性访问不经
    # __builtins__ 拦截,故在静态层封禁(见上)。两层纵深防御。
    return None


# ── 运行时守卫（堵静态守卫绕过，如 __builtins__['ope'+'n'] 动态构造） ──
# 方案：替换 exec 命名空间的 builtins —— open/exec/eval 等 builtin 全被 stub（raise），
# __import__ 白名单化（仅安全标准库）。沙箱代码无法拿到 os/subprocess 模块，
# 动态构造 builtin 调用也被 stub 拦截。（settrace/monitoring 对 asyncio 协程与
# builtin 调用均不可靠，实测弃用。）
# S4 强化：移除 io/copy/operator/enum — 这些模块提供元编程链可触达 os/socket，
# 导致沙箱逃逸。模型代码不需要它们（文件 IO 由 tools 注入 namespace 提供）。
# asyncio 保留：模型代码常需 `await asyncio.sleep()`/`asyncio.gather()`；但它的危险子模块
# （`asyncio.subprocess`，能起进程）**必须显式禁用** —— 见 `DANGEROUS_MODULES` 里的条目与
# `_module_blocked`（静态 + 运行时两层都拦；早先只在注释里声称"由 DANGEROUS_ATTR_PATHS 拦截"，
# 而那里并没有对应条目，属**注释声称了不存在的防护**）。
SAFE_IMPORTS = frozenset(
    {
        "json",
        "math",
        "random",
        "datetime",
        "re",
        "collections",
        "itertools",
        "typing",
        "time",
        "functools",
        "decimal",
        "string",
        "asyncio",
    }
)
BLOCKED_BUILTINS = frozenset({"open", "exec", "eval", "compile", "input", "breakpoint"})
# 反射攻击函数
BLOCKED_REFLECTIVE = frozenset(
    {"getattr", "setattr", "delattr", "dir", "vars", "locals", "globals"}
)


def _make_blocked_builtin(name: str) -> Callable[..., Any]:
    def _blocked(*_args: Any, **_kwargs: Any) -> NoReturn:  # noqa: ANN002, ANN003 — 桩函数
        raise RuntimeError(f"blocked by runtime guard: {name}()")

    return _blocked


def safe_builtins() -> dict[str, Any]:
    """构造受控 builtins：安全库白名单导入 + 危险 builtin 全部 stub。"""
    orig = vars(_b).copy()
    real_import = orig["__import__"]

    def _guarded_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:  # noqa: A002
        root = (name or "").split(".")[0]
        # 两层都要判：根在白名单里、**且**没有命中禁用的点分条目 —— 否则
        # `import asyncio.subprocess` 会因为根是合法的 asyncio 而一路放行（能起进程）。
        if root in SAFE_IMPORTS and not _module_blocked(name):
            return real_import(name, globals, locals, fromlist, level)
        raise RuntimeError(f"blocked by runtime guard: import '{name}'")

    # 阻塞危险内置函数
    for name in BLOCKED_BUILTINS:
        orig[name] = _make_blocked_builtin(name)

    # 阻塞反射攻击函数
    for name in BLOCKED_REFLECTIVE:
        orig[name] = _make_blocked_builtin(name)

    # 替换 __import__ 为安全版本
    orig["__import__"] = _guarded_import

    # 移除潜在的危险属性访问
    orig.pop("__builtins__", None)  # 防止通过 __builtins__ 访问内置函数

    return orig
