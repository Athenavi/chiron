#!/usr/bin/env python
"""构建 PageIndex / LangExtract 的**真实**组件包（组件源目录布局）。

产物就是 ``app/components/manager.py`` 能直接吃的那种东西：

    <out>/<name>/manifest.json          # 契约见 docs/desktop-local/components.md §3
    <out>/<name>/bundle.zip             # 解压后 = 组件版本目录的内容

``bundle.zip`` 里的布局::

    run.py                              # entry：启动器，起 /healthz + /info
    runtime/site-packages/**            # 隔离 venv 装出来的整棵依赖树
    runtime/requirements.lock.txt       # pip freeze 快照（复现用）
    runtime/build-info.json             # 解释器 / 平台 / 依赖 / 体积诊断

为什么依赖走 zip 而不是逐个文件列进 ``manifest.files``：manager 限制
``files`` 最多 256 项，而一个真实依赖树有几千个文件；``files`` 里出现 ``.zip``
时 manager 会先按文件级 sha256 校验、再解压、最后删掉归档（见 manager
``_extract_zip`` / ``install``），所以「一个归档 + 归档 sha256」是唯一可行形态。

为什么还要 ``run.py`` 这一层：manager 用 ``CHIRON_PYTHON`` 或 core 自己的
``sys.executable`` 启动 ``.py`` entry，**不会**用 venv 里的解释器，也不会把
venv 的 site-packages 加进 ``sys.path``。所以构建产物不能依赖
``<venv>/Scripts/python.exe``（它里面的 ``pyvenv.cfg`` 指向构建机的 base
解释器，搬台机器就废），只能带**纯 site-packages**，由 ``run.py`` 在启动时
``sys.path.insert`` 进去。

用法::

    cd desktop-local

    # 真实构建（联网 pip install，产出 dist/components/<name>/）
    python scripts/build_component.py --name pageindex --out dist/components
    python scripts/build_component.py --name langextract --out dist/components

    # 换解释器 / 换锁文件 / 带上离线依赖目录
    python scripts/build_component.py --name pageindex --out dist/components \\
        --python C:/Python312/python.exe --requirements lock.txt

    # 离线冒烟（不建 venv、不装依赖，只验证打包链路）
    python scripts/build_component.py --name pageindex --out /tmp/out --no-venv

    # 校验已产出的包（不构建、不联网）
    python scripts/build_component.py --name pageindex --out dist/components --verify

    # 装到本地 core（把 out 当组件源）
    CHIRON_COMPONENT_BASE_URL=dist/components python -m app.main

进度写 stderr，最终摘要以 JSON 写 stdout（便于脚本消费）。退出码 0 = 成功。

脚本只用标准库（venv / subprocess / zipfile / hashlib），**不 import app**：
它要能在没有本地 core 依赖的机器 / CI 上单独跑。
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

# --------------------------------------------------------------------- 路径 / 常量
SCRIPT_DIR = Path(__file__).resolve().parent
DESKTOP_LOCAL = SCRIPT_DIR.parent
REQUIREMENTS_DIR = SCRIPT_DIR / "components"
DEFAULT_OUT = DESKTOP_LOCAL / "dist" / "components"

MANIFEST_NAME = "manifest.json"
BUNDLE_NAME = "bundle.zip"
LAUNCHER_NAME = "run.py"
RUNTIME_DIR = "runtime"
SITE_PACKAGES_DIR = "site-packages"
LOCK_NAME = "requirements.lock.txt"
BUILD_INFO_NAME = "build-info.json"

#: 与 ``app/components/manager.py`` 的同名常量保持一致（构建期先自检，别等安装时才炸）
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024

#: venv 里自带的构建工具，不属于运行时依赖，打进去纯增体积
SKIP_TOP_LEVEL_DIRS = ("pip", "setuptools", "wheel", "pkg_resources", "_distutils_hack")

VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
DRIVE_RE = re.compile(r"^[A-Za-z]:")


class BuildError(RuntimeError):
    """构建失败（消息直接打给人看）。"""


# --------------------------------------------------------------------- 组件表
@dataclass(frozen=True)
class ComponentTarget:
    """一个可构建组件的静态信息（manifest 的其余字段由构建期填充）。"""

    name: str
    #: 组件包自身版本，直接当 ``manifest.version`` 与安装目录名
    version: str
    #: ``run.py`` 里 import 的顶层模块名（``--self-check`` 与 /info 用）
    import_name: str
    #: 默认依赖锁文件名（相对 ``scripts/components/``）
    requirements: str
    #: 启动参数（写进 manifest.args，manager 原样追加到命令行）
    args: tuple[str, ...] = ()
    health_path: str = "/healthz"
    #: 冷导入可能要十几秒（包里没预编译 .pyc 时），留足余量；/healthz 本身不会等导入
    health_timeout_ms: int = 30000
    description: str = ""


TARGETS: dict[str, ComponentTarget] = {
    "pageindex": ComponentTarget(
        name="pageindex",
        version="0.4.4",
        import_name="pageindex",
        requirements="pageindex.requirements.txt",
        description="PageIndex 0.4.4（树形章节索引）运行时包",
    ),
    "langextract": ComponentTarget(
        name="langextract",
        version="1.7.1",
        import_name="langextract",
        requirements="langextract.requirements.txt",
        description="LangExtract 1.7.1（结构化字段抽取）运行时包",
    ),
}


def target_for(name: str) -> ComponentTarget:
    target = TARGETS.get(str(name or "").strip())
    if target is None:
        raise BuildError(f"未知组件：{name}（可选：{' / '.join(sorted(TARGETS))}）")
    return target


def _log(message: str) -> None:
    print(f"[build_component] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------- 路径安全
def safe_entry_path(raw: str, what: str = "条目") -> str:
    """把归档条目 / manifest 路径规范成安全相对 POSIX 路径。

    与 ``manager.safe_relative`` 同一套规则（拒绝空、绝对路径、盘符、``..``），
    这样构建期能拦下的东西，安装期一定也拦得住（测试里两边都比对过）。
    """
    text = str(raw or "").strip()
    if not text:
        raise BuildError(f"{what}为空")
    normalized = text.replace("\\", "/")
    if normalized.startswith("/"):
        raise BuildError(f"{what}是绝对路径：{text}")
    if DRIVE_RE.match(normalized):
        raise BuildError(f"{what}带盘符：{text}")
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise BuildError(f"{what}含上跳（..）：{text}")
        parts.append(part)
    if not parts:
        raise BuildError(f"{what}规范化后为空：{text}")
    return "/".join(parts)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            candidate = Path(root) / name
            if candidate.is_symlink():
                continue
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
    return total


# --------------------------------------------------------------------- 子进程
def run(cmd: Sequence[str], *, what: str, cwd: Path | None = None) -> str:
    """跑一条命令；失败时把 stderr 尾部塞进异常（真实错误，不吞）。"""
    _log(f"{what}：{' '.join(cmd)}")
    completed = subprocess.run(  # noqa: S603 —— 命令由本脚本拼好，不含外部输入
        list(cmd),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-15:]
        detail = "\n".join(tail) or "（无输出）"
        raise BuildError(f"{what} 失败（退出码 {completed.returncode}）：\n{detail}")
    return completed.stdout or ""


# --------------------------------------------------------------------- venv
def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        candidates = (venv_dir / "Scripts" / "python.exe", venv_dir / "bin" / "python.exe")
    else:
        candidates = (venv_dir / "bin" / "python", venv_dir / "bin" / "python3")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BuildError(f"venv 里找不到解释器：{venv_dir}")


def create_venv(python_exe: str, venv_dir: Path) -> Path:
    run([python_exe, "-m", "venv", str(venv_dir)], what="创建隔离 venv")
    return venv_python(venv_dir)


def install_requirements(
    venv_py: Path,
    requirements: Path,
    *,
    find_links: Sequence[str | Path] = (),
    offline: bool = False,
) -> None:
    """在 venv 里 ``pip install -r``。

    ``offline`` 配 ``find_links`` 就是"只用本地 wheel 目录装"：CI 先把 wheel 缓存下来，
    构建时不再对 PyPI 发一个请求（也用于本机限时环境）。
    """
    cmd = [
        str(venv_py),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        # 产物里本来就会跳过 __pycache__（见 copy_site_packages），装的时候编译
        # 几千个 .pyc 纯属浪费：在慢盘 / 杀毒扫描的环境里能省掉一半安装时间。
        "--no-compile",
    ]
    if offline:
        cmd.append("--no-index")
    for location in find_links:
        cmd.extend(["--find-links", str(location)])
    cmd.extend(["-r", str(requirements)])
    run(cmd, what=f"pip install -r {requirements.name}")


def venv_site_packages(venv_py: Path) -> list[Path]:
    """问 venv 要 purelib / platlib（Windows 上两者相同，去重后可能只剩一个）。"""
    code = (
        "import json, sysconfig;"
        "p = sysconfig.get_paths();"
        "print(json.dumps(sorted({p['purelib'], p['platlib']})))"
    )
    output = run([str(venv_py), "-c", code], what="定位 venv site-packages").strip().splitlines()
    if not output:
        raise BuildError("定位 venv site-packages 失败（无输出）")
    paths = [Path(item) for item in json.loads(output[-1])]
    return [path for path in paths if path.is_dir()]


def pip_freeze(venv_py: Path) -> str:
    try:
        return run([str(venv_py), "-m", "pip", "freeze", "--all"], what="记录 pip freeze")
    except BuildError as exc:  # freeze 失败不该毁掉整个构建
        _log(f"警告：pip freeze 失败，锁定快照将为空（{exc}）")
        return ""


# --------------------------------------------------------------------- 拷贝依赖树
def copy_site_packages(
    sources: Iterable[Path],
    dest: Path,
    *,
    skip_top_level: Sequence[str] = SKIP_TOP_LEVEL_DIRS,
) -> dict[str, Any]:
    """把若干 site-packages 合并拷进 ``dest``。

    - 跳过 ``__pycache__``（可再生成，省体积）；
    - 跳过符号链接（manager 解压时见到 symlink 条目会整包拒绝，所以包里不能有）；
    - 跳过 venv 自带的 pip / setuptools / wheel 一类构建工具；
    - 顺序合并，同名文件后者覆盖（日志里记一条）。
    """
    files = 0
    total = 0
    skipped: list[str] = []
    overridden: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)

    for source in sources:
        if not source.is_dir():
            raise BuildError(f"site-packages 目录不存在：{source}")
        for root, dirnames, filenames in os.walk(source, followlinks=False):
            root_path = Path(root)
            at_top = root_path == source
            kept_dirs: list[str] = []
            for name in sorted(dirnames):
                child = root_path / name
                if at_top and name in skip_top_level:
                    skipped.append(f"{child}（构建工具，跳过）")
                    continue
                if name == "__pycache__":
                    skipped.append(f"{child}（字节码缓存，跳过）")
                    continue
                if child.is_symlink():
                    skipped.append(f"{child}（符号链接，跳过）")
                    continue
                kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in sorted(filenames):
                child = root_path / name
                if child.is_symlink():
                    skipped.append(f"{child}（符号链接，跳过）")
                    continue
                rel = child.relative_to(source)
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    overridden.append(str(rel))
                shutil.copyfile(child, target)
                files += 1
                try:
                    total += target.stat().st_size
                except OSError:
                    continue

    return {
        "files": files,
        "bytes": total,
        "skipped": skipped,
        "overridden": overridden,
        "sources": [str(source) for source in sources],
    }


# --------------------------------------------------------------------- 启动器
_LAUNCHER_TEMPLATE = Template(
    '''#!/usr/bin/env python
"""$name 组件启动器（由 desktop-local/scripts/build_component.py 生成，勿手改）。

它做三件事，只做三件事：

1. 把随包携带的 ``runtime/site-packages`` 加到 ``sys.path`` 最前面 —— 本地 core
   用 base 解释器启动 ``.py`` entry，不会替我们激活 venv；
2. 在 ``CHIRON_COMPONENT_HOST`` / ``CHIRON_COMPONENT_PORT``（core 借好的端口）上
   起一个最小 HTTP 服务；
3. ``/healthz`` 回 200（进程活着、端口收下了，core 的健康检查看这个），
   ``/info`` 回依赖导入的真实结果（导入失败也如实报，不假装成功）。

真实业务接口（索引 / 抽取）不在本批次范围内：这里只有健康与自检端点。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = "$name"
IMPORT_NAME = "$import_name"
VERSION = "$version"

HERE = Path(__file__).resolve().parent
SITE_PACKAGES = HERE / "runtime" / "site-packages"

_PROBE: dict[str, object] | None = None
_PROBE_LOCK = threading.Lock()


def bootstrap() -> None:
    """把随包携带的依赖树放到 sys.path 最前面（幂等）。"""
    if not SITE_PACKAGES.is_dir():
        return
    path = str(SITE_PACKAGES)
    if path not in sys.path:
        sys.path.insert(0, path)


def probe() -> dict[str, object]:
    """导入一次真实组件包并缓存结果。

    组件包的冷导入可能要十几秒（langextract 这种，尤其包里没预编译 .pyc 时），
    所以这个函数只该被 /info 与 --self-check 同步调用；/healthz 走 import_state()。
    """
    global _PROBE
    with _PROBE_LOCK:
        if _PROBE is not None:
            return _PROBE
        result: dict[str, object] = {"ok": False, "module": IMPORT_NAME, "error": "", "file": "", "version": ""}
        try:
            module = importlib.import_module(IMPORT_NAME)
            result["ok"] = True
            result["file"] = str(getattr(module, "__file__", "") or "")
            result["version"] = str(getattr(module, "__version__", "") or "")
        except BaseException as exc:  # 组件导入炸了也要能报出来，不能把进程带走
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["traceback"] = traceback.format_exc()[-4000:]
        _PROBE = result
        return _PROBE


def import_state() -> dict[str, object]:
    """非阻塞视图：探测没跑完就说 pending，绝不让一次冷导入拖住健康检查。"""
    current = _PROBE
    if current is None:
        return {"ok": None, "pending": True, "module": IMPORT_NAME}
    return current


def warm_up() -> None:
    """后台把导入探测跑掉（daemon 线程，失败也不影响服务）。"""
    try:
        probe()
    except BaseException:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "$name/" + VERSION

    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
        path = self.path.split("?", 1)[0]
        if path in ("/healthz", "/health"):
            self.respond(200, {
                "status": "ok",
                "component": NAME,
                "version": VERSION,
                "pid": os.getpid(),
                "import": import_state(),
            })
            return
        if path == "/info":
            self.respond(200, {
                "component": NAME,
                "version": VERSION,
                "python": sys.version,
                "executable": sys.executable,
                "sitePackages": str(SITE_PACKAGES),
                "import": probe(),
            })
            return
        self.respond(404, {"error": "not_found", "path": path})

    def respond(self, status: int, payload: dict[str, object]) -> None:
        body = (json.dumps(payload, ensure_ascii=False) + "\\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s %s\\n" % (NAME, fmt % args))
        sys.stderr.flush()


def main(argv: list[str]) -> int:
    bootstrap()
    if "--self-check" in argv:
        report = {
            "component": NAME,
            "version": VERSION,
            "python": sys.version,
            "sitePackages": str(SITE_PACKAGES),
            "sitePackagesExists": SITE_PACKAGES.is_dir(),
            "import": probe(),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["import"]["ok"] else 1

    host = os.environ.get("CHIRON_COMPONENT_HOST") or "127.0.0.1"
    raw_port = os.environ.get("CHIRON_COMPONENT_PORT") or "0"
    try:
        port = int(raw_port)
    except ValueError:
        sys.stderr.write("%s CHIRON_COMPONENT_PORT 不是整数：%r\\n" % (NAME, raw_port))
        return 2
    threading.Thread(target=warm_up, name="import-probe", daemon=True).start()
    server = ThreadingHTTPServer((host, port), Handler)
    print(json.dumps({
        "event": "listening",
        "component": NAME,
        "version": VERSION,
        "host": host,
        "port": server.server_port,
    }), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''
)


def write_launcher(path: Path, target: ComponentTarget) -> None:
    path.write_text(
        _LAUNCHER_TEMPLATE.substitute(
            name=target.name,
            import_name=target.import_name,
            version=target.version,
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# --------------------------------------------------------------------- 打包
def collect_payload_files(payload_root: Path) -> list[tuple[str, Path]]:
    """列出 payload 下所有待打包文件（``(相对 POSIX 路径, 绝对路径)``）。

    跳过符号链接（manager 见到 symlink 条目会整包拒绝），并对每个相对路径做一次
    穿越校验 —— 写包前就把不安全的条目拦掉，别指望解压端兜着。
    """
    entries: list[tuple[str, Path]] = []
    for root, dirnames, filenames in os.walk(payload_root, followlinks=False):
        root_path = Path(root)
        keep: list[str] = []
        for name in sorted(dirnames):
            child = root_path / name
            if child.is_symlink():
                _log(f"跳过符号链接目录：{child}")
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            child = root_path / name
            if child.is_symlink():
                _log(f"跳过符号链接文件：{child}")
                continue
            rel = safe_entry_path(child.relative_to(payload_root).as_posix(), "payload 文件")
            entries.append((rel, child))
    entries.sort(key=lambda item: item[0])
    return entries


def check_payload_limits(entries: Sequence[tuple[str, Path]]) -> dict[str, Any]:
    """构建期就对齐 manager 的体积上限（单文件 256 MiB / 解压总量 1 GiB）。"""
    total = 0
    oversized: list[str] = []
    for rel, path in entries:
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES:
            oversized.append(f"{rel}（{size} 字节 > 上限 {MAX_FILE_BYTES}）")
    if oversized:
        raise BuildError("以下文件超过 manager 的单文件上限，装不进去：" + "；".join(oversized))
    if total > MAX_TOTAL_BYTES:
        raise BuildError(
            f"payload 解压总量 {total} 字节超过 manager 的上限 {MAX_TOTAL_BYTES} 字节；"
            "请裁剪依赖（例如去掉开发期依赖）或分组件拆包"
        )
    return {"files": len(entries), "uncompressedBytes": total}


def write_bundle(
    payload_root: Path,
    entries: Sequence[tuple[str, Path]],
    out_zip: Path,
    *,
    compresslevel: int = 6,
) -> dict[str, Any]:
    """把 payload 打成一个 zip（条目名 = 相对 POSIX 路径，时间戳固定便于复现）。"""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=compresslevel) as archive:
        for rel, path in entries:
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # 显式写普通文件位，避免把构建机的权限/时间带进去（也避免误标成 symlink）
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            with path.open("rb") as source, archive.open(info, "w") as sink:
                shutil.copyfileobj(source, sink, 1024 * 1024)

    with zipfile.ZipFile(out_zip) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        compressed = sum(info.compress_size for info in infos)
        uncompressed = sum(info.file_size for info in infos)
    size = out_zip.stat().st_size
    return {
        "path": str(out_zip),
        "bytes": size,
        "sha256": sha256_file(out_zip),
        "entries": len(infos),
        "compressedBytes": compressed,
        "uncompressedBytes": uncompressed,
    }


def validate_bundle(bundle: Path, *, entry: str) -> list[str]:
    """按 manager 的解压规则自检一个归档；返回问题列表（空 = 通过）。

    不抛异常：调用方（``build`` / ``verify_package``）拿到列表后统一报错，
    测试也直接断言列表内容。
    """
    problems: list[str] = []
    try:
        archive = zipfile.ZipFile(bundle)
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"归档打不开：{bundle}（{exc}）"]

    with archive:
        names: set[str] = set()
        total = 0
        for info in archive.infolist():
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                problems.append(f"归档含符号链接（manager 会整包拒绝）：{info.filename}")
            try:
                rel = safe_entry_path(info.filename, "归档条目")
            except BuildError as exc:
                problems.append(f"归档含不安全条目：{exc}")
                continue
            names.add(rel)
            if info.file_size > MAX_FILE_BYTES:
                problems.append(f"条目超过单文件上限：{rel}（{info.file_size} 字节）")
            total += info.file_size
        if total > MAX_TOTAL_BYTES:
            problems.append(f"解压总量 {total} 字节超过上限 {MAX_TOTAL_BYTES} 字节")
        try:
            entry_rel = safe_entry_path(entry, "manifest.entry")
        except BuildError as exc:
            problems.append(f"entry 不合法：{exc}")
        else:
            if entry_rel not in names:
                problems.append(f"entry 不在归档里：{entry_rel}")
    return problems


# --------------------------------------------------------------------- manifest
def build_manifest(target: ComponentTarget, version: str, archive: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": target.name,
        "version": version,
        "entry": LAUNCHER_NAME,
        "args": list(target.args),
        # 单个归档 + 顶层 sha256（manager 只允许这种情况用字符串摘要）
        "files": [{"path": BUNDLE_NAME, "sha256": archive["sha256"]}],
        "sha256": archive["sha256"],
        "health": {"path": target.health_path, "timeoutMs": target.health_timeout_ms},
        "description": target.description,
        "builtWith": "desktop-local/scripts/build_component.py",
    }


# --------------------------------------------------------------------- 构建
@dataclass(frozen=True)
class BuildOptions:
    name: str
    out: Path
    python: str = sys.executable
    requirements: Path | None = None
    version: str | None = None
    skip_install: bool = False
    no_venv: bool = False
    extra_site_packages: tuple[Path, ...] = field(default_factory=tuple)
    find_links: tuple[Path, ...] = field(default_factory=tuple)
    offline: bool = False
    compresslevel: int = 6
    keep_work: bool = False
    work_dir: Path | None = None


def build(
    options: BuildOptions,
    *,
    installer: Callable[[Path, Path], None] | None = None,
) -> dict[str, Any]:
    """跑完整的构建流程，返回摘要（**不**打印）。

    ``installer`` 是测试用的注入点：默认走真实 ``pip install -r``，测试里换成
    "往 venv 的 site-packages 写一个假包"，这样整条链路（venv → 拷贝 → zip →
    manifest）都能被验证，且不联网。
    """
    target = target_for(options.name)
    version = str(options.version or target.version).strip()
    if not VERSION_RE.match(version):
        raise BuildError(f"manifest.version 不合法：{version!r}（只允许字母数字与 . _ + -）")

    requirements = options.requirements or (REQUIREMENTS_DIR / target.requirements)
    requirements = Path(requirements)
    install = installer or functools.partial(
        install_requirements,
        find_links=options.find_links,
        offline=options.offline,
    )
    # 锁文件缺失要在建 venv **之前**就报，别让人等 30 秒才看到"文件不存在"
    if not options.no_venv and not options.skip_install and not requirements.is_file():
        raise BuildError(f"依赖锁文件不存在：{requirements}（用 --requirements 指定，或加 --no-venv 跳过依赖）")

    started = time.monotonic()
    timings: dict[str, float] = {}
    work = Path(options.work_dir) if options.work_dir else Path(tempfile.mkdtemp(prefix=f"chiron-{target.name}-"))
    work.mkdir(parents=True, exist_ok=True)
    payload = work / "payload"
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)

    site_dirs: list[Path] = []
    lock_text = ""
    venv_info: dict[str, Any] = {"used": False}
    copied: dict[str, Any] = {"files": 0, "bytes": 0, "skipped": [], "overridden": [], "sources": []}

    try:
        # --- 1. 隔离 venv + 装依赖 ---
        if not options.no_venv:
            step = time.monotonic()
            venv_dir = work / "venv"
            venv_py = create_venv(options.python, venv_dir)
            timings["venv"] = round(time.monotonic() - step, 2)
            venv_info = {"used": True, "dir": str(venv_dir), "python": str(venv_py)}

            if not options.skip_install:
                step = time.monotonic()
                install(venv_py, requirements)
                timings["install"] = round(time.monotonic() - step, 2)
                lock_text = pip_freeze(venv_py)
            site_dirs = venv_site_packages(venv_py)
            _log(f"venv site-packages：{', '.join(str(item) for item in site_dirs) or '（空）'}")

        all_site_dirs = [*site_dirs, *options.extra_site_packages]

        # --- 2. 组装 payload ---
        step = time.monotonic()
        if all_site_dirs:
            copied = copy_site_packages(all_site_dirs, payload / RUNTIME_DIR / SITE_PACKAGES_DIR)
        else:
            (payload / RUNTIME_DIR / SITE_PACKAGES_DIR).mkdir(parents=True, exist_ok=True)

        if lock_text:
            (payload / RUNTIME_DIR / LOCK_NAME).write_text(lock_text, encoding="utf-8")

        write_launcher(payload / LAUNCHER_NAME, target)

        build_info = {
            "component": target.name,
            "version": version,
            "entry": LAUNCHER_NAME,
            "importName": target.import_name,
            "builtAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "builtWith": "desktop-local/scripts/build_component.py",
            "buildPython": sys.version,
            "buildExecutable": sys.executable,
            "basePython": options.python,
            "platform": sys.platform,
            "requiresPython": ">=3.10",
            "requirements": str(requirements) if not options.no_venv else "",
            "dependencies": {
                "files": copied["files"],
                "bytes": copied["bytes"],
                "skipped": len(copied["skipped"]),
            },
            "warnings": [
                "runtime/site-packages 是纯文件拷贝：C 扩展（.pyd/.so）与解释器版本、平台绑定，"
                "必须在目标平台用目标 Python 版本构建",
            ],
        }
        (payload / RUNTIME_DIR / BUILD_INFO_NAME).write_text(
            json.dumps(build_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        timings["payload"] = round(time.monotonic() - step, 2)

        # --- 3. 打包 + 自检 ---
        step = time.monotonic()
        entries = collect_payload_files(payload)
        if not entries:
            raise BuildError("payload 为空，没什么可打包的")
        limits = check_payload_limits(entries)
        bundle = write_bundle(payload, entries, work / BUNDLE_NAME, compresslevel=options.compresslevel)
        problems = validate_bundle(Path(bundle["path"]), entry=LAUNCHER_NAME)
        if problems:
            raise BuildError("自检未通过：" + "；".join(problems))
        timings["bundle"] = round(time.monotonic() - step, 2)

        # --- 4. 落位成组件源布局 ---
        out_dir = Path(options.out) / target.name
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle["path"], out_dir / BUNDLE_NAME)
        manifest = build_manifest(target, version, bundle)
        (out_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        if options.keep_work:
            _log(f"保留构建临时目录：{work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    elapsed = round(time.monotonic() - started, 2)
    return {
        "name": target.name,
        "version": version,
        "out": str(out_dir),
        "manifest": str(out_dir / MANIFEST_NAME),
        "bundle": {
            "path": str(out_dir / BUNDLE_NAME),
            "bytes": bundle["bytes"],
            "sha256": bundle["sha256"],
            "entries": bundle["entries"],
            "uncompressedBytes": bundle["uncompressedBytes"],
        },
        "entry": LAUNCHER_NAME,
        "files": limits["files"],
        "payloadBytes": limits["uncompressedBytes"],
        "sitePackages": {
            "files": copied["files"],
            "bytes": copied["bytes"],
            "skipped": len(copied["skipped"]),
            "skippedSample": copied["skipped"][:10],
            "overridden": len(copied["overridden"]),
            "sources": copied["sources"],
        },
        "venv": venv_info,
        "timings": timings,
        "elapsedSeconds": elapsed,
        # 未压缩总量 / zip 大小：越大说明压得越狠（纯二进制依赖通常接近 1）
        "compressionRatio": round(bundle["uncompressedBytes"] / bundle["bytes"], 2) if bundle["bytes"] else 0,
    }


# --------------------------------------------------------------------- 校验
def verify_package(package_dir: Path, *, name: str | None = None) -> dict[str, Any]:
    """校验一个已产出的组件包（不构建、不联网）。

    按 manager 的 manifest 规则逐项复核：name/version/entry 形状、每个文件的
    sha256、归档条目安全、entry 真的在归档里、体积上限。
    """
    package_dir = Path(package_dir)
    manifest_path = package_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BuildError(f"没有 manifest：{manifest_path}")
    try:
        raw = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"manifest 不是合法 JSON：{manifest_path}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise BuildError("manifest 必须是 JSON 对象")

    problems: list[str] = []
    manifest_name = str(raw.get("name") or "")
    if name and manifest_name != name:
        problems.append(f"manifest.name（{manifest_name}）与请求的组件（{name}）不一致")
    if manifest_name not in TARGETS:
        problems.append(f"manifest.name 不在受管组件表里：{manifest_name!r}")
    version = str(raw.get("version") or "")
    if not VERSION_RE.match(version):
        problems.append(f"manifest.version 不合法：{version!r}")

    raw_files = raw.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise BuildError("manifest.files 必须是非空数组")

    archived: list[Path] = []
    for item in raw_files:
        if isinstance(item, str):
            rel, digest = item, str(raw.get("sha256") or "")
        elif isinstance(item, dict):
            rel = str(item.get("path") or "")
            digest = str(item.get("sha256") or raw.get("sha256") or "")
        else:
            problems.append("manifest.files 里有非法项（既不是字符串也不是对象）")
            continue
        try:
            rel = safe_entry_path(rel, "manifest.files 里的文件")
        except BuildError as exc:
            problems.append(str(exc))
            continue
        digest = digest.strip().lower()
        if digest.startswith("sha256:"):
            digest = digest.split(":", 1)[1].strip()
        if not HEX_RE.match(digest):
            problems.append(f"{rel} 的 sha256 不是 64 位十六进制：{digest!r}")
            continue
        target_file = package_dir / rel
        if not target_file.is_file():
            problems.append(f"缺文件：{target_file}")
            continue
        actual = sha256_file(target_file)
        if actual != digest:
            problems.append(f"{rel} 校验不符：期望 {digest}，实际 {actual}")
        archived.append(target_file)

    entry = str(raw.get("entry") or "")
    for candidate in archived:
        if candidate.suffix.lower() == ".zip":
            problems += validate_bundle(candidate, entry=entry or LAUNCHER_NAME)

    if problems:
        raise BuildError("产物校验失败：" + "；".join(problems))

    return {
        "name": manifest_name,
        "version": version,
        "dir": str(package_dir),
        "entry": entry,
        "files": [str(item) for item in archived],
        "bytes": sum(item.stat().st_size for item in archived),
        "ok": True,
    }


# --------------------------------------------------------------------- CLI
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_component.py",
        description="构建 PageIndex / LangExtract 组件包（产出组件源目录布局 <out>/<name>/）",
    )
    parser.add_argument("--name", required=True, choices=sorted(TARGETS), help="组件名")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"组件源根目录（默认 {DEFAULT_OUT}）",
    )
    parser.add_argument("--python", default=sys.executable, help="建 venv 用的基础解释器（默认当前解释器）")
    parser.add_argument("--requirements", type=Path, default=None, help="依赖锁文件（默认 scripts/components/<name>.requirements.txt）")
    parser.add_argument("--version", default=None, help="覆盖 manifest.version（默认取组件包版本）")
    parser.add_argument("--no-venv", action="store_true", help="不建 venv、不装依赖（离线冒烟用）")
    parser.add_argument("--skip-install", action="store_true", help="建 venv 但不 pip install")
    parser.add_argument(
        "--extra-site-packages",
        action="append",
        default=[],
        type=Path,
        metavar="DIR",
        help="额外并入的 site-packages 目录（可重复；离线 wheel 用）",
    )
    parser.add_argument("--compresslevel", type=int, default=6, help="zip 压缩级别 0-9（默认 6）")
    parser.add_argument(
        "--find-links",
        action="append",
        default=[],
        type=Path,
        metavar="DIR",
        help="额外给 pip 的本地 wheel 目录（可重复；CI 缓存 / 离线构建用）",
    )
    parser.add_argument("--offline", action="store_true", help="pip 加 --no-index：只从 --find-links 装，不联网")
    parser.add_argument("--keep-work", action="store_true", help="保留构建临时目录（排查用）")
    parser.add_argument("--verify", action="store_true", help="只校验 <out>/<name> 现有产物，不构建")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not 0 <= args.compresslevel <= 9:
        _log(f"--compresslevel 必须在 0-9 之间：{args.compresslevel}")
        return 2
    try:
        if args.verify:
            report = verify_package(Path(args.out) / args.name, name=args.name)
        else:
            options = BuildOptions(
                name=args.name,
                out=Path(args.out),
                python=args.python,
                requirements=args.requirements,
                version=args.version,
                skip_install=args.skip_install,
                no_venv=args.no_venv,
                extra_site_packages=tuple(args.extra_site_packages),
                find_links=tuple(args.find_links),
                offline=args.offline,
                compresslevel=args.compresslevel,
                keep_work=args.keep_work,
            )
            report = build(options)
    except BuildError as exc:
        _log(f"失败：{exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    _log("完成" if args.verify else f"完成：{report['bundle']['bytes']} 字节，sha256 {report['bundle']['sha256'][:12]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
