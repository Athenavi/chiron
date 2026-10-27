"""本地组件管理：安装、校验、启停（pageindex / langextract）。

设计边界（与 ``frontend-vue/electron/components.cjs`` 对齐，见
``docs/desktop-local/components.md``）：

- **组件源由用户提供**，本模块不内置任何下载地址：``CHIRON_COMPONENT_BASE_URL``
  指向一个本地目录（``<源>/<name>/manifest.json``）或一个 HTTP 基址
  （``<基址>/<name>/manifest.json``）。未配置时安装返回 409
  ``component_source_unconfigured``，绝不静默成功。
- **安装只落到 data-dir 之下**：``<data-dir>/components/<name>/<version>``，全过程在
  ``<data-dir>/components/.tmp`` 里的临时目录完成，校验通过后才 move 过去；失败不留残骸。
- **路径穿越一律拒绝**：``..`` / 绝对路径 / 盘符 / symlink（zip 与 tar 都查）都在解压前
  逐条目拦下，不靠 ``extractall`` 的默认行为。
- **sha256 是硬要求**：每个待安装文件都必须有校验值（逐文件 ``sha256``，或单个归档配
  顶层 ``sha256``）。缺校验值的 manifest 直接判为非法，不做"反正都是本地源"的例外。
- **启动只 spawn ``entry``**：``.py`` 用解释器（``CHIRON_PYTHON`` 优先，否则当前 Python），
  ``.js`` 用 node，其余按可执行文件直接执行；环境变量下发 ``CHIRON_DATA_DIR`` /
  ``CHIRON_INFERENCE_PROXY`` 以及 ``CHIRON_COMPONENT_NAME`` / ``_VERSION`` / ``_PORT``
  （端口由我们借一个空闲端口后告知组件，健康检查与 ``baseUrl`` 都基于它）。
- **子进程输出写文件**（``<data-dir>/logs/components/<name>.log``），不读 PIPE：读不完的
  PIPE 会写满缓冲把组件卡死，而组件日志不该进 core 的 stdout。

本模块是**同步**实现（下载 / 解压 / spawn 都是阻塞调用），HTTP 层用
``asyncio.to_thread`` 调用，避免把事件循环占住。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

# --------------------------------------------------------------------- 常量
#: 组件源（契约名）；同义的 ``CHIRON_LOCAL_`` 前缀版本也认（config.py 的 env_prefix）
ENV_BASE_URL = "CHIRON_COMPONENT_BASE_URL"
ENV_BASE_URL_LOCAL = "CHIRON_LOCAL_COMPONENT_BASE_URL"
#: 可选，指定启动 ``.py`` 组件用的解释器
ENV_PYTHON = "CHIRON_PYTHON"
#: 可选，指定启动 ``.js`` 组件用的运行时
ENV_NODE = "CHIRON_NODE"

MANIFEST_NAME = "manifest.json"
INSTALLED_NAME = "installed.json"

#: 组件名白名单之外的写法一律拒绝（同时也是路径穿越的第一道闸）
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")

#: 单个文件 / 整包 / 解压总量的上限（本地组件够用，防止 tar bomb 撑爆磁盘）
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
#: 组件清单本身不该有这么大
MAX_MANIFEST_BYTES = 1024 * 1024

HTTP_TIMEOUT = 30.0
DEFAULT_HEALTH_TIMEOUT = 20.0
STOP_TIMEOUT = 5.0
KILL_TIMEOUT = 3.0
HEALTH_INTERVAL = 0.1


# --------------------------------------------------------------------- 异常
class ComponentError(Exception):
    """组件操作的统一异常：``status`` / ``code`` 直接映射成 HTTP 响应。"""

    status = 500
    code = "component_error"

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra: dict[str, Any] = extra


class UnknownComponentError(ComponentError):
    status = 404
    code = "unknown_component"


class InvalidComponentNameError(ComponentError):
    status = 400
    code = "invalid_component_name"


class SourceUnconfiguredError(ComponentError):
    status = 409
    code = "component_source_unconfigured"


class SourceUnavailableError(ComponentError):
    status = 502
    code = "component_source_unavailable"


class NotInSourceError(ComponentError):
    status = 404
    code = "component_not_in_source"


class ManifestInvalidError(ComponentError):
    status = 400
    code = "component_manifest_invalid"


class IntegrityFailedError(ComponentError):
    status = 400
    code = "component_integrity_failed"


class UnsafeEntryError(ComponentError):
    status = 400
    code = "component_unsafe_path"


class NotInstalledError(ComponentError):
    status = 404
    code = "component_not_installed"


class AlreadyRunningError(ComponentError):
    status = 409
    code = "component_running"


class StartFailedError(ComponentError):
    status = 500
    code = "component_start_failed"


class UnsupportedEntryError(ComponentError):
    status = 400
    code = "component_entry_unsupported"


# --------------------------------------------------------------------- 规格
@dataclass(frozen=True)
class ComponentSpec:
    """一个组件的静态元数据（不含任何安装状态）。"""

    name: str
    title: str
    description: str
    #: manifest 没给 ``entry`` 时用的默认入口（空 = 必须由 manifest 提供）
    default_entry: str = ""
    #: 启动时额外下发的环境变量（组件私有配置，``env.setdefault`` 语义）
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FileSpec:
    """manifest 里的一个待安装文件。"""

    rel: str
    sha256: str


@dataclass(frozen=True)
class ComponentManifest:
    """解析并校验过形状的 ``manifest.json``。"""

    name: str
    version: str
    entry: str
    args: tuple[str, ...]
    files: tuple[FileSpec, ...]
    health_path: str = ""
    health_timeout: float = DEFAULT_HEALTH_TIMEOUT
    raw: Mapping[str, Any] = field(default_factory=dict)


def default_specs() -> tuple[ComponentSpec, ...]:
    """默认组件表。函数内导入避免 ``__init__`` ↔ 子模块的循环。"""
    from app.components.langextract import COMPONENT as LANGEXTRACT
    from app.components.pageindex import COMPONENT as PAGEINDEX

    return (PAGEINDEX, LANGEXTRACT)


# --------------------------------------------------------------------- 路径安全
def safe_component_name(raw: str) -> str:
    """组件名必须是白名单形态的小写标识符；``../x``、``a/b`` 一类直接拒绝。"""
    token = str(raw or "").strip()
    if not token:
        raise InvalidComponentNameError("组件名不能为空")
    if "/" in token or "\\" in token or token in {".", ".."}:
        raise InvalidComponentNameError(f"组件名不能含路径分隔符或上跳：{raw}")
    if not NAME_PATTERN.match(token):
        raise InvalidComponentNameError(f"组件名不合法：{raw}（只允许小写字母、数字与 . _ -）")
    return token


def safe_relative(raw: str, what: str = "路径") -> str:
    """把 manifest / 归档里的条目名规范成安全的相对 POSIX 路径。

    拒绝：空、绝对路径、Windows 盘符、任何 ``..`` 段落。返回的是 ``/`` 分隔的相对路径，
    可以直接与安装目录拼接。
    """
    text = str(raw or "").strip()
    if not text:
        raise UnsafeEntryError(f"{what}为空")
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        raise UnsafeEntryError(f"{what}是绝对路径：{text}")
    if _DRIVE_PATTERN.match(normalized):
        raise UnsafeEntryError(f"{what}带盘符：{text}")
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafeEntryError(f"{what}含上跳（..）：{text}")
        parts.append(part)
    if not parts:
        raise UnsafeEntryError(f"{what}规范化后为空：{text}")
    return "/".join(parts)


def ensure_within(root: Path, target: Path) -> Path:
    """确认 ``target`` 落在 ``root`` 内（解析 symlink 之后），返回解析后的路径。"""
    root_resolved = root.resolve()
    resolved = target.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise UnsafeEntryError(f"路径逃出安装目录：{target}")
    return resolved


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_archive(rel: str) -> bool:
    lowered = rel.lower()
    return any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def free_port() -> int:
    """借一个空闲 loopback 端口（只取号，不占坑）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def dir_size(root: Path) -> int:
    """目录体积：跳过符号链接，读不到的条目忽略（不抛）。"""
    total = 0
    if not root.is_dir():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
        except OSError:
            continue
    return total


# --------------------------------------------------------------------- manifest
def _normalize_digest(value: Any, what: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1].strip()
    if not HEX_PATTERN.match(text):
        raise ManifestInvalidError(f"{what} 的 sha256 不是 64 位十六进制：{value!r}")
    return text


def _parse_files(payload: Mapping[str, Any]) -> list[list[str]]:
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ManifestInvalidError("manifest.files 必须是非空数组（文件相对路径）")
    if len(raw_files) > 256:
        raise ManifestInvalidError("manifest.files 条目过多（上限 256）")

    entries: list[list[str]] = []
    for item in raw_files:
        if isinstance(item, str):
            entries.append([safe_relative(item, "manifest.files 里的文件"), ""])
        elif isinstance(item, dict):
            raw_rel = item.get("path") or item.get("name") or ""
            rel = safe_relative(raw_rel, "manifest.files 里的文件")
            digest = item.get("sha256")
            entries.append([rel, _normalize_digest(digest, f"manifest.files[{rel}]") if digest else ""])
        else:
            raise ManifestInvalidError("manifest.files 的每一项必须是字符串或 {path, sha256} 对象")
    return entries


def _apply_top_level_digests(payload: Mapping[str, Any], entries: list[list[str]]) -> None:
    """把 manifest 顶层 ``sha256`` 摊到各文件上。

    - 字符串：只允许配合**单个** ``files`` 项（典型的"一个归档"场景）。
    - 对象：``{"<rel>": "<hex>"}``，键必须是 ``files`` 里列出的文件。
    """
    top = payload.get("sha256")
    if top is None:
        return
    if isinstance(top, str):
        if len(entries) != 1:
            raise ManifestInvalidError(
                "manifest.sha256 是单个摘要，只能配一个 files 项；多文件请用 {\"path\": \"sha256\"} 形式"
            )
        entries[0][1] = _normalize_digest(top, "manifest.sha256")
        return
    if isinstance(top, dict):
        for key, value in top.items():
            rel = safe_relative(key, "manifest.sha256 的键")
            matched = [entry for entry in entries if entry[0] == rel]
            if not matched:
                raise ManifestInvalidError(f"manifest.sha256 里列出了未登记的文件：{rel}")
            matched[0][1] = _normalize_digest(value, f"manifest.sha256[{rel}]")
        return
    raise ManifestInvalidError("manifest.sha256 必须是字符串（单个归档）或对象（逐文件）")


def _parse_args(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw_args = payload.get("args")
    if raw_args is None:
        return ()
    if not isinstance(raw_args, list):
        raise ManifestInvalidError("manifest.args 必须是字符串数组")
    if len(raw_args) > 64:
        raise ManifestInvalidError("manifest.args 条目过多（上限 64）")
    args: list[str] = []
    for item in raw_args:
        if not isinstance(item, (str, int, float)) or isinstance(item, bool):
            raise ManifestInvalidError("manifest.args 的每一项必须是字符串或数字")
        text = str(item)
        if len(text) > 500:
            raise ManifestInvalidError("manifest.args 单个参数过长（上限 500 字符）")
        args.append(text)
    return tuple(args)


def _parse_health(payload: Mapping[str, Any]) -> tuple[str, float]:
    """``health`` 可以是 ``"/healthz"``、``{"path": "/healthz", "timeoutMs": 5000}`` 或 ``health_path``。"""
    raw = payload.get("health", payload.get("health_path"))
    if raw is None or raw == "":
        return "", DEFAULT_HEALTH_TIMEOUT
    timeout = DEFAULT_HEALTH_TIMEOUT
    if isinstance(raw, str):
        path = raw
    elif isinstance(raw, dict):
        path = str(raw.get("path") or "")
        raw_timeout = raw.get("timeout_ms", raw.get("timeoutMs"))
        if raw_timeout is not None:
            try:
                timeout = float(raw_timeout) / 1000.0
            except (TypeError, ValueError) as exc:
                raise ManifestInvalidError(f"manifest.health.timeoutMs 不是数字：{raw_timeout!r}") from exc
            if timeout <= 0 or timeout > 300:
                raise ManifestInvalidError("manifest.health.timeoutMs 必须在 (0, 300000] 毫秒内")
    else:
        raise ManifestInvalidError("manifest.health 必须是字符串或 {path, timeoutMs} 对象")

    text = path.strip()
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        raise ManifestInvalidError(f"manifest.health.path 必须是以 / 开头且不含空白的本地路径：{path!r}")
    return text, timeout


def parse_manifest(payload: Mapping[str, Any], expected_name: str) -> ComponentManifest:
    """校验 ``manifest.json`` 的形状并补齐默认值；不合法一律抛 ``ManifestInvalidError``。"""
    if not isinstance(payload, Mapping):
        raise ManifestInvalidError("manifest.json 必须是 JSON 对象")

    raw_name = str(payload.get("name") or "").strip()
    if not raw_name:
        raise ManifestInvalidError("manifest.name 缺失")
    if raw_name != expected_name:
        raise ManifestInvalidError(f"manifest.name（{raw_name}）与请求的组件（{expected_name}）不一致")

    raw_version = str(payload.get("version") or "").strip()
    if not raw_version:
        raise ManifestInvalidError("manifest.version 缺失")
    if not VERSION_PATTERN.match(raw_version):
        raise ManifestInvalidError(f"manifest.version 不合法：{raw_version!r}（不能含路径分隔符）")

    entries = _parse_files(payload)
    _apply_top_level_digests(payload, entries)
    missing = [rel for rel, digest in entries if not digest]
    if missing:
        raise ManifestInvalidError(
            "以下文件缺少 sha256 校验值，拒绝安装：" + ", ".join(missing) + "（用 manifest.sha256 或 files[].sha256 提供）"
        )

    entry = str(payload.get("entry") or "").strip()
    if not entry:
        raise ManifestInvalidError("manifest.entry 缺失（组件启动入口的相对路径）")
    entry_rel = safe_relative(entry, "manifest.entry")

    health_path, health_timeout = _parse_health(payload)

    return ComponentManifest(
        name=raw_name,
        version=raw_version,
        entry=entry_rel,
        args=_parse_args(payload),
        files=tuple(FileSpec(rel=rel, sha256=digest) for rel, digest in entries),
        health_path=health_path,
        health_timeout=health_timeout,
        raw=dict(payload),
    )


# --------------------------------------------------------------------- 组件源
def _http_get(url: str, *, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "chiron-local-components/1"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310 —— 只允许 http(s)
            status = getattr(response, "status", 200)
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotInSourceError(f"组件源里没有该资源（404）：{url}") from exc
        raise SourceUnavailableError(f"组件源返回 HTTP {exc.code}：{url}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SourceUnavailableError(f"无法访问组件源 {url}：{exc}") from exc
    if status != 200:
        raise SourceUnavailableError(f"组件源返回 HTTP {status}：{url}")
    if len(data) > limit:
        raise SourceUnavailableError(f"组件源文件超过上限 {limit} 字节：{url}")
    return data


@dataclass(frozen=True)
class ComponentSource:
    """组件源：本地目录（``kind="dir"``）或 HTTP 基址（``kind="http"``）。"""

    kind: str
    location: str

    @property
    def is_http(self) -> bool:
        return self.kind == "http"

    def describe(self) -> dict[str, Any]:
        if self.is_http:
            return {
                "configured": True,
                "kind": "http",
                "location": self.location,
                "available": True,
                "detail": "HTTP 组件源（可用性在安装时才真正探测）",
            }
        root = Path(self.location)
        available = root.is_dir()
        return {
            "configured": True,
            "kind": "dir",
            "location": str(root),
            "available": available,
            "detail": "本地目录组件源" if available else f"组件源目录不存在：{root}",
        }

    # --- 读取 ---
    def read_manifest(self, name: str) -> dict[str, Any]:
        raw = self._read(name, MANIFEST_NAME, limit=MAX_MANIFEST_BYTES)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ManifestInvalidError(f"{name} 的 manifest.json 不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ManifestInvalidError(f"{name} 的 manifest.json 必须是 JSON 对象")
        return parsed

    def read_file(self, name: str, rel: str) -> bytes:
        safe_rel = safe_relative(rel, f"组件 {name} 的文件")
        return self._read(name, safe_rel, limit=MAX_FILE_BYTES)

    def _read(self, name: str, rel: str, *, limit: int) -> bytes:
        if self.is_http:
            url = f"{self.location.rstrip('/')}/{quote(name)}/{quote(rel)}"
            return _http_get(url, limit=limit)
        root = Path(self.location)
        if not root.is_dir():
            raise SourceUnavailableError(f"组件源目录不存在：{root}（检查 {ENV_BASE_URL}）")
        component_dir = root / name
        if not component_dir.is_dir():
            raise NotInSourceError(f"组件源里没有 {name}：{component_dir}")
        target = ensure_within(component_dir, component_dir / rel)
        if not target.is_file():
            raise NotInSourceError(f"组件源缺少文件：{target}")
        size = target.stat().st_size
        if size > limit:
            raise SourceUnavailableError(f"组件源文件超过上限 {limit} 字节：{target}")
        return target.read_bytes()


def resolve_source(
    settings: Any = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ComponentSource | None:
    """解析组件源；未配置返回 ``None``（调用方据此给 409）。"""
    env = os.environ if environ is None else environ
    value = ""
    for key in (ENV_BASE_URL, ENV_BASE_URL_LOCAL):
        raw = str(env.get(key) or "").strip()
        if raw:
            value = raw
            break
    if not value:
        value = str(getattr(settings, "component_base_url", "") or "").strip()
    if not value:
        return None

    lowered = value.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return ComponentSource(kind="http", location=value)
    if lowered.startswith("file://"):
        return ComponentSource(kind="dir", location=str(Path(value[7:]).expanduser()))
    return ComponentSource(kind="dir", location=str(Path(os.path.expandvars(value)).expanduser()))


# --------------------------------------------------------------------- 解压
def _extract_zip(archive: Path, dest: Path, budget: list[int]) -> None:
    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                rel = safe_relative(info.filename, "归档条目")
                target = ensure_within(dest, dest / rel)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise UnsafeEntryError(f"归档含符号链接，拒绝解压：{info.filename}")
                budget[0] += info.file_size
                if budget[0] > MAX_TOTAL_BYTES:
                    raise ManifestInvalidError(f"归档解压后超过上限 {MAX_TOTAL_BYTES} 字节：{archive.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
    except zipfile.BadZipFile as exc:
        raise ManifestInvalidError(f"归档损坏，无法解压：{archive.name}（{exc}）") from exc


def _extract_tar(archive: Path, dest: Path, budget: list[int]) -> None:
    try:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if member.issym() or member.islnk():
                    raise UnsafeEntryError(f"归档含符号链接/硬链接，拒绝解压：{member.name}")
                if not (member.isfile() or member.isdir()):
                    raise UnsafeEntryError(f"归档含不支持的条目类型（设备/管道等）：{member.name}")
                rel = safe_relative(member.name, "归档条目")
                target = ensure_within(dest, dest / rel)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                budget[0] += member.size
                if budget[0] > MAX_TOTAL_BYTES:
                    raise ManifestInvalidError(f"归档解压后超过上限 {MAX_TOTAL_BYTES} 字节：{archive.name}")
                source = tf.extractfile(member)
                if source is None:
                    raise ManifestInvalidError(f"归档条目不可读：{member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
    except tarfile.TarError as exc:
        raise ManifestInvalidError(f"归档损坏，无法解压：{archive.name}（{exc}）") from exc


def extract_archive(archive: Path, dest: Path) -> None:
    """把 ``archive`` 解压到 ``dest``；逐条目校验路径与类型，不调用 ``extractall``。"""
    dest.mkdir(parents=True, exist_ok=True)
    lowered = archive.name.lower()
    budget = [0]
    if lowered.endswith(".zip"):
        _extract_zip(archive, dest, budget)
        return
    if any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES if suffix != ".zip"):
        _extract_tar(archive, dest, budget)
        return
    raise ManifestInvalidError(f"不支持的归档格式：{archive.name}")


# --------------------------------------------------------------------- 进程记录
@dataclass
class ProcessRecord:
    """一个在跑的子进程（含日志句柄，停止时一并关掉）。"""

    name: str
    version: str
    entry: str
    child: subprocess.Popen[bytes]
    port: int
    base_url: str
    started_at: str
    log_path: Path
    log_handle: Any

    def alive(self) -> bool:
        return self.child.poll() is None

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "entry": self.entry,
            "running": self.alive(),
            "pid": self.child.pid,
            "port": self.port,
            "baseUrl": self.base_url,
            "startedAt": self.started_at,
            "log": str(self.log_path),
            "exitCode": self.child.returncode,
        }

    def terminate(self) -> None:
        """SIGTERM → 等 → SIGKILL；句柄最后关。"""
        if self.alive():
            try:
                self.child.terminate()
            except OSError:
                pass  # 已经退出：下面 wait 会用 returncode 反映
            try:
                self.child.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    self.child.kill()
                except OSError:
                    pass
                try:
                    self.child.wait(timeout=KILL_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pass  # 交给 OS 回收，不再无限等
        try:
            self.log_handle.close()
        except OSError:
            pass


# --------------------------------------------------------------------- 管理器
class ComponentManager:
    """组件安装 / 启停的唯一入口（一个 core 进程一个实例，挂在 app.state 上）。"""

    def __init__(
        self,
        settings: Any = None,
        *,
        specs: Iterable[ComponentSpec] | None = None,
        env: Mapping[str, str] | None = None,
        data_dir: str | os.PathLike[str] | None = None,
        inference_proxy: str | None = None,
        source: ComponentSource | None = None,
    ) -> None:
        merged = dict(os.environ)
        merged.update({str(key): str(value) for key, value in (env or {}).items()})
        self._env = merged
        self.settings = settings

        raw_data_dir = data_dir if data_dir is not None else getattr(settings, "data_dir", None)
        self.data_dir = Path(raw_data_dir) if raw_data_dir else Path.cwd() / "chiron-local"
        if inference_proxy is not None:
            self.inference_proxy = str(inference_proxy)
        else:
            self.inference_proxy = str(getattr(settings, "inference_proxy", "") or "")

        self._specs: dict[str, ComponentSpec] = {}
        for spec in specs if specs is not None else default_specs():
            self._specs[spec.name] = spec
        self._source = source
        self._records: dict[str, ProcessRecord] = {}
        self._lock = threading.Lock()

    # --- 路径 ---
    @property
    def component_root(self) -> Path:
        return self.data_dir / "components"

    def component_dir(self, name: str) -> Path:
        return self.component_root / safe_component_name(name)

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs" / "components"

    def specs(self) -> tuple[ComponentSpec, ...]:
        return tuple(self._specs.values())

    def _spec(self, raw: str) -> ComponentSpec:
        token = safe_component_name(raw)
        spec = self._specs.get(token)
        if spec is None:
            raise UnknownComponentError(f"未知组件：{raw}（可选：{' / '.join(self._specs) or '无'}）")
        return spec

    # --- 组件源 ---
    def source(self) -> ComponentSource | None:
        if self._source is None:
            self._source = resolve_source(self.settings, environ=self._env)
        return self._source

    def set_source(self, source: ComponentSource | None) -> None:
        self._source = source

    def require_source(self) -> ComponentSource:
        source = self.source()
        if source is None:
            raise SourceUnconfiguredError(
                f"未配置组件源：请把 {ENV_BASE_URL} 指向本地目录或 HTTP 基址后重启本地后端"
                "（例如 CHIRON_COMPONENT_BASE_URL=/path/to/components）",
            )
        return source

    def source_summary(self) -> dict[str, Any]:
        source = self.source()
        if source is None:
            return {
                "configured": False,
                "kind": "",
                "location": "",
                "available": False,
                "detail": f"未配置 {ENV_BASE_URL}（本地目录或 HTTP 基址）",
            }
        return source.describe()

    # --- 安装状态 ---
    def installed(self, name: str) -> dict[str, Any] | None:
        """返回该组件当前生效的安装记录；未安装返回 ``None``。

        识别两种来源：本模块写的 ``<version>/installed.json``，以及用户手工把
        ``manifest.json`` 直接放进 ``<data-dir>/components/<name>``。
        """
        token = safe_component_name(name)
        base = self.component_root / token
        if not base.is_dir():
            return None

        candidates: list[tuple[float, Path, dict[str, Any]]] = []
        for record_file in base.glob(f"*/{INSTALLED_NAME}"):
            try:
                data = json.loads(record_file.read_text("utf-8"))
                mtime = record_file.stat().st_mtime
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            candidates.append((mtime, record_file.parent, data))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            _, path, data = candidates[0]
            args = data.get("args") if isinstance(data.get("args"), list) else []
            files = data.get("files") if isinstance(data.get("files"), list) else []
            entry = str(data.get("entry") or "")
            return {
                "name": token,
                "version": str(data.get("version") or ""),
                "entry": entry,
                "args": [str(arg) for arg in args],
                "health": str(data.get("health") or ""),
                "dir": str(path),
                "source": str(data.get("source") or ""),
                "sourceKind": str(data.get("sourceKind") or ""),
                "installedAt": str(data.get("installedAt") or ""),
                "files": files,
                "entryExists": bool(entry) and (path / entry).is_file() if entry else False,
                "manual": False,
            }

        manifest_file = base / MANIFEST_NAME
        if manifest_file.is_file():
            try:
                raw = json.loads(manifest_file.read_text("utf-8"))
            except (OSError, ValueError):
                return None
            if not isinstance(raw, dict):
                return None
            entry = str(raw.get("entry") or "")
            args = raw.get("args") if isinstance(raw.get("args"), list) else []
            return {
                "name": token,
                "version": str(raw.get("version") or ""),
                "entry": entry,
                "args": [str(arg) for arg in args],
                "health": "",
                "dir": str(base),
                "source": str(manifest_file),
                "sourceKind": "manual",
                "installedAt": "",
                "files": [],
                "entryExists": bool(entry) and (base / entry).is_file() if entry else False,
                "manual": True,
            }
        return None

    # --- 运行状态 ---
    def record(self, name: str) -> ProcessRecord | None:
        token = safe_component_name(name)
        with self._lock:
            record = self._records.get(token)
            if record is not None and not record.alive():
                self._records.pop(token, None)
                return None
            return record

    def is_running(self, name: str) -> bool:
        return self.record(name) is not None

    def list(self) -> list[dict[str, Any]]:
        """契约 §2 的 ``ComponentInfo[]``，另附 pid / baseUrl / dir 供诊断。"""
        items: list[dict[str, Any]] = []
        for spec in self._specs.values():
            installed = self.installed(spec.name)
            record = self.record(spec.name)
            running = record is not None
            items.append(
                {
                    "name": spec.name,
                    "title": spec.title,
                    "description": spec.description,
                    "version": installed["version"] if installed else "",
                    "installed": installed is not None,
                    "running": running,
                    "sizeBytes": dir_size(self.component_dir(spec.name)),
                    "entry": installed["entry"] if installed else "",
                    "dir": str(self.component_dir(spec.name)),
                    "pid": record.child.pid if record else None,
                    "baseUrl": record.base_url if record else "",
                }
            )
        return items

    def status(self, name: str) -> dict[str, Any]:
        spec = self._spec(name)
        for item in self.list():
            if item["name"] == spec.name:
                return item
        raise UnknownComponentError(f"未知组件：{name}")

    # --- 安装 ---
    def install(self, name: str) -> dict[str, Any]:
        """从组件源安装 ``name``：取 manifest → 下载 → 校验 → 解压 → 落位 → 写 installed.json。"""
        spec = self._spec(name)
        if self.is_running(spec.name):
            raise AlreadyRunningError(f"组件 {spec.name} 正在运行，请先停止再安装")
        source = self.require_source()

        raw_manifest = source.read_manifest(spec.name)
        manifest = parse_manifest(raw_manifest, spec.name)

        staging_root = self.component_root / ".tmp"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{spec.name}-{manifest.version}-", dir=str(staging_root)))
        payload = staging / "payload"
        payload.mkdir()
        installed_files: list[dict[str, Any]] = []
        total = 0

        try:
            for item in manifest.files:
                data = source.read_file(spec.name, item.rel)
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise SourceUnavailableError(f"组件 {spec.name} 的文件总量超过上限 {MAX_TOTAL_BYTES} 字节")
                digest = sha256_hex(data)
                if digest != item.sha256:
                    raise IntegrityFailedError(
                        f"组件 {spec.name} 的文件 {item.rel} 校验失败：期望 {item.sha256}，实际 {digest}",
                        file=item.rel,
                        expected=item.sha256,
                        actual=digest,
                    )
                target = ensure_within(payload, payload / item.rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                installed_files.append({"path": item.rel, "sha256": digest, "bytes": len(data)})

            for item in manifest.files:
                if not is_archive(item.rel):
                    continue
                archive = ensure_within(payload, payload / item.rel)
                extract_archive(archive, payload)
                archive.unlink()

            entry_path = ensure_within(payload, payload / manifest.entry)
            if not entry_path.is_file():
                raise ManifestInvalidError(
                    f"组件 {spec.name} 的 entry 在安装包里不存在：{manifest.entry}"
                    "（若 entry 在归档内，请确认 manifest.entry 是解压后的相对路径）"
                )

            version_dir = self.component_dir(spec.name) / manifest.version
            if version_dir.exists():
                shutil.rmtree(version_dir)
            version_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(payload), str(version_dir))

            record = {
                "name": spec.name,
                "version": manifest.version,
                "entry": manifest.entry,
                "args": list(manifest.args),
                "health": manifest.health_path,
                "files": installed_files,
                "source": source.location,
                "sourceKind": source.kind,
                "installedAt": _utc_now(),
            }
            (version_dir / INSTALLED_NAME).write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            # 原样留一份 manifest：排查时不用回头翻组件源，手工探测也能认出“这是装过的”
            (version_dir / MANIFEST_NAME).write_text(
                json.dumps(dict(manifest.raw), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            self._prune_other_versions(spec.name, manifest.version)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        return {
            "name": spec.name,
            "version": manifest.version,
            "installed": True,
            "dir": str(self.component_dir(spec.name) / manifest.version),
            "entry": manifest.entry,
            "args": list(manifest.args),
            "files": installed_files,
            "source": source.location,
            "installedAt": record["installedAt"],
            "requiresSource": False,
        }

    def _prune_other_versions(self, name: str, keep_version: str) -> None:
        """安装成功后清掉同组件的旧版本目录（只删本模块写的版本目录）。"""
        base = self.component_dir(name)
        if not base.is_dir():
            return
        for child in base.iterdir():
            if child.name == keep_version or not child.is_dir():
                continue
            if not (child / INSTALLED_NAME).is_file():
                continue  # 不是我们装的，不动
            shutil.rmtree(child, ignore_errors=True)

    # --- 启动 ---
    def start(self, name: str, *, health_timeout: float | None = None) -> dict[str, Any]:
        spec = self._spec(name)
        existing = self.record(spec.name)
        if existing is not None:
            return existing.snapshot()

        installed = self.installed(spec.name)
        if installed is None:
            raise NotInstalledError(
                f"组件 {spec.name} 未安装；先调用 POST /v1/components/{spec.name}/install"
                f"（安装目录：{self.component_dir(spec.name)}）"
            )
        if not installed["entry"]:
            raise ManifestInvalidError(f"组件 {spec.name} 的 manifest 缺少 entry，无法启动")
        version_dir = Path(installed["dir"])
        entry = ensure_within(version_dir, version_dir / installed["entry"])
        if not entry.is_file():
            raise NotInstalledError(f"组件 {spec.name} 的 entry 不存在：{entry}")

        port = free_port()
        argv = self._command(entry, installed.get("args") or [])
        child_env = self._child_env(spec, installed["version"], port)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{spec.name}.log"
        handle = log_path.open("ab")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            child = subprocess.Popen(  # noqa: S603 —— 命令来自已校验的 manifest，不是用户输入拼接
                argv,
                cwd=str(version_dir),
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except OSError as exc:
            handle.close()
            raise StartFailedError(f"组件 {spec.name} 启动失败（{argv[0]}）：{exc}") from exc

        record = ProcessRecord(
            name=spec.name,
            version=installed["version"],
            entry=installed["entry"],
            child=child,
            port=port,
            base_url=f"http://127.0.0.1:{port}",
            started_at=_utc_now(),
            log_path=log_path,
            log_handle=handle,
        )
        with self._lock:
            self._records[spec.name] = record

        health = {"path": installed.get("health") or "", "checked": False, "ok": False}
        if health["path"]:
            timeout = health_timeout if health_timeout is not None else DEFAULT_HEALTH_TIMEOUT
            try:
                _wait_health(record, str(health["path"]), timeout)
            except ComponentError:
                record.terminate()
                with self._lock:
                    self._records.pop(spec.name, None)
                raise
            health["checked"] = True
            health["ok"] = True

        snapshot = record.snapshot()
        snapshot["health"] = health
        return snapshot

    def _command(self, entry: Path, args: Sequence[str]) -> list[str]:
        suffix = entry.suffix.lower()
        if suffix == ".py":
            return [self._python(), str(entry), *args]
        if suffix in {".js", ".cjs", ".mjs"}:
            node = self._env.get(ENV_NODE) or shutil.which("node") or "node"
            return [node, str(entry), *args]
        if suffix in {".exe", ".bat", ".cmd", ".sh", ".com", ""}:
            return [str(entry), *args]
        if os.access(entry, os.X_OK):
            return [str(entry), *args]
        raise UnsupportedEntryError(
            f"不支持的组件入口类型：{entry.name}（支持 .py / .js / 可执行文件，或用 CHIRON_PYTHON 指定解释器）"
        )

    def _python(self) -> str:
        return self._env.get(ENV_PYTHON) or sys.executable or "python"

    def _child_env(self, spec: ComponentSpec, version: str, port: int) -> dict[str, str]:
        # 组件源地址不往下传：那是我们的配置，传下去只会让组件误以为自己是源
        env = {key: value for key, value in self._env.items() if key not in {ENV_BASE_URL, ENV_BASE_URL_LOCAL}}
        env["CHIRON_DATA_DIR"] = str(self.data_dir)
        if self.inference_proxy:
            env["CHIRON_INFERENCE_PROXY"] = self.inference_proxy
        env["CHIRON_COMPONENT_NAME"] = spec.name
        env["CHIRON_COMPONENT_VERSION"] = version
        env["CHIRON_COMPONENT_PORT"] = str(port)
        env["CHIRON_COMPONENT_HOST"] = "127.0.0.1"
        for key, value in spec.env:
            env.setdefault(key, value)
        return env

    # --- 停止 ---
    def stop(self, name: str) -> dict[str, Any]:
        """终止组件进程。没在跑时返回 ``stopped: False``（幂等，不报错）。"""
        spec = self._spec(name)
        record = self.record(spec.name)
        if record is None:
            return {"name": spec.name, "running": False, "stopped": False, "detail": "组件未在运行"}
        pid = record.child.pid
        record.terminate()
        with self._lock:
            self._records.pop(spec.name, None)
        return {
            "name": spec.name,
            "running": False,
            "stopped": True,
            "pid": pid,
            "exitCode": record.child.returncode,
            "log": str(record.log_path),
        }

    def stop_all(self) -> list[dict[str, Any]]:
        """停掉所有组件（core 退出时调用，避免留孤儿进程）。"""
        with self._lock:
            names = list(self._records)
        results = []
        for name in names:
            try:
                results.append(self.stop(name))
            except ComponentError as exc:  # pragma: no cover —— 已知组件才会进 records
                results.append({"name": name, "running": False, "stopped": False, "detail": str(exc)})
        return results

    # --- 卸载 ---
    def remove(self, name: str) -> dict[str, Any]:
        spec = self._spec(name)
        if self.is_running(spec.name):
            raise AlreadyRunningError(f"组件 {spec.name} 正在运行，请先停止再卸载")
        base = self.component_dir(spec.name)
        if not base.is_dir():
            raise NotInstalledError(f"组件 {spec.name} 未安装（{base} 不存在）")
        try:
            shutil.rmtree(base)
        except OSError as exc:
            raise ComponentError(f"组件 {spec.name} 卸载失败：{exc}") from exc
        return {"name": spec.name, "removed": True, "dir": str(base)}


# --------------------------------------------------------------------- 健康检查
def _http_ok(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 —— 只探 loopback
            return 200 <= int(getattr(response, "status", 200)) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _wait_health(record: ProcessRecord, path: str, timeout: float) -> None:
    """轮询组件的健康端点；进程中途退出立即失败，不白等到超时。"""
    url = f"{record.base_url}{path}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not record.alive():
            raise StartFailedError(
                f"组件 {record.name} 启动后立即退出（退出码 {record.child.returncode}），"
                f"日志：{record.log_path}"
            )
        if _http_ok(url, timeout=1.0):
            return
        time.sleep(HEALTH_INTERVAL)
    raise StartFailedError(
        f"组件 {record.name} 在 {timeout:.1f}s 内没有响应 {url}，日志：{record.log_path}"
    )


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "ARCHIVE_SUFFIXES",
    "AlreadyRunningError",
    "ComponentError",
    "ComponentManager",
    "ComponentManifest",
    "ComponentSource",
    "ComponentSpec",
    "ENV_BASE_URL",
    "ENV_BASE_URL_LOCAL",
    "ENV_NODE",
    "ENV_PYTHON",
    "FileSpec",
    "INSTALLED_NAME",
    "IntegrityFailedError",
    "InvalidComponentNameError",
    "MANIFEST_NAME",
    "ManifestInvalidError",
    "NotInSourceError",
    "NotInstalledError",
    "ProcessRecord",
    "SourceUnavailableError",
    "SourceUnconfiguredError",
    "StartFailedError",
    "UnknownComponentError",
    "UnsafeEntryError",
    "UnsupportedEntryError",
    "default_specs",
    "dir_size",
    "ensure_within",
    "extract_archive",
    "free_port",
    "is_archive",
    "parse_manifest",
    "resolve_source",
    "safe_component_name",
    "safe_relative",
    "sha256_hex",
]
