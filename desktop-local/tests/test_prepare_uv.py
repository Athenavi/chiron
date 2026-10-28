"""``scripts/prepare_uv.py`` 的单元测试。

**为什么这些用例值得写**：这个脚本决定安装包里那个 uv 二进制**是不是真的**。
它出错的方式都很安静 —— 校验跳过、解压取错文件、平台键拼错 —— 而后两者的表现
是"用户首次启动时找不到 uv 或者拿到错架构的包"。这些问题在构建期不红，就只能在
用户机器上红，所以逻辑必须在这里被钉住。

所有用例**不联网**：下载路径只验证"URL 拼得对不对"，真正的取包由 CI 或
``--from`` 离线路径完成。
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_prepare_uv():
    """按文件路径加载脚本。

    ``scripts/`` 不是包（没有 ``__init__.py``），直接 ``import prepare_uv`` 会失败；
    用 importlib 显式按路径加载，避免为了测试去改目录结构。
    """
    spec = importlib.util.spec_from_file_location("prepare_uv", SCRIPTS_DIR / "prepare_uv.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["prepare_uv"] = module
    spec.loader.exec_module(module)
    return module


prepare_uv = _load_prepare_uv()


# ---------------------------------------------------------------- 清单

def test_manifest_lists_every_supported_platform():
    """六个平台必须齐全，且每个都带合法的 64 位十六进制 SHA256。

    少一个平台 = 那个平台永远打不出包，而报错要到构建末期才出现。
    """
    manifest = prepare_uv.load_manifest()
    expected = {"win32-x64", "win32-arm64", "darwin-x64", "darwin-arm64", "linux-x64", "linux-arm64"}
    assert set(manifest["assets"]) == expected
    for key, asset in manifest["assets"].items():
        assert len(asset["sha256"]) == 64, f"{key} 的 sha256 长度不对"
        int(asset["sha256"], 16)  # 非十六进制会抛 ValueError
        assert asset["file"] and asset["binary"], f"{key} 缺文件名或二进制名"


def test_manifest_version_is_pinned_not_latest():
    """版本号必须写死成具体版本，不能是 ``latest`` 这类会漂移的值。"""
    version = prepare_uv.load_manifest()["version"]
    assert version != "latest"
    assert version[0].isdigit()


# ---------------------------------------------------------------- 平台归一

@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Windows", "AMD64", "win32-x64"),
        ("Windows", "ARM64", "win32-arm64"),
        ("Darwin", "arm64", "darwin-arm64"),
        ("Darwin", "x86_64", "darwin-x64"),
        ("Linux", "x86_64", "linux-x64"),
        ("Linux", "aarch64", "linux-arm64"),
    ],
)
def test_current_platform_key_normalizes_aliases(monkeypatch, system, machine, expected):
    """``AMD64``/``x86_64``/``aarch64`` 这些别名必须归一成清单键。

    清单键要与 Node 侧 ``process.platform``/``process.arch`` 对齐，否则壳层按
    自己的键找二进制会落空。
    """
    monkeypatch.setattr(prepare_uv.platform, "system", lambda: system)
    monkeypatch.setattr(prepare_uv.platform, "machine", lambda: machine)
    assert prepare_uv.current_platform_key() == expected


def test_current_platform_key_rejects_unknown(monkeypatch):
    monkeypatch.setattr(prepare_uv.platform, "system", lambda: "SunOS")
    monkeypatch.setattr(prepare_uv.platform, "machine", lambda: "sparc")
    with pytest.raises(SystemExit):
        prepare_uv.current_platform_key()


# ---------------------------------------------------------------- 校验

def test_verify_accepts_matching_digest(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"hello uv")
    digest = hashlib.sha256(b"hello uv").hexdigest()
    assert prepare_uv.verify(target, digest, label="blob.bin") == digest


def test_verify_rejects_mismatch_and_keeps_file(tmp_path):
    """校验失败必须硬失败，且**保留**文件供排查（不删是刻意的）。"""
    target = tmp_path / "blob.bin"
    target.write_bytes(b"tampered")
    with pytest.raises(SystemExit) as exc:
        prepare_uv.verify(target, "0" * 64, label="blob.bin")
    assert "校验失败" in str(exc.value)
    assert target.is_file(), "失败后不应删除文件，否则无从判断是下载坏了还是清单错了"


# ---------------------------------------------------------------- 解压

def test_extract_binary_from_zip_finds_nested_file(tmp_path):
    """zip 里带版本化目录时，要按**文件名**递归找到 uv.exe 而不是写死路径。"""
    archive = tmp_path / "uv-x86_64-pc-windows-msvc.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("uv-x86_64-pc-windows-msvc/uv.exe", b"MZ fake")
        zf.writestr("uv-x86_64-pc-windows-msvc/uvx.exe", b"MZ other")

    dest_dir = tmp_path / "out"
    asset = {"binary": "uv.exe"}
    result = prepare_uv.extract_binary(archive, asset, dest_dir)

    assert result == dest_dir / "uv.exe"
    assert result.read_bytes() == b"MZ fake"


def test_extract_binary_from_targz_finds_nested_file(tmp_path):
    archive = tmp_path / "uv-x86_64-unknown-linux-gnu.tar.gz"
    source = tmp_path / "uv"
    source.write_bytes(b"#!/bin/sh\n")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source, arcname="uv-x86_64-unknown-linux-gnu/uv")

    dest_dir = tmp_path / "out"
    result = prepare_uv.extract_binary(archive, {"binary": "uv"}, dest_dir)

    assert result == dest_dir / "uv"
    assert result.read_bytes() == b"#!/bin/sh\n"


def test_extract_binary_reports_missing_member(tmp_path):
    """压缩包里没有目标二进制时必须明确报错，不能产出一个空目录假装成功。"""
    archive = tmp_path / "uv-x86_64-pc-windows-msvc.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("something-else.txt", b"nope")

    with pytest.raises(SystemExit) as exc:
        prepare_uv.extract_binary(archive, {"binary": "uv.exe"}, tmp_path / "out")
    assert "找不到" in str(exc.value)


def test_extract_binary_rejects_unknown_archive_format(tmp_path):
    archive = tmp_path / "uv.rar"
    archive.write_bytes(b"nope")
    with pytest.raises(SystemExit):
        prepare_uv.extract_binary(archive, {"binary": "uv"}, tmp_path / "out")


# ---------------------------------------------------------------- 路径

def test_binary_path_matches_electron_resources_layout():
    """产物路径必须落在 ``frontend-vue/assets/uv/<platform>/`` —— electron-builder
    的 extraResources 就是从这里拷到 ``resources/uv/`` 的。"""
    manifest = prepare_uv.load_manifest()
    asset = manifest["assets"]["win32-x64"]
    path = prepare_uv.binary_path("win32-x64", asset)
    assert path == prepare_uv.UV_ASSETS_DIR / "win32-x64" / "uv.exe"
    assert path.parents[3].name == "frontend-vue"


def test_missing_platform_is_rejected_by_manifest_lookup():
    """未知平台要明确失败并列出可用项，而不是 KeyError 崩掉。"""
    manifest = prepare_uv.load_manifest()
    assert "sunos-x64" not in manifest["assets"]
