#!/usr/bin/env python
"""把 ``uv`` 二进制定位/下载到 ``frontend-vue/assets/uv/<platform>/``，供日后打进安装包。

**为什么需要 uv**：本地模式不再随包携带 Python 运行时（那会把"构建机装了什么"硬编码进
安装包，且每次升级依赖都要重新发版）。改由 uv 在**用户首次启动时**自建 venv 并装依赖，
好处是：包体小、依赖可独立修、missing 依赖能就地重建。详见 ``docs/desktop-local/packaging.md``。

**为什么在构建期下载而不是运行时下载**：uv 是**跑在用户机上**的可执行文件，必须先躺在
安装包里，用户首次启动才有工具可用。只在构建期取一次，运行期完全不联网下载 uv 本体 ——
这样"哪来的、哪个版本、校验值多少"都在仓库里可审计。

**为什么必须强校验 SHA256**：``vendor/规划.md`` §8 要求「版本号 / SHA256 / 下载地址取不到
就写未固化，不自动下载工具」。本脚本据此把三者固化在 ``uv_manifest.json``：版本号写死、
每个平台一个 SHA256、地址由 base_url 拼出。校验不符时**直接失败**（保留坏文件供排查），
绝不"先装上再说"。

用法::

    # 为当前平台下载/就位并自检（CI 与本地打包都走这条）
    python desktop-local/scripts/prepare_uv.py

    # 指定平台（交叉打包时用）
    python desktop-local/scripts/prepare_uv.py --platform darwin-arm64

    # 只用已经下载好的压缩包（离线环境 / 内网镜像），仍然强校验
    python desktop-local/scripts/prepare_uv.py --from D:/downloads/uv-x86_64-pc-windows-msvc.zip

    # 只自检，不下载（CI 里验证"当前产物这一档是否可用"）
    python desktop-local/scripts/prepare_uv.py --check

    # 删掉已就位的二进制
    python desktop-local/scripts/prepare_uv.py --clean

自检内容：目标二进制存在、``uv --version`` 能跑出与清单一致的版本号。
任何一条不过都以非零退出 —— 打出一个跑不起来的 uv，用户首次启动必然失败。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DESKTOP_LOCAL = Path(__file__).resolve().parents[1]
REPO_ROOT = DESKTOP_LOCAL.parent
FRONTEND_VUE = REPO_ROOT / "frontend-vue"
# 产物位置与 electron-builder.yml 的 extraResources 对应（assets/uv → resources/uv）
UV_ASSETS_DIR = FRONTEND_VUE / "assets" / "uv"
MANIFEST_PATH = Path(__file__).resolve().parent / "uv_manifest.json"

# 下载超时（秒）。uv 各平台产物约 15–20MB，慢网络留足余量；
# 超时要显式报错，不能静默继续（否则后半程会以"文件不存在"这种误导性理由失败）。
DOWNLOAD_TIMEOUT = 180


def load_manifest() -> dict:
    """读固化清单。缺字段就立刻失败 —— 这是"取不到就未固化"的执行点。"""
    if not MANIFEST_PATH.is_file():
        raise SystemExit(f"缺少 uv 清单：{MANIFEST_PATH}")
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"uv 清单不是合法 JSON：{MANIFEST_PATH}：{exc}") from exc
    for key in ("version", "base_url", "assets"):
        if not data.get(key):
            raise SystemExit(f"uv 清单缺少 {key}：{MANIFEST_PATH}")
    return data


def current_platform_key() -> str:
    """把 ``platform.system()/machine()`` 归一成清单里的 ``<platform>-<arch>``。

    **为什么要归一**：不同 Python 发行版报的名字不一致（Windows 报 ``AMD64``、
    Linux 报 ``x86_64``、macOS 报 ``arm64``），而清单键用的是 Node 侧
    ``process.platform`` / ``process.arch`` 的写法（壳层按它找二进制）。两边必须对上，
    否则打包时找不到清单项、运行时装错架构的二进制。
    """
    system = platform.system().lower()
    if system == "windows":
        os_key = "win32"
    elif system == "darwin":
        os_key = "darwin"
    elif system == "linux":
        os_key = "linux"
    else:
        raise SystemExit(f"不支持的平台：{platform.system()}")

    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        raise SystemExit(f"不支持的 CPU 架构：{platform.machine()}")
    return f"{os_key}-{arch}"


def target_dir(platform_key: str) -> Path:
    return UV_ASSETS_DIR / platform_key


def binary_path(platform_key: str, asset: dict) -> Path:
    return target_dir(platform_key) / str(asset["binary"])


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, expected: str, *, label: str) -> str:
    """强校验 SH256。不符时**保留**文件并报出两个值，方便判断是下载坏了还是清单写错了。"""
    actual = sha256_of(path)
    if actual != expected:
        raise SystemExit(
            f"{label} 校验失败：\n"
            f"  期望 {expected}\n"
            f"  实际 {actual}\n"
            f"  文件保留在 {path}（可能是下载不完整，也可能是清单里的值过期了）"
        )
    return actual


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  · 下载 {url}")
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
            with dest.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except Exception as exc:  # noqa: BLE001 - 网络错误类型太杂，统一转成可读失败
        # 不能静默跳过：这条路径失败意味着本次打包没有可用的 uv
        raise SystemExit(f"下载失败：{url}\n  {type(exc).__name__}: {exc}") from exc


def extract_binary(archive: Path, asset: dict, dest_dir: Path) -> Path:
    """从 zip / wheel / tar.gz 里取出 uv 本体。

    发行包的压缩包里带版本化目录（如 ``uv-x86_64-pc-windows-msvc/uv.exe``，
    PyPI 的 wheel 里是 ``uv-<ver>.data/scripts/uv[.exe]``），所以按**文件名**递归
    查找而不是固定路径 —— 目录名随版本变，写死会在升级时静默解不出文件。
    """
    want = str(asset["binary"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / want

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # .whl 就是一个 zip：PyPI 的 wheel 走这条
        if archive.suffix in (".zip", ".whl"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp_path)
        elif archive.name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive, "r:gz") as tf:
                # Python 3.12+ 的 filter 参数；旧版会因关键字不存在而报错，故做兼容
                try:
                    tf.extractall(tmp_path, filter="data")
                except TypeError:  # pragma: no cover - 仅旧解释器
                    tf.extractall(tmp_path)
        else:
            raise SystemExit(f"不认识的压缩格式：{archive.name}")

        found = next((p for p in tmp_path.rglob(want) if p.is_file()), None)
        if found is None:
            raise SystemExit(f"{archive.name} 里找不到 {want}")
        shutil.copy2(found, dest)

    if os.name != "nt":
        # 解压后的权限位不可靠（zip 尤其如此），显式补可执行位
        dest.chmod(0o755)
    return dest


def self_check(platform_key: str, asset: dict, version: str, *, verbose: bool = True) -> int:
    """真实执行一次 ``uv --version`` 并比对版本号。

    只检查文件存在是不够的：拿错架构的二进制在 Windows 上会以
    ``%1 is not a valid Win32 application`` 失败，在 Linux 上是
    ``cannot execute binary file``。这两类都必须在这里红掉，而不是等到用户首次启动。
    """
    exe = binary_path(platform_key, asset)
    if not exe.is_file():
        print(f"FAIL 缺少 {exe}", file=sys.stderr)
        return 1

    # 交叉打包场景（例如在 Linux 上为 Windows 产出）根本执行不了，只能做存在性检查
    is_cross = platform_key != current_platform_key()
    if is_cross:
        print(f"OK   {exe}（跨平台产物，跳过执行校验）")
        return 0

    try:
        proc = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            # 干净环境：不继承 PYTHON*/UV_* 之类，避免构建机配置影响判定
            env={"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL {exe} 无法执行：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        print(f"FAIL {exe} --version 退出码 {proc.returncode}：{output.strip()}", file=sys.stderr)
        return 1
    if version not in output:
        print(f"FAIL 版本不符：期望包含 {version}，实际 {output.strip()}", file=sys.stderr)
        return 1

    if verbose:
        print(f"OK   {exe} → {output.strip()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="就位 uv 二进制（构建期）")
    parser.add_argument("--platform", help="目标平台键，如 win32-x64 / darwin-arm64")
    parser.add_argument("--from", dest="archive", help="已下载的 uv 压缩包（离线时用）")
    parser.add_argument("--check", action="store_true", help="只自检，不下载")
    parser.add_argument("--clean", action="store_true", help="删除已就位的二进制")
    args = parser.parse_args(argv)

    manifest = load_manifest()
    version = str(manifest["version"])
    platform_key = args.platform or current_platform_key()

    assets = manifest["assets"]
    if platform_key not in assets:
        # 明确列出可用项：漏一个平台的代价是那个平台永远打不出包，必须让人一眼看到
        raise SystemExit(
            f"清单里没有平台 {platform_key}；可用：{', '.join(sorted(assets))}"
        )
    asset = assets[platform_key]

    if args.clean:
        directory = target_dir(platform_key)
        if directory.exists():
            shutil.rmtree(directory)
            print(f"已删除 {directory}")
        else:
            print(f"无需删除（{directory} 不存在）")
        return 0

    if args.check:
        return self_check(platform_key, asset, version)

    dest_dir = target_dir(platform_key)
    dest = binary_path(platform_key, asset)

    if args.archive:
        # 离线路径：仍然强校验，只是来源换成本地文件
        source = Path(args.archive)
        if not source.is_file():
            raise SystemExit(f"--from 不是文件：{source}")
        verify(source, str(asset["sha256"]), label=source.name)
        extract_binary(source, asset, dest_dir)
        print(f"  · 已从本地压缩包取出 {dest.name}")
    else:
        # 清单里给了完整 url 就用它（PyPI 的 wheel 路径带哈希目录，无法由 base_url 拼出）；
        # 否则按 base_url/<version>/<file> 拼（GitHub Release 的布局）。
        url = str(asset.get("url") or "").strip()
        if not url:
            url = f"{str(manifest['base_url']).rstrip('/')}/{version}/{asset['file']}"
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / str(asset["file"])
            download(url, archive)
            verify(archive, str(asset["sha256"]), label=str(asset["file"]))
            extract_binary(archive, asset, dest_dir)
        print(f"  · 已校验并解出 {dest}")

    result = self_check(platform_key, asset, version)
    if result != 0:
        return result

    print(f"\nuv {version} 已就位于 {dest_dir}")
    print("   打包时由 electron-builder 的 extraResources 拷到 resources/uv/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
