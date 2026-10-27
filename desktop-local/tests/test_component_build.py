"""``scripts/build_component.py``（真实组件包的构建脚本）的测试。

**不联网**：不 ``pip install`` 任何包。依赖用一个写进 venv site-packages 的**假包**代替
（``build(installer=...)`` 是脚本留的注入点），所以整条链路 —— 建 venv → 装配
payload → 打 zip → 写 manifest —— 都走真实代码，只有"从哪里弄来依赖"被换掉。

**会建真 venv / 起真进程**：两个集成用例分别跑 ``python -m venv`` 与 manager 的
``start()``（spawn ``run.py``，轮询 ``/healthz``），都自己收拾干净。

覆盖重点：

- manifest 能被 ``app.components.manager.parse_manifest`` 接受（构建端与安装端同一套契约）
- bundle.zip 的条目、sha256、以及"归档里没有符号链接"
- 路径穿越（``..`` / 绝对路径 / 盘符）在**脚本侧**与 **manager 侧**被一致拒绝
- payload 体积上限自检（单文件 / 总量，与 manager 的常量对齐）
- ``--verify`` 能查出被篡改的 bundle
- 端到端：产出的组件源能被 manager 真装上、真启动起来
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from app.components import (
    COMPONENT_NAMES,
    ComponentManager,
    ComponentSource,
    UnsafeEntryError,
    parse_manifest,
    safe_relative,
)
from app.components.manager import extract_archive

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
BUILD_SCRIPT = SCRIPTS_DIR / "build_component.py"
REQUIREMENTS_DIR = SCRIPTS_DIR / "components"


# --------------------------------------------------------------------- 夹具
@pytest.fixture(scope="module")
def build_module() -> ModuleType:
    """按文件路径加载 ``scripts/build_component.py``（它不是包，import 不到）。"""
    assert BUILD_SCRIPT.is_file(), f"构建脚本不存在：{BUILD_SCRIPT}"
    spec = importlib.util.spec_from_file_location("chiron_build_component", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def venv_python() -> Path:
    """建**一次** venv 给模块内共用（Windows 上约 3-6 秒）。"""
    import subprocess
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="chiron-build-test-venv-"))
    try:
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(root / "venv")],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"无法创建 venv：{result.stderr.strip()[:300]}")
        if sys.platform == "win32":
            candidate = root / "venv" / "Scripts" / "python.exe"
        else:
            candidate = root / "venv" / "bin" / "python"
        if not candidate.is_file():
            pytest.skip(f"venv 里没有解释器：{candidate}")
        yield candidate
    finally:
        shutil.rmtree(root, ignore_errors=True)


def fake_dependency_files(venv_py: Path) -> Path:
    """往 venv 的 purelib 里写一个假包，返回该目录（模拟 ``pip install`` 的结果）。"""
    import subprocess

    code = "import sysconfig; print(sysconfig.get_paths()['purelib'])"
    purelib = Path(
        subprocess.run([str(venv_py), "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    )
    pkg = purelib / "fakepkg"
    (pkg / "sub").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text('"""假组件包，只用于构建脚本的测试。"""\n__version__ = "0.0.1-test"\n', "utf-8")
    (pkg / "sub" / "helper.py").write_text("VALUE = 42\n", "utf-8")
    (pkg / "data.json").write_text('{"fake": true}\n', "utf-8")
    # 字节码缓存必须被构建跳过（省体积、跨解释器无意义）
    (pkg / "__pycache__").mkdir(exist_ok=True)
    (pkg / "__pycache__" / "helper.cpython-311.pyc").write_bytes(b"\x00bytecode")
    dist_info = purelib / "fakepkg-0.0.1.dist-info"
    dist_info.mkdir(exist_ok=True)
    (dist_info / "METADATA").write_text("Name: fakepkg\nVersion: 0.0.1\n", "utf-8")
    return purelib


def fake_installer(venv_py: Path, requirements: Path) -> None:
    """假装 ``pip install -r <requirements>``：只往 site-packages 写假包，不联网。"""
    del requirements  # 真实 installer 会读它；这里只要有假依赖就够验证链路
    fake_dependency_files(venv_py)


def write_zip(path: Path, entries: dict[str, bytes], *, symlink: str | None = None) -> None:
    """造一个 zip；``symlink`` 给定时额外塞一个符号链接条目（模拟恶意包）。"""
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")


# --------------------------------------------------------------------- 组件表 / 锁文件
def test_targets_match_managed_components(build_module: ModuleType) -> None:
    """构建脚本的组件表必须与 ``app.components.COMPONENT_NAMES`` 完全一致。"""
    assert set(build_module.TARGETS) == set(COMPONENT_NAMES) == {"pageindex", "langextract"}


def test_limits_are_aligned_with_manager(build_module: ModuleType) -> None:
    """体积上限是构建期自检的基准，必须与 manager 的常量同值。"""
    from app.components import manager

    assert build_module.MAX_FILE_BYTES == manager.MAX_FILE_BYTES
    assert build_module.MAX_TOTAL_BYTES == manager.MAX_TOTAL_BYTES


@pytest.mark.parametrize("name", ["pageindex", "langextract"])
def test_requirements_are_pinned(build_module: ModuleType, name: str) -> None:
    path = REQUIREMENTS_DIR / build_module.TARGETS[name].requirements
    assert path.is_file(), f"缺依赖锁文件：{path}"
    lines = [line.strip() for line in path.read_text("utf-8").splitlines() if line.strip()]
    specs = [line for line in lines if not line.startswith("#")]
    target = build_module.TARGETS[name]
    assert any(line.startswith(target.name) and f"=={target.version}" in line for line in specs), specs
    # openai 3.x 改了 chat.completions 的响应形状，两个组件都锁在 1.x
    assert any(line.startswith("openai") and "<3" in line for line in specs), specs


# --------------------------------------------------------------------- 路径安全
@pytest.mark.parametrize(
    "raw",
    ["../evil.py", "a/../../evil.py", "/etc/passwd", "C:/Windows/system32/x.py", "\\\\server\\share\\x", "", "."],
)
def test_safe_entry_path_rejects_traversal(build_module: ModuleType, raw: str) -> None:
    """脚本侧的穿越校验必须拒绝，且与 manager 的 ``safe_relative`` 判断一致。"""
    with pytest.raises(build_module.BuildError):
        build_module.safe_entry_path(raw)
    with pytest.raises(UnsafeEntryError):
        safe_relative(raw)


def test_safe_entry_path_keeps_normal_paths(build_module: ModuleType) -> None:
    assert build_module.safe_entry_path("./runtime/site-packages") == "runtime/site-packages"
    assert build_module.safe_entry_path("run.py") == "run.py"
    assert build_module.safe_entry_path("a\\b\\c.py") == "a/b/c.py"


# --------------------------------------------------------------------- 打包 / sha256
def test_write_bundle_digest_and_entries(build_module: ModuleType, tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    (payload / "runtime" / "site-packages").mkdir(parents=True)
    (payload / "run.py").write_bytes(b"print('hi')\n")
    (payload / "runtime" / "site-packages" / "mod.py").write_bytes(b"X = 1\n")

    entries = build_module.collect_payload_files(payload)
    assert [rel for rel, _ in entries] == ["run.py", "runtime/site-packages/mod.py"]

    archive = build_module.write_bundle(payload, entries, tmp_path / "bundle.zip")
    bundle = Path(archive["path"])
    # sha256 就是文件内容的摘要（manager 拿它做完整性校验）
    assert archive["sha256"] == build_module.sha256_file(bundle)
    assert archive["entries"] == 2
    assert archive["uncompressedBytes"] == sum(path.stat().st_size for _, path in entries)
    assert bundle.stat().st_size == archive["bytes"]

    with zipfile.ZipFile(bundle) as zf:
        info = zf.getinfo("run.py")
        assert (info.external_attr >> 16) & 0o170000 == stat.S_IFREG, "归档条目必须是普通文件"
        assert zf.read("runtime/site-packages/mod.py") == b"X = 1\n"


def test_validate_bundle_requires_entry(build_module: ModuleType, tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "other.py").write_text("pass\n", "utf-8")
    archive = build_module.write_bundle(payload, build_module.collect_payload_files(payload), tmp_path / "b.zip")

    problems = build_module.validate_bundle(Path(archive["path"]), entry="run.py")
    assert any("entry 不在归档里" in item for item in problems), problems
    assert build_module.validate_bundle(Path(archive["path"]), entry="other.py") == []


def test_validate_bundle_rejects_traversal(build_module: ModuleType, tmp_path: Path) -> None:
    """恶意 zip（``../`` 与绝对路径）在脚本侧被拒，manager 侧也拒 —— 两端一致。"""
    evil = tmp_path / "evil.zip"
    write_zip(evil, {"../evil.py": b"boom", "run.py": b"print(1)\n"})

    problems = build_module.validate_bundle(evil, entry="run.py")
    assert any("不安全" in item for item in problems), problems

    with pytest.raises(UnsafeEntryError):
        extract_archive(evil, tmp_path / "extracted")
    assert not (tmp_path / "evil.py").exists(), "解压绝不能写到目标目录之外"


def test_validate_bundle_rejects_symlink_entry(build_module: ModuleType, tmp_path: Path) -> None:
    evil = tmp_path / "link.zip"
    write_zip(evil, {"run.py": b"print(1)\n"}, symlink="passwd")
    problems = build_module.validate_bundle(evil, entry="run.py")
    assert any("符号链接" in item for item in problems), problems


def test_collect_payload_files_skips_symlinks(build_module: ModuleType, tmp_path: Path) -> None:
    """符号链接条目会让 manager 整包拒绝，所以构建期就要把它们排除。"""
    if sys.platform == "win32":
        pytest.skip("Windows 上创建符号链接需要额外权限")
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "real.py").write_text("X = 1\n", "utf-8")
    (payload / "link.py").symlink_to(payload / "real.py")

    entries = build_module.collect_payload_files(payload)
    assert [rel for rel, _ in entries] == ["real.py"]


# --------------------------------------------------------------------- 体积上限
def test_build_rejects_oversized_payload(build_module: ModuleType, tmp_path: Path, monkeypatch: Any) -> None:
    """单文件超限要在构建期就炸掉，不能等安装时 manager 报 502。"""
    monkeypatch.setattr(build_module, "MAX_FILE_BYTES", 8)
    options = build_module.BuildOptions(name="pageindex", out=tmp_path / "out", no_venv=True)
    with pytest.raises(build_module.BuildError, match="上限"):
        build_module.build(options)


def test_build_rejects_oversized_total(build_module: ModuleType, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(build_module, "MAX_TOTAL_BYTES", 16)
    options = build_module.BuildOptions(name="pageindex", out=tmp_path / "out", no_venv=True)
    with pytest.raises(build_module.BuildError, match="上限"):
        build_module.build(options)


def test_build_rejects_unknown_component(build_module: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(build_module.BuildError, match="未知组件"):
        build_module.build(build_module.BuildOptions(name="core", out=tmp_path))


def test_build_rejects_bad_version(build_module: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(build_module.BuildError, match="version"):
        build_module.build(build_module.BuildOptions(name="pageindex", out=tmp_path, no_venv=True, version="../etc"))


# --------------------------------------------------------------------- 集成：假包 → 真安装
def test_build_with_fake_package(build_module: ModuleType, tmp_path: Path, venv_python: Path) -> None:
    """建真 venv + 假依赖 → 产出的组件源能被 manager 真装上。"""
    out = tmp_path / "components"
    report = build_module.build(
        build_module.BuildOptions(name="pageindex", out=out, python=str(venv_python)),
        installer=fake_installer,
    )

    out_dir = out / "pageindex"
    manifest_path = out_dir / "manifest.json"
    bundle_path = out_dir / "bundle.zip"
    assert manifest_path.is_file() and bundle_path.is_file()

    raw = json.loads(manifest_path.read_text("utf-8"))
    # 1) manifest 必须过 manager 的形状校验（构建端与安装端同一套契约）
    parsed = parse_manifest(raw, "pageindex")
    assert parsed.entry == "run.py"
    assert parsed.health_path == "/healthz"
    assert len(parsed.files) == 1
    assert parsed.files[0].sha256 == report["bundle"]["sha256"]

    # 2) 依赖树进了包，字节码缓存与构建工具没进包
    with zipfile.ZipFile(bundle_path) as zf:
        names = set(zf.namelist())
    assert "run.py" in names
    assert "runtime/site-packages/fakepkg/__init__.py" in names
    assert "runtime/site-packages/fakepkg/data.json" in names
    assert "runtime/build-info.json" in names
    assert "runtime/requirements.lock.txt" in names
    assert not [name for name in names if "__pycache__" in name], names
    assert not [name for name in names if "site-packages/pip/" in name], names

    # 3) manager 真装：bundle 被校验、解压、删除，entry 落位
    manager = ComponentManager(
        data_dir=tmp_path / "data",
        source=ComponentSource(kind="dir", location=str(out)),
        env={},
    )
    installed = manager.install("pageindex")
    version_dir = Path(installed["dir"])
    assert (version_dir / "run.py").is_file()
    assert (version_dir / "runtime" / "site-packages" / "fakepkg" / "__init__.py").is_file()
    assert not (version_dir / "bundle.zip").exists(), "装完不该留归档"
    assert manager.status("pageindex")["installed"] is True


def test_built_package_starts_and_answers_health(
    build_module: ModuleType, tmp_path: Path, venv_python: Path
) -> None:
    """打包产物被 manager 真启动：``/healthz`` 200，``/info`` 如实报告依赖导入失败。"""
    import urllib.request

    out = tmp_path / "components"
    build_module.build(
        build_module.BuildOptions(name="langextract", out=out, python=str(venv_python)),
        installer=fake_installer,
    )

    manager = ComponentManager(
        data_dir=tmp_path / "data",
        source=ComponentSource(kind="dir", location=str(out)),
        env={},
    )
    manager.install("langextract")
    try:
        started = manager.start("langextract")
        assert started["running"] is True
        assert started["health"]["checked"] is True
        assert started["health"]["ok"] is True

        with urllib.request.urlopen(f"{started['baseUrl']}/info", timeout=5) as response:
            info = json.loads(response.read().decode("utf-8"))
        assert info["component"] == "langextract"
        # 假包里没有真的 langextract 模块：启动器不能因此崩溃，只能如实报告
        assert info["import"]["ok"] is False
        assert "langextract" in info["import"]["error"]

        stopped = manager.stop("langextract")
        assert stopped["stopped"] is True
    finally:
        manager.stop_all()


# --------------------------------------------------------------------- 校验子命令 / CLI
def test_verify_detects_tampered_bundle(build_module: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "components"
    build_module.build(build_module.BuildOptions(name="pageindex", out=out, no_venv=True))
    package = out / "pageindex"

    assert build_module.verify_package(package, name="pageindex")["ok"] is True

    bundle = package / "bundle.zip"
    data = bytearray(bundle.read_bytes())
    data[-1] ^= 0xFF
    bundle.write_bytes(bytes(data))
    with pytest.raises(build_module.BuildError, match="校验不符"):
        build_module.verify_package(package, name="pageindex")


def test_verify_rejects_wrong_name(build_module: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "components"
    build_module.build(build_module.BuildOptions(name="pageindex", out=out, no_venv=True))
    with pytest.raises(build_module.BuildError, match="不一致"):
        build_module.verify_package(out / "pageindex", name="langextract")


def test_cli_builds_offline(build_module: ModuleType, tmp_path: Path) -> None:
    """``--no-venv`` 走完整 CLI（argparse → main → stdout JSON），不联网。"""
    import subprocess

    out = tmp_path / "components"
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--name", "langextract", "--out", str(out), "--no-venv"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["name"] == "langextract"
    assert summary["bundle"]["sha256"] == build_module.sha256_file(out / "langextract" / "bundle.zip")

    raw = json.loads((out / "langextract" / "manifest.json").read_text("utf-8"))
    parsed = parse_manifest(raw, "langextract")  # manager 认可
    assert parsed.version == build_module.TARGETS["langextract"].version


def test_cli_verify_subcommand(build_module: ModuleType, tmp_path: Path) -> None:
    import subprocess

    out = tmp_path / "components"
    built = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--name", "pageindex", "--out", str(out), "--no-venv"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr

    verified = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--name", "pageindex", "--out", str(out), "--verify"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["ok"] is True


def test_build_rejects_missing_requirements(build_module: ModuleType, tmp_path: Path) -> None:
    """锁文件不存在要在建 venv 之前就失败（别让人等完 venv 才报错）。"""
    options = build_module.BuildOptions(
        name="pageindex",
        out=tmp_path / "out",
        requirements=tmp_path / "nope.txt",
    )
    with pytest.raises(build_module.BuildError, match="依赖锁文件不存在"):
        build_module.build(options)


def test_cli_reports_real_error_for_missing_requirements(tmp_path: Path) -> None:
    """CLI 上缺锁文件是退出码 1 + 人话错误，不是 traceback。"""
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--name",
            "pageindex",
            "--out",
            str(tmp_path / "out"),
            "--requirements",
            str(tmp_path / "nope.txt"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "依赖锁文件不存在" in result.stderr
    assert "Traceback" not in result.stderr
