"""本地组件管理（安装 / 校验 / 启停）的单元测试。

**不联网**：组件源是临时目录，另加一个绑在 ``127.0.0.1:0`` 的 ``http.server`` 覆盖 HTTP 分支。
**会起真进程**：启动测试跑的是本机 Python 写的几行脚本（写环境变量 / 起 HTTP 健康端点），
每个用例都在 ``finally`` 里 ``stop_all``，不留孤儿。

覆盖重点：

- 组件源未配置 → ``SourceUnconfiguredError``（HTTP 层 409 ``component_source_unconfigured``）
- sha256 校验失败 / 路径穿越（``..``、绝对路径、盘符）/ zip 与 tar 里的符号链接
- 安装落位到 ``<data-dir>/components/<name>/<version>`` 并写 ``installed.json``
- 启动下发 ``CHIRON_DATA_DIR`` / ``CHIRON_INFERENCE_PROXY`` / ``CHIRON_COMPONENT_PORT``，
  健康检查通过，停止后进程真的没了
- HTTP 层：列表 / 安装 / 启动 / 停止 / 卸载的形状与错误码
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import socket
import sys
import tarfile
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from app.api import components as components_api
from app.components import (
    AlreadyRunningError,
    ComponentManager,
    ComponentSource,
    IntegrityFailedError,
    InvalidComponentNameError,
    ManifestInvalidError,
    NotInstalledError,
    SourceUnconfiguredError,
    UnsafeEntryError,
    parse_manifest,
    safe_component_name,
    safe_relative,
)
from app.main import create_app
from fastapi.testclient import TestClient

from tests.conftest import make_settings

# --------------------------------------------------------------------- 夹具 / 工具
#: 写一个环境变量快照后睡觉，用于验证「启动参数真的下发了」
SLEEP_SCRIPT = """\
import json, os, time
from pathlib import Path

data = Path(os.environ["CHIRON_DATA_DIR"])
data.mkdir(parents=True, exist_ok=True)
keys = (
    "CHIRON_DATA_DIR",
    "CHIRON_INFERENCE_PROXY",
    "CHIRON_COMPONENT_NAME",
    "CHIRON_COMPONENT_VERSION",
    "CHIRON_COMPONENT_PORT",
)
snapshot = {key: os.environ.get(key, "") for key in keys}
(data / "component-env.json").write_text(json.dumps(snapshot), encoding="utf-8")
time.sleep(60)
"""

#: 在借来的端口上起一个健康端点，用于验证健康检查
HEALTH_SCRIPT = """\
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


port = int(os.environ["CHIRON_COMPONENT_PORT"])
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def make_manager(tmp_path: Path, source: Path | str | None = None, **overrides: Any) -> ComponentManager:
    """构造一个把组件源指向临时目录的管理器（``source=None`` = 未配置源）。"""
    env = {"CHIRON_COMPONENT_BASE_URL": "" if source is None else str(source), "CHIRON_PYTHON": sys.executable}
    env.update(overrides.pop("env", {}))
    return ComponentManager(make_settings(tmp_path), env=env, **overrides)


def build_source(
    root: Path,
    name: str,
    files: dict[str, bytes],
    *,
    version: str = "1.0.0",
    entry: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在 ``root/<name>`` 造一个可安装的组件（文件 + 逐文件 sha256 的 manifest）。"""
    component = root / name
    component.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for rel, data in files.items():
        target = component / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digests[rel] = hashlib.sha256(data).hexdigest()
    payload: dict[str, Any] = {
        "name": name,
        "version": version,
        "files": [{"path": rel, "sha256": digest} for rel, digest in digests.items()],
        "entry": entry or next(iter(files)),
        "args": [],
    }
    if manifest:
        payload.update(manifest)
    (component / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    return payload


def write_zip(path: Path, entries: dict[str, bytes]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path.read_bytes()


def write_tar(path: Path, files: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if symlink is not None:
            link = tarfile.TarInfo(symlink)
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive.addfile(link)
    return path.read_bytes()


@contextmanager
def make_client(settings: Any, manager: ComponentManager | None = None) -> Iterator[TestClient]:
    """把组件路由挂到一个临时 app 上（本批不往 main.py 里注册）。"""
    app = create_app(settings)
    app.include_router(components_api.router)
    if manager is not None:
        app.state.component_manager = manager
    with TestClient(app) as client:
        yield client


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    path = tmp_path / "source"
    path.mkdir()
    return path


# --------------------------------------------------------------------- 工具函数
def test_safe_relative_blocks_traversal() -> None:
    assert safe_relative("./a/b.py") == "a/b.py"
    assert safe_relative("a\\b.py") == "a/b.py"
    for bad in ("../evil.py", "a/../../evil.py", "/etc/passwd", "C:/windows/x.dll", "", "."):
        with pytest.raises(UnsafeEntryError):
            safe_relative(bad)


def test_safe_component_name_blocks_traversal() -> None:
    assert safe_component_name("pageindex") == "pageindex"
    for bad in ("../etc", "a/b", "..", "", "PageIndex", "a" * 80):
        with pytest.raises(Exception):  # noqa: B017 —— 具体类型不重要，拒绝即可
            safe_component_name(bad)


def test_parse_manifest_requires_digest_for_every_file() -> None:
    with pytest.raises(ManifestInvalidError, match="sha256"):
        parse_manifest(
            {"name": "pageindex", "version": "1.0.0", "files": ["app.py"], "entry": "app.py"},
            "pageindex",
        )


def test_parse_manifest_accepts_single_archive_digest() -> None:
    manifest = parse_manifest(
        {
            "name": "pageindex",
            "version": "2.0.0",
            "files": ["bundle.zip"],
            "sha256": "a" * 64,
            "entry": "app.py",
            "args": ["--port", 8080],
            "health": {"path": "/healthz", "timeoutMs": 3000},
        },
        "pageindex",
    )
    assert manifest.files[0].sha256 == "a" * 64
    assert manifest.entry == "app.py"
    assert manifest.args == ("--port", "8080")
    assert manifest.health_path == "/healthz"
    assert manifest.health_timeout == pytest.approx(3.0)


# --------------------------------------------------------------------- 安装
def test_install_requires_source(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    assert manager.source_summary()["configured"] is False
    with pytest.raises(SourceUnconfiguredError, match="CHIRON_COMPONENT_BASE_URL"):
        manager.install("pageindex")
    assert not manager.component_dir("pageindex").exists()


def test_install_from_local_directory(tmp_path: Path, source_dir: Path) -> None:
    build_source(source_dir, "pageindex", {"app.py": b"print('hi')\n", "lib/util.py": b"x = 1\n"})
    manager = make_manager(tmp_path, source_dir)

    result = manager.install("pageindex")

    assert result["installed"] is True
    assert result["version"] == "1.0.0"
    version_dir = tmp_path / "data" / "components" / "pageindex" / "1.0.0"
    assert version_dir.is_dir()
    assert (version_dir / "app.py").read_bytes() == b"print('hi')\n"
    assert (version_dir / "lib" / "util.py").is_file()

    installed = json.loads((version_dir / "installed.json").read_text("utf-8"))
    assert installed["entry"] == "app.py"
    assert installed["source"] == str(source_dir)
    assert {item["path"] for item in installed["files"]} == {"app.py", "lib/util.py"}
    # 原样保留的 manifest 便于排查/手工探测
    assert json.loads((version_dir / "manifest.json").read_text("utf-8"))["version"] == "1.0.0"

    listed = manager.list()
    pageindex = next(item for item in listed if item["name"] == "pageindex")
    assert pageindex["installed"] is True
    assert pageindex["version"] == "1.0.0"
    assert pageindex["sizeBytes"] > 0
    assert pageindex["running"] is False


def test_install_rejects_checksum_mismatch(tmp_path: Path, source_dir: Path) -> None:
    build_source(
        source_dir,
        "pageindex",
        {"app.py": b"print('hi')\n"},
        manifest={"files": [{"path": "app.py", "sha256": "0" * 64}]},
    )
    manager = make_manager(tmp_path, source_dir)

    with pytest.raises(IntegrityFailedError) as excinfo:
        manager.install("pageindex")
    assert excinfo.value.extra["file"] == "app.py"
    assert not manager.component_dir("pageindex").exists()


def test_install_rejects_traversal_in_manifest(tmp_path: Path, source_dir: Path) -> None:
    build_source(source_dir, "pageindex", {"app.py": b"x"}, manifest={"files": ["../evil.py"]})
    manager = make_manager(tmp_path, source_dir)
    with pytest.raises(UnsafeEntryError):
        manager.install("pageindex")

    build_source(source_dir, "langextract", {"app.py": b"x"}, version="../../escape")
    with pytest.raises(ManifestInvalidError, match="version"):
        manager.install("langextract")
    assert not (tmp_path / "data" / "escape").exists()


def test_install_rejects_absolute_entry(tmp_path: Path, source_dir: Path) -> None:
    build_source(source_dir, "pageindex", {"app.py": b"x"}, entry="/bin/sh")
    manager = make_manager(tmp_path, source_dir)
    with pytest.raises(UnsafeEntryError):
        manager.install("pageindex")


def test_install_zip_archive(tmp_path: Path, source_dir: Path) -> None:
    archive = source_dir / "pageindex" / "bundle.zip"
    write_zip(archive, {"pkg/app.py": b"print('zip')\n", "pkg/data/notes.txt": b"notes\n"})
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    build_source(
        source_dir,
        "pageindex",
        {},
        entry="pkg/app.py",
        manifest={"files": ["bundle.zip"], "sha256": digest},
    )

    manager = make_manager(tmp_path, source_dir)
    manager.install("pageindex")

    version_dir = tmp_path / "data" / "components" / "pageindex" / "1.0.0"
    assert (version_dir / "pkg" / "app.py").read_bytes() == b"print('zip')\n"
    assert (version_dir / "pkg" / "data" / "notes.txt").is_file()
    assert not (version_dir / "bundle.zip").exists(), "装完不该留归档"


def test_install_rejects_zip_traversal(tmp_path: Path, source_dir: Path) -> None:
    archive = source_dir / "pageindex" / "bundle.zip"
    write_zip(archive, {"../evil.py": b"boom"})
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    build_source(
        source_dir,
        "pageindex",
        {},
        entry="app.py",
        manifest={"files": ["bundle.zip"], "sha256": digest},
    )

    manager = make_manager(tmp_path, source_dir)
    with pytest.raises(UnsafeEntryError):
        manager.install("pageindex")
    assert not (tmp_path / "data" / "components" / "evil.py").exists()


def test_install_rejects_tar_symlink(tmp_path: Path, source_dir: Path) -> None:
    archive = source_dir / "langextract" / "bundle.tar"
    write_tar(archive, {"pkg/app.py": b"x"}, symlink="pkg/link.py")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    build_source(
        source_dir,
        "langextract",
        {},
        entry="pkg/app.py",
        manifest={"files": ["bundle.tar"], "sha256": digest},
    )

    manager = make_manager(tmp_path, source_dir)
    with pytest.raises(UnsafeEntryError, match="链接"):
        manager.install("langextract")


def test_install_from_http_source(tmp_path: Path, source_dir: Path) -> None:
    build_source(source_dir, "langextract", {"app.py": b"print('http')\n"})
    handler = _QuietHandler
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), lambda *args, **kwargs: handler(*args, directory=str(source_dir), **kwargs)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        manager = make_manager(tmp_path, source=None)
        manager.set_source(ComponentSource(kind="http", location=base))
        manager.install("langextract")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    version_dir = tmp_path / "data" / "components" / "langextract" / "1.0.0"
    assert (version_dir / "app.py").read_bytes() == b"print('http')\n"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """把 ``directory`` 打到请求里；顺带静音访问日志。"""

    def log_message(self, *args: Any) -> None:
        pass


# --------------------------------------------------------------------- 启停
def test_start_and_stop_python_component(tmp_path: Path, source_dir: Path) -> None:
    build_source(source_dir, "pageindex", {"app.py": SLEEP_SCRIPT.encode("utf-8")})
    manager = make_manager(tmp_path, source_dir, inference_proxy="http://127.0.0.1:9/infer")
    manager.install("pageindex")

    try:
        started = manager.start("pageindex")
        assert started["running"] is True
        assert started["pid"] > 0
        assert started["baseUrl"].startswith("http://127.0.0.1:")
        assert started["health"]["checked"] is False

        # 环境变量确实下发到了子进程（等它写完快照）
        snapshot_path = tmp_path / "data" / "component-env.json"
        snapshot = _wait_json(snapshot_path)
        assert snapshot["CHIRON_DATA_DIR"] == str(tmp_path / "data")
        assert snapshot["CHIRON_INFERENCE_PROXY"] == "http://127.0.0.1:9/infer"
        assert snapshot["CHIRON_COMPONENT_NAME"] == "pageindex"
        assert snapshot["CHIRON_COMPONENT_VERSION"] == "1.0.0"
        assert snapshot["CHIRON_COMPONENT_PORT"] == str(started["port"])

        assert manager.is_running("pageindex") is True
        stopped = manager.stop("pageindex")
        assert stopped["stopped"] is True
        assert stopped["pid"] == started["pid"]
        assert manager.is_running("pageindex") is False
    finally:
        manager.stop_all()

    assert manager.stop("pageindex")["stopped"] is False, "停止是幂等的"


def test_start_runs_health_check(tmp_path: Path, source_dir: Path) -> None:
    build_source(
        source_dir,
        "langextract",
        {"app.py": HEALTH_SCRIPT.encode("utf-8")},
        manifest={"health": {"path": "/healthz", "timeoutMs": 10000}},
    )
    manager = make_manager(tmp_path, source_dir)
    manager.install("langextract")
    try:
        started = manager.start("langextract")
        assert started["health"] == {"path": "/healthz", "checked": True, "ok": True}
    finally:
        manager.stop_all()


def test_start_requires_install(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    with pytest.raises(NotInstalledError, match="未安装"):
        manager.start("pageindex")


def test_remove_deletes_directory(tmp_path: Path, source_dir: Path) -> None:
    build_source(source_dir, "pageindex", {"app.py": SLEEP_SCRIPT.encode("utf-8")})
    manager = make_manager(tmp_path, source_dir)
    manager.install("pageindex")

    result = manager.remove("pageindex")
    assert result["removed"] is True
    assert not manager.component_dir("pageindex").exists()
    with pytest.raises(NotInstalledError):
        manager.remove("pageindex")


def test_remove_refuses_while_running(tmp_path: Path, source_dir: Path) -> None:
    build_source(source_dir, "pageindex", {"app.py": SLEEP_SCRIPT.encode("utf-8")})
    manager = make_manager(tmp_path, source_dir)
    manager.install("pageindex")
    try:
        manager.start("pageindex")
        with pytest.raises(AlreadyRunningError):
            manager.remove("pageindex")
    finally:
        manager.stop_all()


def test_manual_layout_is_detected(tmp_path: Path) -> None:
    """用户手工把 manifest.json 放进 ``components/<name>`` 也算已安装（不写 installed.json）。"""
    manager = make_manager(tmp_path)
    manual = manager.component_dir("pageindex")
    manual.mkdir(parents=True)
    (manual / "manifest.json").write_text(json.dumps({"name": "pageindex", "version": "0.9.0"}), "utf-8")
    (manual / "app.py").write_text("print(1)\n", "utf-8")

    installed = manager.installed("pageindex")
    assert installed is not None
    assert installed["manual"] is True
    assert installed["version"] == "0.9.0"
    assert manager.list()[0]["installed"] is True


# --------------------------------------------------------------------- HTTP 层
def test_api_list_without_source(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    with make_client(make_settings(tmp_path), manager) as client:
        response = client.get("/v1/components")
    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body["data"]] == ["pageindex", "langextract"]
    assert body["source"]["configured"] is False
    for item in body["data"]:
        assert item["installed"] is False
        assert item["running"] is False


def test_api_install_without_source_returns_409(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    with make_client(make_settings(tmp_path), manager) as client:
        response = client.post("/v1/components/pageindex/install")
    assert response.status_code == 409
    assert response.json()["error"] == "component_source_unconfigured"


def test_api_install_start_stop_remove(tmp_path: Path, source_dir: Path) -> None:
    build_source(source_dir, "langextract", {"app.py": SLEEP_SCRIPT.encode("utf-8")})
    manager = make_manager(tmp_path, source_dir)
    try:
        with make_client(make_settings(tmp_path), manager) as client:
            installed = client.post("/v1/components/langextract/install")
            assert installed.status_code == 201
            assert installed.json()["data"]["version"] == "1.0.0"

            listed = client.get("/v1/components").json()
            assert listed["source"]["configured"] is True
            entry = next(item for item in listed["data"] if item["name"] == "langextract")
            assert entry["installed"] is True

            started = client.post("/v1/components/langextract/start")
            assert started.status_code == 200
            assert started.json()["data"]["pid"] > 0
            assert started.json()["data"]["baseUrl"].startswith("http://127.0.0.1:")

            running = next(item for item in client.get("/v1/components").json()["data"] if item["name"] == "langextract")
            assert running["running"] is True

            stopped = client.post("/v1/components/langextract/stop")
            assert stopped.status_code == 200
            assert stopped.json()["data"]["stopped"] is True

            idempotent = client.post("/v1/components/langextract/stop")
            assert idempotent.status_code == 200
            assert idempotent.json()["data"]["stopped"] is False

            removed = client.delete("/v1/components/langextract")
            assert removed.status_code == 200
            assert removed.json()["data"]["removed"] is True

            missing = client.post("/v1/components/langextract/start")
            assert missing.status_code == 404
            assert missing.json()["error"] == "component_not_installed"
    finally:
        manager.stop_all()


def test_api_rejects_unknown_and_unsafe_names(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    with make_client(make_settings(tmp_path), manager) as client:
        unknown = client.post("/v1/components/sqlite/install")
        assert unknown.status_code == 404
        assert unknown.json()["error"] == "unknown_component"

        # 带 ``..`` 的名字会在路由层就被规范化掉（拿不到我们的 handler），但也绝不可能命中磁盘
        unsafe = client.post("/v1/components/..%2Fetc/install")
        assert unsafe.status_code in {400, 404}

    # 直接进管理器也必须被拒
    with pytest.raises(InvalidComponentNameError):
        manager.install("../etc")


def test_api_integrity_failure_is_reported(tmp_path: Path, source_dir: Path) -> None:
    build_source(
        source_dir,
        "pageindex",
        {"app.py": b"x"},
        manifest={"files": [{"path": "app.py", "sha256": "f" * 64}]},
    )
    manager = make_manager(tmp_path, source_dir)
    with make_client(make_settings(tmp_path), manager) as client:
        response = client.post("/v1/components/pageindex/install")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "component_integrity_failed"
    assert body["file"] == "app.py"


# --------------------------------------------------------------------- 辅助
def _wait_json(path: Path, timeout: float = 20.0) -> dict[str, Any]:
    """等子进程把 JSON 写出来（启动 + 解释器初始化需要点时间）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                return json.loads(path.read_text("utf-8"))
            except ValueError:
                pass
        time.sleep(0.05)
    raise AssertionError(f"子进程没有写出 {path}")


def test_free_port_is_available() -> None:
    """``free_port`` 借到的端口当下确实可以绑定（健康检查依赖这一点）。"""
    from app.components.manager import free_port

    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
