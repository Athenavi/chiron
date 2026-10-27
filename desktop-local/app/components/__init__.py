"""本地组件包：pageindex / langextract 的安装与启停。

分层：

- :mod:`app.components.manager`  **唯一**做安装、校验、spawn、停止的模块；
- :mod:`app.components.pageindex` / :mod:`app.components.langextract`  只有元数据与薄封装；
- :mod:`app.api.components`  只做 HTTP 形状转换（``{"data": ...}`` / 明确错误码）。

组件源由 ``CHIRON_COMPONENT_BASE_URL`` 指定（本地目录或 HTTP 基址），**不内置下载地址**；
未配置时安装返回 409 ``component_source_unconfigured``。详见
``docs/desktop-local/components.md``。
"""

from __future__ import annotations

from typing import Any

from app.components.langextract import COMPONENT as LANGEXTRACT
from app.components.manager import (
    ENV_BASE_URL,
    ENV_BASE_URL_LOCAL,
    ENV_NODE,
    ENV_PYTHON,
    INSTALLED_NAME,
    MANIFEST_NAME,
    AlreadyRunningError,
    ComponentError,
    ComponentManager,
    ComponentManifest,
    ComponentSource,
    ComponentSpec,
    FileSpec,
    IntegrityFailedError,
    InvalidComponentNameError,
    ManifestInvalidError,
    NotInSourceError,
    NotInstalledError,
    SourceUnavailableError,
    SourceUnconfiguredError,
    StartFailedError,
    UnknownComponentError,
    UnsafeEntryError,
    UnsupportedEntryError,
    default_specs,
    parse_manifest,
    resolve_source,
    safe_component_name,
    safe_relative,
    sha256_hex,
)
from app.components.pageindex import COMPONENT as PAGEINDEX

#: 受管组件（顺序即 UI 展示顺序）。``core`` 由 Electron 主进程管理，不在此表中。
COMPONENT_SPECS: tuple[ComponentSpec, ...] = (PAGEINDEX, LANGEXTRACT)
COMPONENT_NAMES: tuple[str, ...] = tuple(spec.name for spec in COMPONENT_SPECS)


def spec_for(name: str) -> ComponentSpec:
    """按名字取组件元数据；未知组件抛 ``UnknownComponentError``。"""
    token = safe_component_name(name)
    for spec in COMPONENT_SPECS:
        if spec.name == token:
            return spec
    raise UnknownComponentError(f"未知组件：{name}（可选：{' / '.join(COMPONENT_NAMES)}）")


def describe(settings: Any = None, *, manager: ComponentManager | None = None) -> dict[str, Any]:
    """组件层的一个快照（给 ``/readyz`` 一类诊断端点用，不含密钥）。"""
    instance = manager or ComponentManager(settings)
    return {"source": instance.source_summary(), "components": instance.list()}


__all__ = [
    "COMPONENT_NAMES",
    "COMPONENT_SPECS",
    "ENV_BASE_URL",
    "ENV_BASE_URL_LOCAL",
    "ENV_NODE",
    "ENV_PYTHON",
    "INSTALLED_NAME",
    "LANGEXTRACT",
    "MANIFEST_NAME",
    "PAGEINDEX",
    "AlreadyRunningError",
    "ComponentError",
    "ComponentManager",
    "ComponentManifest",
    "ComponentSource",
    "ComponentSpec",
    "FileSpec",
    "IntegrityFailedError",
    "InvalidComponentNameError",
    "ManifestInvalidError",
    "NotInSourceError",
    "NotInstalledError",
    "SourceUnavailableError",
    "SourceUnconfiguredError",
    "StartFailedError",
    "UnknownComponentError",
    "UnsafeEntryError",
    "UnsupportedEntryError",
    "default_specs",
    "describe",
    "parse_manifest",
    "resolve_source",
    "safe_component_name",
    "safe_relative",
    "sha256_hex",
    "spec_for",
]
