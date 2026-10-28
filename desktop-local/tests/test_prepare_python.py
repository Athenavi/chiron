"""``scripts/prepare_python.py`` 的单元测试。

**为什么值得写**：这个脚本决定安装包里那份解释器**能不能建出环境**。
它出错的方式都很安静 —— 复制到错的层级、漏掉解释器、版本对不上 —— 表现出来
是"用户首次启动永远卡在创建环境、所有页面报服务繁忙"。必须在这里钉住。

所有用例**不联网**：`--from` 是我们唯一的离线路径，也是 CI 之外能测的路径。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    """``scripts/`` 不是包，按文件路径加载。"""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prepare_python = _load("prepare_python")


def _make_layout(root: Path, *, with_exe: bool = True) -> Path:
    """造一个 uv 风格的安装目录：版本目录里放解释器。"""
    version = root / "cpython-3.12.16-windows-x86_64-none"
    version.mkdir(parents=True)
    if with_exe:
        exe = version / ("python.exe" if sys.platform == "win32" else "bin/python3")
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"")
        if sys.platform != "win32":
            exe.chmod(0o755)
    return version


def test_platform_key_uses_node_spelling():
    """键必须是 `<process.platform>-<arch>` 写法，否则壳层按它找不到目录。"""
    key = prepare_python.current_platform_key()
    assert key.split("-")[0] in {"win32", "darwin", "linux"}
    assert key.split("-")[1] in {"x64", "arm64"}


def test_find_installed_layout_skips_management_entries(tmp_path):
    """uv 会在安装根放 .temp/.lock/.gitignore 和一个 junction。

    只认"带解释器的实体目录"，否则复制出来的目录名不可预测。
    """
    (tmp_path / ".temp").mkdir()
    (tmp_path / ".lock").write_bytes(b"")
    version = _make_layout(tmp_path)

    found = prepare_python.find_installed_layout(tmp_path)
    assert found is not None
    assert found.name == version.name
    assert prepare_python.interpreter_in(found) is not None


def test_find_installed_layout_returns_none_without_interpreter(tmp_path):
    """目录在但没解释器 → 必须返回 None，让调用方显式失败而不是装个空壳。"""
    _make_layout(tmp_path, with_exe=False)
    assert prepare_python.find_installed_layout(tmp_path) is None


def test_copy_interpreter_keeps_version_directory(tmp_path, monkeypatch):
    """复制后必须是 `<platform>/<版本目录>/python[.exe]`。

    壳层 `bundledPython()` 在 `<platform>/` 下逐个找子目录 —— 少一层就找不到解释器。
    """
    assets = tmp_path / "assets"
    monkeypatch.setattr(prepare_python, "PYTHON_ASSETS_DIR", assets)
    (tmp_path / ".temp").mkdir()

    source = _make_layout(tmp_path / "src")
    (source / ".temp").mkdir()  # 应被清掉
    (source / "BUILD").write_bytes(b"")

    dest = prepare_python.copy_interpreter(source, "win32-x64")

    assert dest.parent.name == "win32-x64"
    assert dest.name == source.name, "版本目录名要保留，版本升级时不该改 JS"
    assert prepare_python.interpreter_in(dest) is not None
    assert not (dest / ".temp").exists(), "下载缓存不该进安装包"
    assert not (dest / "BUILD").exists()


def test_uv_binary_reports_missing_with_actionable_hint(tmp_path, monkeypatch):
    """没有 uv 时要给出可照做的命令，而不是一句"找不到文件"。"""
    monkeypatch.setattr(prepare_python, "UV_ASSETS_DIR", tmp_path / "nope")
    try:
        prepare_python.uv_binary("win32-x64")
    except SystemExit as exc:
        assert "prepare_uv.py" in str(exc)
    else:  # pragma: no cover - 走到这里说明没抛
        raise AssertionError("缺 uv 时必须失败")


def test_runtime_version_matches_shell_layer():
    """构建脚本与壳层各自写死版本号，必须一致，否则装出的环境与声明不符。"""
    shell = (
        Path(__file__).resolve().parents[2] / "frontend-vue" / "electron" / "runtime-install.cjs"
    ).read_text(encoding="utf-8")
    assert f"RUNTIME_PYTHON_VERSION = '{prepare_python.RUNTIME_PYTHON_VERSION}'" in shell
