"""批 H：扩展注册 API 测试（方案 03 §4）。

四类验收对应：
① 部署级扩展能在启动加载时注册工具 / 后端虚拟路由；
② **租户无法注册任意 Python**（可调用对象一律被拒绝）；
③ 租户级声明式配置能被解释成「启用了哪些工具」；
④ 部署目录外的路径被拒（路径穿越防护）。

另有 fail-soft 用例（清单缺失 / 损坏不拖垮加载）。
"""

from __future__ import annotations

import json

import pytest

from app.backends.composite import CompositeBackend
from app.backends.protocol import (
    EditResult,
    FileStat,
    GlobResult,
    GrepResult,
    LsResult,
    ReadBytesResult,
    ReadResult,
    WriteResult,
)
from app.config import settings
from app.plugins.extensions import (
    SOURCE_DEPLOYMENT,
    DeploymentExtensions,
    TenantExtensionError,
    TenantExtensions,
    interpret_tenant_config,
    load_deployment_extensions,
    load_deployment_extensions_from_settings,
)
from app.tools.registry import ToolRegistry

#: 部署目录里的 handler 模块（部署者是受信方，允许正常 Python）。
HANDLER_SRC = '''
async def echo(*, msg="", **_kw):
    return {"echo": msg}
'''

#: 后端工厂模块（自包含，避免依赖 tests 是否为可 import 的包）。
BACKEND_SRC = '''
class _ArtifactsResult:
    def __init__(self, path):
        self.path = "artifacts:" + path


class _ArtifactsBackend:
    async def read(self, path, *, offset=0, limit=200):
        return _ArtifactsResult(path)


def artifacts_backend():
    return _ArtifactsBackend()
'''


class _StubBackend:
    """最小 ``BackendProtocol`` 实现：只让 ``read`` 携带可辨认的标签。"""

    def __init__(self, label: str) -> None:
        self.label = label

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> ReadResult:
        return ReadResult(path=f"{self.label}:{path}", content=self.label)

    async def read_bytes(self, path: str) -> ReadBytesResult:
        return ReadBytesResult(path=path)

    async def stat(self, path: str) -> FileStat | None:
        return None

    async def write(self, path: str, content: str) -> WriteResult:
        return WriteResult(path=path)

    async def edit(self, path: str, old: str, new: str) -> EditResult:
        return EditResult(path=path)

    async def ls(self, path: str = ".") -> LsResult:
        return LsResult()

    async def glob(self, pattern: str, *, path: str = ".") -> GlobResult:
        return GlobResult()

    async def grep(self, pattern: str, *, path: str = ".", max_results: int = 100) -> GrepResult:
        return GrepResult()

    def supports_execution(self) -> bool:
        return False


def _write_manifest(root, manifest: dict) -> None:
    (root / "extensions.json").write_text(json.dumps(manifest), encoding="utf-8")


# ── ① 部署级扩展注册工具 ──────────────────────────────────────────────────


async def test_deployment_registers_tool_and_executes(tmp_path):
    (tmp_path / "handlers.py").write_text(HANDLER_SRC, encoding="utf-8")
    _write_manifest(
        tmp_path,
        {
            "tools": [
                {
                    "name": "deploy_echo",
                    "description": "部署级回显工具",
                    "parameters": {"type": "object", "properties": {"msg": {"type": "string"}}},
                    "handler": "handlers.py:echo",
                }
            ]
        },
    )

    registry = ToolRegistry()
    report = load_deployment_extensions(
        tmp_path, registry=registry, extensions=DeploymentExtensions()
    )

    assert report.ok, report.errors
    assert report.loaded_tools == ["deploy_echo"]
    tool = registry.get("deploy_echo")
    assert tool is not None
    # 来源标注为部署级，与内建 / MCP 区分
    assert tool.source == SOURCE_DEPLOYMENT
    # 工具真的可执行（handler 从部署目录加载）
    assert (await registry.execute("deploy_echo", {"msg": "hi"}))["echo"] == "hi"


# ── ① 部署级扩展注册后端虚拟路由 ──────────────────────────────────────────


async def test_deployment_registers_backend_route(tmp_path):
    (tmp_path / "backends.py").write_text(BACKEND_SRC, encoding="utf-8")
    _write_manifest(
        tmp_path,
        {"backends": [{"mount": "/artifacts/", "factory": "backends.py:artifacts_backend"}]},
    )

    extensions = DeploymentExtensions()
    report = load_deployment_extensions(
        tmp_path, registry=ToolRegistry(), extensions=extensions
    )

    assert report.ok, report.errors
    assert report.loaded_routes == ["/artifacts/"]
    assert "/artifacts/" in extensions.routes

    # 装配成 CompositeBackend 后，虚拟路径真的被路由到挂载后端
    composite = extensions.build_backend(_StubBackend("default"))
    assert isinstance(composite, CompositeBackend)
    assert "/artifacts/" in composite.mounts
    routed = await composite.read("/artifacts/blob.txt")
    assert routed.path == "artifacts:blob.txt"
    # 默认路径仍走默认后端
    plain = await composite.read("a.txt")
    assert plain.path == "default:a.txt"


# ── ④ 部署目录外的路径被拒（路径穿越防护） ───────────────────────────────


async def test_deployment_rejects_relative_path_traversal(tmp_path):
    # 部署目录内的文件正常存在，但清单故意引用目录外的 ../outside.py
    root = tmp_path / "deploy"
    root.mkdir()
    (tmp_path / "outside.py").write_text(HANDLER_SRC, encoding="utf-8")
    _write_manifest(
        root,
        {"tools": [{"name": "evil", "handler": "../outside.py:echo"}]},
    )

    registry = ToolRegistry()
    report = load_deployment_extensions(
        root, registry=registry, extensions=DeploymentExtensions()
    )

    assert not report.ok
    assert any("escapes" in err for err in report.errors)
    assert registry.get("evil") is None


async def test_deployment_rejects_absolute_path(tmp_path):
    root = tmp_path / "deploy"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text(HANDLER_SRC, encoding="utf-8")
    _write_manifest(
        root,
        {"tools": [{"name": "evil", "handler": f"{outside.as_posix()}:echo"}]},
    )

    registry = ToolRegistry()
    report = load_deployment_extensions(
        root, registry=registry, extensions=DeploymentExtensions()
    )

    assert not report.ok
    assert registry.get("evil") is None


# ── fail-soft：清单缺失 / 损坏不拖垮加载 ─────────────────────────────────


async def test_missing_manifest_is_fail_soft(tmp_path):
    report = load_deployment_extensions(
        tmp_path, registry=ToolRegistry(), extensions=DeploymentExtensions()
    )
    assert not report.ok
    assert report.errors  # 记录了原因而不是抛异常


async def test_corrupt_manifest_is_fail_soft(tmp_path):
    (tmp_path / "extensions.json").write_text("{ not json", encoding="utf-8")
    report = load_deployment_extensions(
        tmp_path, registry=ToolRegistry(), extensions=DeploymentExtensions()
    )
    assert not report.ok


async def test_bad_entry_does_not_block_good_entry(tmp_path):
    # 一个坏条目（handler 不可调用）不应影响另一个好条目
    (tmp_path / "handlers.py").write_text(HANDLER_SRC + "\nNOT_CALLABLE = 42\n", encoding="utf-8")
    _write_manifest(
        tmp_path,
        {
            "tools": [
                {"name": "bad", "handler": "handlers.py:NOT_CALLABLE"},
                {"name": "good", "handler": "handlers.py:echo"},
            ]
        },
    )

    registry = ToolRegistry()
    report = load_deployment_extensions(
        tmp_path, registry=registry, extensions=DeploymentExtensions()
    )

    assert not report.ok  # 有错误
    assert report.loaded_tools == ["good"]  # 但好条目照常加载
    assert registry.get("good") is not None
    assert registry.get("bad") is None


# ── ② 租户无法注册任意 Python ─────────────────────────────────────────────


def test_tenant_register_tool_is_rejected():
    tenant = TenantExtensions()
    with pytest.raises(TenantExtensionError):
        tenant.register_tool("evil", lambda: None)


def test_tenant_config_with_callable_is_rejected():
    # 即便把函数塞进"声明式"配置，解释层也会拒绝它（不执行租户代码）
    with pytest.raises(TenantExtensionError):
        interpret_tenant_config({"enabled_tools": [lambda: 1]})


def test_tenant_config_with_nested_callable_is_rejected():
    with pytest.raises(TenantExtensionError):
        interpret_tenant_config({"mcp_servers": ["ok"], "extra": {"cb": print}})


# ── ③ 租户声明式配置 → 「启用了哪些工具」 ────────────────────────────────


def test_tenant_declarative_enabled_tools():
    caps = interpret_tenant_config(
        {
            "enabled_tools": ["read_file", "write_file"],
            "prompt_fragments": ["总是用中文回答"],
            "mcp_servers": ["git"],
            "backend_routes": {"/artifacts/": "artifacts"},
        }
    )
    assert caps.enabled_tools == ("read_file", "write_file")
    assert caps.prompt_fragments == ("总是用中文回答",)
    assert caps.mcp_servers == ("git",)
    assert caps.backend_routes == {"/artifacts/": "artifacts"}


def test_tenant_unknown_tools_are_filtered():
    caps = interpret_tenant_config(
        {"enabled_tools": ["read_file", "does_not_exist"]},
        known_tools={"read_file", "write_file"},
    )
    assert caps.enabled_tools == ("read_file",)


def test_tenant_empty_config_is_empty_capabilities():
    caps = interpret_tenant_config(None)
    assert caps.enabled_tools == ()
    assert caps.prompt_fragments == ()
    assert caps.mcp_servers == ()


def test_tenant_extensions_facade_interprets():
    caps = TenantExtensions().interpret({"enabled_tools": ["read_file"]})
    assert caps.enabled_tools == ("read_file",)


# ── 启动路径：settings 开关（批 H 接线，`app/main.py` 走的正是这一条） ──


def test_settings_disabled_loads_nothing(tmp_path, monkeypatch):
    """默认关：即使目录里有可用清单也不加载（零行为变化）。"""
    (tmp_path / "handlers.py").write_text(HANDLER_SRC, encoding="utf-8")
    _write_manifest(
        tmp_path, {"tools": [{"name": "deploy_echo", "handler": "handlers.py:echo"}]}
    )
    monkeypatch.setattr(settings, "deploy_extensions_enabled", False)
    monkeypatch.setattr(settings, "deploy_extensions_dir", str(tmp_path))

    registry = ToolRegistry()
    assert load_deployment_extensions_from_settings(registry) is None
    assert registry.get("deploy_echo") is None


def test_settings_enabled_without_dir_is_fail_soft(monkeypatch):
    """开了开关却没配目录：返回 None（记告警），不抛异常。"""
    monkeypatch.setattr(settings, "deploy_extensions_enabled", True)
    monkeypatch.setattr(settings, "deploy_extensions_dir", "")

    assert load_deployment_extensions_from_settings(ToolRegistry()) is None


def test_settings_enabled_loads_tool_and_backend_route(tmp_path, monkeypatch):
    """开启 + 清单有效：工具进注册表，后端虚拟路由进 DeploymentExtensions。"""
    (tmp_path / "handlers.py").write_text(HANDLER_SRC, encoding="utf-8")
    (tmp_path / "backends.py").write_text(BACKEND_SRC, encoding="utf-8")
    _write_manifest(
        tmp_path,
        {
            "tools": [{"name": "deploy_echo", "handler": "handlers.py:echo"}],
            "backends": [
                {"mount": "/artifacts/", "factory": "backends.py:artifacts_backend"}
            ],
        },
    )
    monkeypatch.setattr(settings, "deploy_extensions_enabled", True)
    monkeypatch.setattr(settings, "deploy_extensions_dir", str(tmp_path))

    registry = ToolRegistry()
    extensions = DeploymentExtensions()
    report = load_deployment_extensions_from_settings(registry, extensions=extensions)

    assert report is not None, "开关开启 + 清单有效时不应返回 None"
    assert report.ok, report.errors
    assert registry.get("deploy_echo") is not None
    assert "/artifacts/" in extensions.routes
