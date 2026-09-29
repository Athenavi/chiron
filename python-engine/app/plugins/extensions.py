"""批 H：扩展注册 API（方案 03 §4）—— 拆成**部署级**与**租户级**两层。

deepagents 的 ``register_middleware / register_tool / register_backend_route`` 是
"进程内任意注册"，前提是**单租户进程**。Chiron 是多租户 SaaS，照搬等于"任意 Python
注入引擎进程"，因此必须拆开（方案 03 §4 的表格）：

| 层 | 谁能用 | 能注册什么 | 生效方式 | 安全约束 |
|---|---|---|---|---|
| 部署级 | 部署者（自托管/私有化） | 工具、后端虚拟路由（对接批 B 的 ``CompositeBackend``） | 启动时加载 + **重启生效** | 只读部署目录、不走用户上传通道、校验来源 |
| 租户级 | 租户 | **声明式**能力：启用哪些工具、提示词片段、后端路由映射、MCP server | 运行时热生效 | **不执行租户代码**，只做配置解释 |

**关键判断（硬约束）**：**不提供"租户在引擎进程内注册任意 Python"的通道**。
``TenantExtensions.register_tool`` 对任何调用都拒绝；``interpret_tenant_config``
递归拒绝配置里出现的可调用对象。租户要自定义逻辑，只能走两条既有合法路径 ——
MCP server（外部进程）或插件（``plugin_runner.py`` 子进程沙箱）。

部署级的"校验来源"落在 ``_resolve_within_root``：清单引用的任何 Python 模块都必须
**落在部署目录内**，越界（``../`` 逃逸 / 绝对路径）一律拒绝 —— 否则这份清单本身
就变成了"任意文件读取/执行"的原语。加载失败一律 **fail-soft**（记日志 + 收集
``errors``），绝不拖垮引擎启动（方案 03 §4 验收③）。
"""

from __future__ import annotations

import importlib.util
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

from app.backends.composite import CompositeBackend
from app.backends.protocol import BackendProtocol
from app.config import settings

if TYPE_CHECKING:
    from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

#: 注册来源：部署级（部署者，受信域） / 租户级（声明式，不执行代码）。
OWNER_DEPLOYMENT = "deployment"
OWNER_TENANT = "tenant"

#: ``ToolDef.source`` 取值：部署级扩展注入的工具（与内建 / MCP 区分）。
#: 前端 ``ToolInfo.source`` 的展示文案需同步新增这一来源（见交付说明的接线项）。
SOURCE_DEPLOYMENT = "deployment"

#: 部署清单固定文件名（放在部署目录根）。
MANIFEST_NAME = "extensions.json"


class ExtensionError(Exception):
    """部署级清单的声明非法（来源越界 / 引用无效 / 模块加载失败）。"""


class TenantExtensionError(Exception):
    """租户级配置非法（含可执行对象）或试图注册代码。"""


# ── 部署清单的加载结果 ────────────────────────────────────────────────────


@dataclass
class LoadReport:
    """一次部署级加载的结果（fail-soft：部分成功也返回，错误逐条列出）。"""

    loaded_tools: list[str] = field(default_factory=list)
    loaded_routes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否**全部**加载成功（有任一错误即 False —— 便于启动期告警）。"""
        return not self.errors


# ── 部署级扩展 ────────────────────────────────────────────────────────────


def _normalize_mount(mount: str) -> str:
    """把挂载点归一化为 ``/name/`` 形式（与 ``CompositeBackend`` 的规则一致）。

    在登记时就归一化，是为了让 ``routes`` 诊断面与 ``CompositeBackend.mounts``
    永远一致 —— 否则 ``artifacts`` / ``/artifacts`` / ``/artifacts/`` 会看起来是三条
    不同的路由，排查时误导。
    """
    trimmed = mount.strip().strip("/")
    return "/" if not trimmed else f"/{trimmed}/"


class DeploymentExtensions:
    """部署级扩展的登记表（进程内、**重启生效** —— 按方案 03 §4 不做热切换）。

    工具直接登记进 ``ToolRegistry``（见 ``load_deployment_extensions``）；本类只负责
    **后端虚拟路由** —— 因为它最终要装配成批 B 的 ``CompositeBackend``。
    """

    def __init__(self) -> None:
        self._routes: dict[str, BackendProtocol] = {}

    def register_backend_route(self, mount: str, backend: BackendProtocol) -> None:
        """登记"虚拟路径前缀 → 后端"的映射（同名覆盖，重启生效）。"""
        self._routes[_normalize_mount(mount)] = backend

    @property
    def routes(self) -> dict[str, BackendProtocol]:
        """已登记的路由（归一化后的挂载点 → 后端）。"""
        return dict(self._routes)

    def build_backend(self, default: BackendProtocol) -> CompositeBackend:
        """把已登记路由装配成 ``CompositeBackend``（启动期设进后端上下文的默认后端）。

        无路由时返回的 ``CompositeBackend`` 与 ``default`` 行为等价 —— 调用方
        无需判断"有没有扩展"，接线可以无条件走这一条路径。
        """
        return CompositeBackend(default, dict(self._routes))


def _resolve_within_root(root: Path, rel: str) -> Path:
    """把相对路径解析到部署目录内；越界（``..`` 逃逸 / 绝对路径）一律拒绝。

    这是"校验来源"的落点，也是路径穿越防护点：部署清单**只读部署目录**，任何试图
    引用目录外文件的声明都必须在此被挡下。
    """
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        raise ExtensionError(f"path {rel!r} escapes the deployment directory")
    if not candidate.is_file():
        raise ExtensionError(f"path {rel!r} is not a readable file in the deployment directory")
    return candidate


def _load_module_within(root: Path, rel: str) -> ModuleType:
    """从部署目录内按文件路径加载一个 Python 模块（不登记进 ``sys.modules``）。

    刻意用 ``spec_from_file_location`` 而非普通 import：部署扩展不在 ``app`` 包路径下，
    且必须**按路径**校验来源（越界在 ``_resolve_within_root`` 里已被拒绝）。
    """
    target = _resolve_within_root(root, rel)
    spec = importlib.util.spec_from_file_location(f"_chiron_deploy_ext_{target.stem}", target)
    if spec is None or spec.loader is None:  # pragma: no cover — 路径已确认是文件
        raise ExtensionError(f"cannot load module from {rel!r}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — 部署扩展自身的错误不该冒泡到引擎启动
        raise ExtensionError(f"module {rel!r} failed to import: {exc}") from exc
    return module


def _resolve_attr(root: Path, spec: Any) -> Any:
    """解析清单里的 ``"relative/path.py:attr"`` 引用，返回该属性（须可调用）。"""
    if not isinstance(spec, str) or ":" not in spec:
        raise ExtensionError(
            f"invalid module reference {spec!r} (expected 'relative/path.py:attr')"
        )
    rel, attr = spec.rsplit(":", 1)
    module = _load_module_within(root, rel)
    try:
        obj = getattr(module, attr)
    except AttributeError as exc:
        raise ExtensionError(f"attribute {attr!r} not found in {rel!r}") from exc
    if not callable(obj):
        raise ExtensionError(f"{spec!r} does not resolve to a callable")
    return obj


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _load_tools(
    entries: list[Any], root: Path, registry: ToolRegistry, report: LoadReport
) -> None:
    """注册清单里的工具。每个工具独立校验 —— 一个坏条目不影响其余条目。"""
    for item in entries:
        if not isinstance(item, dict):
            report.errors.append(f"tool entry is not an object: {item!r}")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            report.errors.append("tool entry missing a non-empty 'name'")
            continue
        try:
            # 部署者是受信方，但仍要求 handler 是 async 可调用对象：注册表按
            # `await handler(...)` 调用，同步函数会在执行时才炸 —— 启动期就拒绝更可诊断。
            handler = cast("Callable[..., Awaitable[Any]]", _resolve_attr(root, item.get("handler")))
        except ExtensionError as exc:
            report.errors.append(f"tool {name!r}: {exc}")
            continue
        description_raw = item.get("description")
        parameters_raw = item.get("parameters")
        description: str = description_raw if isinstance(description_raw, str) else ""
        parameters: dict[str, Any] = (
            parameters_raw if isinstance(parameters_raw, dict) else {"type": "object"}
        )
        registry.register(name, description, parameters, handler, source=SOURCE_DEPLOYMENT)
        report.loaded_tools.append(name)


def _load_backend_routes(
    entries: list[Any], root: Path, extensions: DeploymentExtensions, report: LoadReport
) -> None:
    """登记清单里的后端虚拟路由（工厂在部署目录内，被调用后为后端实例）。"""
    for item in entries:
        if not isinstance(item, dict):
            report.errors.append(f"backend route entry is not an object: {item!r}")
            continue
        mount = item.get("mount")
        if not isinstance(mount, str) or not mount.strip():
            report.errors.append("backend route entry missing a non-empty 'mount'")
            continue
        try:
            factory = cast("Callable[[], BackendProtocol]", _resolve_attr(root, item.get("factory")))
        except ExtensionError as exc:
            report.errors.append(f"backend route {mount!r}: {exc}")
            continue
        try:
            backend = factory()
        except Exception as exc:  # noqa: BLE001 — 工厂失败只让这条路由失效
            report.errors.append(f"backend route {mount!r}: factory failed: {exc}")
            continue
        if backend is None:
            report.errors.append(f"backend route {mount!r}: factory returned None")
            continue
        extensions.register_backend_route(mount, backend)
        report.loaded_routes.append(_normalize_mount(mount))


def load_deployment_extensions(
    root: str | Path,
    *,
    registry: ToolRegistry,
    extensions: DeploymentExtensions,
    manifest_name: str = MANIFEST_NAME,
) -> LoadReport:
    """从部署目录加载部署级扩展（工具 + 后端虚拟路由），**fail-soft**。

    契约：任何失败都变成 ``report.errors`` 里的一条，**不抛异常** —— 引擎启动不能被
    一份写坏的扩展清单拖垮（方案 03 §4 验收③）。调用方按 ``report.ok`` 决定是否告警。

    只有部署者能走到这里：``root`` 是部署目录（只读、不进用户上传通道），清单里引用的
    模块必须落在该目录内。
    """
    report = LoadReport()
    root_path = Path(root).resolve()
    manifest = root_path / manifest_name
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.errors.append(f"cannot read deployment manifest {manifest}: {exc}")
        logger.warning("deployment extensions manifest unreadable: %s", exc)
        return report
    if not isinstance(data, dict):
        report.errors.append(f"deployment manifest {manifest} must be a JSON object")
        return report
    _load_tools(_as_list(data.get("tools")), root_path, registry, report)
    _load_backend_routes(_as_list(data.get("backends")), root_path, extensions, report)
    return report


def load_deployment_extensions_from_settings(
    registry: ToolRegistry,
    *,
    extensions: DeploymentExtensions | None = None,
) -> LoadReport | None:
    """按 settings 的部署开关加载部署级扩展（供 ``app/main.py`` 启动流程调用）。

    **默认关**：``deploy_extensions_enabled`` 为假（或目录为空）时什么都不做、返回
    ``None`` —— 与批 G 的 hooks 同样"默认关 = 零行为变化"。

    两个设置项需要 ``app/config.py`` 新增（见交付说明的接线项）；此处用 ``getattr``
    兜底，**字段尚未接线时引擎照常启动**，不会因缺配置而崩溃。
    """
    if not getattr(settings, "deploy_extensions_enabled", False):
        return None
    raw_dir = getattr(settings, "deploy_extensions_dir", "") or ""
    if not str(raw_dir).strip():
        logger.warning(
            "deploy_extensions_enabled=true but deploy_extensions_dir is empty; nothing loaded"
        )
        return None
    ext = extensions if extensions is not None else DeploymentExtensions()
    return load_deployment_extensions(str(raw_dir), registry=registry, extensions=ext)


# ── 租户级扩展（声明式） ──────────────────────────────────────────────────


@dataclass(frozen=True)
class TenantCapabilities:
    """把租户声明式配置解释成的一组能力（**纯数据，不含任何可执行对象**）。"""

    enabled_tools: tuple[str, ...] = ()
    prompt_fragments: tuple[str, ...] = ()
    backend_routes: Mapping[str, str] = field(default_factory=dict)
    mcp_servers: tuple[str, ...] = ()


def _reject_executables(value: Any) -> None:
    """递归拒绝任何可调用对象 —— 租户配置**只能是数据**。

    这是"不执行租户代码"硬约束在解释层的兜底：即便调用方绕过类型注解，把函数塞进
    声明式配置，也会在这里被拒绝，而不是被当成 handler 执行。
    """
    if callable(value):
        raise TenantExtensionError(
            "tenant extensions must be declarative data; executable/callable values "
            "are not allowed (use an MCP server or a plugin sandbox instead)"
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_executables(key)
            _reject_executables(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _reject_executables(item)


def _string_list(value: Any) -> list[str]:
    """从配置里取字符串列表（非字符串 / 空串一律丢弃，不报错）。"""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _string_map(value: Any) -> dict[str, str]:
    """从配置里取 ``{str: str}``（其余键值对丢弃）。"""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, str) and key.strip():
            result[key.strip()] = item.strip()
    return result


def interpret_tenant_config(
    config: Mapping[str, Any] | None,
    *,
    known_tools: Iterable[str] | None = None,
) -> TenantCapabilities:
    """把租户的**声明式**配置解释成一组能力（**不执行任何租户代码**）。

    ``known_tools`` 非空时按它过滤 —— 租户可以声明一个不存在的工具名，但漏进工具面
    的只能是**真实存在**的工具（否则就是一条凭空的"启用项"，会让人误以为已生效）。
    """
    if not config:
        return TenantCapabilities()
    _reject_executables(config)
    tools = _string_list(config.get("enabled_tools"))
    if known_tools is not None:
        known = set(known_tools)
        tools = [name for name in tools if name in known]
    return TenantCapabilities(
        enabled_tools=tuple(tools),
        prompt_fragments=tuple(_string_list(config.get("prompt_fragments"))),
        backend_routes=_string_map(config.get("backend_routes")),
        mcp_servers=tuple(_string_list(config.get("mcp_servers"))),
    )


class TenantExtensions:
    """租户级扩面：**只做声明式配置解释**，绝不执行租户代码。

    只暴露一个 ``interpret``。刻意**不提供** ``register_tool`` 这类"注册"入口 ——
    见 ``register_tool`` 的说明。
    """

    def register_tool(self, name: str, handler: Any) -> None:
        """**拒绝**：租户不能在引擎进程内注册可执行工具（方案 03 §4 硬约束）。

        租户要自定义逻辑只有两条合法路径 —— MCP server 或插件子进程沙箱。这里永不
        成功返回；保留 ``handler`` 参数正是为了让"被拒绝"这件事在调用点显式可见。
        """
        raise TenantExtensionError(
            f"tenant {name!r} cannot register executable code: tenant-level extensions "
            "are declarative only (use an MCP server or the plugin sandbox instead)"
        )

    def interpret(
        self,
        config: Mapping[str, Any] | None,
        *,
        known_tools: Iterable[str] | None = None,
    ) -> TenantCapabilities:
        """解释租户声明式配置（见 ``interpret_tenant_config``）。"""
        return interpret_tenant_config(config, known_tools=known_tools)


__all__ = [
    "MANIFEST_NAME",
    "OWNER_DEPLOYMENT",
    "OWNER_TENANT",
    "SOURCE_DEPLOYMENT",
    "DeploymentExtensions",
    "ExtensionError",
    "LoadReport",
    "TenantCapabilities",
    "TenantExtensionError",
    "TenantExtensions",
    "interpret_tenant_config",
    "load_deployment_extensions",
    "load_deployment_extensions_from_settings",
]
