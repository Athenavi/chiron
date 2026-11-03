"""可选后端（extra）的导入方式守卫。

**为什么需要它**：判断"这套 Python 环境能不能用"的唯一可靠判据是**真实 import 整个
`app` 包**（等价于 `pkgutil.walk_packages` + 逐模块 import）。于是模块顶层的
`import boto3` 就把一个**可选 extra** 变成了硬要求 ——
`requirements.txt`（打包态引擎 venv 唯一的依赖来源）刻意不含它，
所以真机上引擎环境装完 340MB、探测仍判"缺少模块 boto3"，本地模式的聊天/知识库/记忆全起不来，
而 CI 全绿（`requirements-dev.txt` 里装了 boto3，所以开发机永远看不见这个缺口）。

两条判据分工：

1. **源码守卫**（通用）：`app/` 下任何模块都不许在**模块顶层**导入
   `[project.optional-dependencies]` 里声明的包。extra 的名字从 pyproject 读，
   新增一个 extra 就自动纳入，不需要维护清单。
2. **行为用例**：被 `sys.meta_path` 挡住 boto3 时，`app.media.s3_store` 仍要能 import；
   而真的去构造 `S3Store` 时必须**响亮地**失败（否则"配了 S3 却静默走本地盘"更难查）。
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def _extra_import_names() -> set[str]:
    """pyproject 里每个 optional extra 对应的顶层导入名。

    发行名 → 导入名只做"小写 + 连字符转下划线"这一层归一（`sentence-transformers` →
    `sentence_transformers`、`boto3` → `boto3`）。这一层不够用时（如 `Pillow` → `PIL`）
    说的是**必需依赖**，而必需依赖由探测本身负责，不在本守卫的射程内。
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data.get("project", {}).get("optional-dependencies", {}) or {}
    names: set[str] = set()
    for requires in extras.values():
        for item in requires:
            spec = str(item).strip()
            if not spec:
                continue
            # 切掉版本约束 / extras / 环境标记，只留发行名
            for separator in ("[", ">", "<", "=", "!", "~", ";", " "):
                spec = spec.split(separator)[0]
            if spec:
                names.add(spec.lower().replace("-", "_"))
    return names


def _module_level_imports(path: Path) -> set[str]:
    """该文件**模块顶层**的导入根名。

    只看 `tree.body` 一层：函数/方法体内的延迟导入是正确写法，`if TYPE_CHECKING:`
    块里的导入不会执行，两者都不会被遍历到。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_module_level_import_of_optional_extras() -> None:
    forbidden = _extra_import_names()
    assert forbidden, "pyproject 没读到 optional extras，这条守卫等于没跑"

    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        hit = _module_level_imports(path) & forbidden
        if hit:
            offenders.append(f"{path.relative_to(ROOT)}: {', '.join(sorted(hit))}")

    assert not offenders, (
        "可选 extra 只能在用到的地方延迟导入（函数体内），否则桌面端"
        "「真实 import 整包」的就绪判据会把可选后端变成硬依赖：" + "; ".join(offenders)
    )


def test_s3_store_imports_without_boto3() -> None:
    """挡住 boto3/botocore 后重新 import `app.media.s3_store`，必须成功。"""
    script = """
import sys


class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name == "boto3" or name.startswith("botocore"):
            raise ModuleNotFoundError("No module named %r" % name, name=name)
        return None


for mod in [m for m in list(sys.modules) if m.split(".")[0] in ("boto3", "botocore")]:
    del sys.modules[mod]
sys.meta_path.insert(0, _Blocker())

import app.media.s3_store as s3

print("IMPORT_OK")
try:
    s3.S3Store("bucket")
except ModuleNotFoundError as exc:
    print("CONSTRUCT_%s" % (exc.name or ""))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert "IMPORT_OK" in output, f"挡住 boto3 后 s3_store 仍导不进来：{output}"
    # 真的要用 S3 时必须报"缺模块"，而不是悄悄降级成别的后端
    assert "CONSTRUCT_boto3" in output, f"构造 S3Store 没有因缺少 boto3 而失败：{output}"


def test_s3_store_keeps_lazy_import_contract() -> None:
    """直接在本进程里确认：模块可导入，且 `boto3` 不在它的命名空间里。

    与上一条互补 —— 上一条证明"挡住也能 import"，这一条证明顶层确实没留引用。
    """
    module = importlib.import_module("app.media.s3_store")
    assert not hasattr(module, "boto3"), "顶层又 import 了 boto3：探测会把可选 extra 当硬依赖"
    assert not hasattr(module, "BotoConfig")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
