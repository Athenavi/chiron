"""MCP 凭据投影（D4）—— 让凭据**只到达它该去的地方**。

## 问题

MCP server 需要凭据才能连（stdio 走 `ServerDef.env`，HTTP 走 `url` 里的 query/token 或
自定义头）。而引擎进程**正是 agent 能读到的地方** —— 它能 `shell_exec`、能读文件、能把
任何东西写进回答。`app/mcp/client.py` 的注释已经承认这条风险（"连接凭据会留在引擎进程里，
而那正是 agent 能读到的地方"），并给了两条缓解：只直连 `read_only` 的 server、stdio 走
`sandboxed_env()`。但还缺**第三条：凭据不得出现在任何输出面上**。

## 本模块负责什么

把"凭据"当作一等对象来对待，提供三件事（都是**纯函数**，可单测）：

1. ``collect_secrets(server)``：从 server 定义里**找出**凭据值（env 值、URL 的 userinfo 与
   敏感 query、args 里紧随敏感旗标的值）；
2. ``redact(text, secrets)``：把凭据替换成 ``[redacted]`` —— 用于日志、错误消息、工具结果；
3. ``safe_label(server)``：给日志用的**不含凭据**的标识（URL 只保留 scheme+host）。

## 边界（有意为之）

* **不做加密存储**：凭据来自插件配置（DB/环境），存储与轮换是那条链路的事。这里只管
  "**它不会再从别的地方漏出来**"。
* **不改连接行为**：本模块不决定"要不要连"，那是 `assert_server_connectable` 的守卫。
* **不猜**：找不出凭据就返回空列表 —— 宁可少脱敏，也不要把正常文本乱替换（那会让日志无法阅读）。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

#: 环境变量名里出现这些片段即视为**凭据**（大小写不敏感）。
SENSITIVE_ENV_MARKERS: tuple[str, ...] = (
    "token", "secret", "password", "passwd", "api_key", "apikey", "access_key",
    "private_key", "credential", "auth",
)

#: URL query 里出现这些名字即视为**凭据**。
SENSITIVE_QUERY_KEYS: frozenset[str] = frozenset({
    "token", "access_token", "api_key", "apikey", "key", "secret", "password", "sig", "signature",
})

#: args 里出现这些旗标时，认为**紧随其后的那个值**是凭据。
SENSITIVE_FLAGS: frozenset[str] = frozenset({
    "--token", "--api-key", "--apikey", "--key", "--secret", "--password", "--auth",
})

#: 替换占位符（与 guards 的输出脱敏保持同一形态，便于统一排查）。
PLACEHOLDER = "[redacted]"

#: 值短于此长度时不做替换 —— 太短的值（如 "-"、"1"）会在正常文本里到处误命中。
MIN_SECRET_LEN = 6


def _is_sensitive_env_name(name: str) -> bool:
    lowered = (name or "").lower()
    return any(marker in lowered for marker in SENSITIVE_ENV_MARKERS)


def collect_secrets(server: Any) -> list[str]:
    """从 ``ServerDef``（或任何有同名属性的对象）里收集凭据值。

    来源三处：``env`` 的敏感键值、``url`` 的 userinfo 与敏感 query、``args`` 里紧随敏感旗标的值。
    返回**去重且已过滤短值**的列表。
    """
    found: list[str] = []

    env = getattr(server, "env", None)
    if isinstance(env, dict):
        for key, value in env.items():
            if isinstance(value, str) and value and _is_sensitive_env_name(str(key)):
                found.append(value)

    url = getattr(server, "url", "") or ""
    if url:
        try:
            parts = urlsplit(url)
        except ValueError:
            parts = None
        if parts is not None:
            if parts.password:
                found.append(parts.password)
            if parts.username:
                found.append(parts.username)
            for qkey, qval in parse_qsl(parts.query, keep_blank_values=False):
                if qkey.lower() in SENSITIVE_QUERY_KEYS and qval:
                    found.append(qval)

    args = getattr(server, "args", None)
    if isinstance(args, (list, tuple)):
        for i, item in enumerate(args):
            if not isinstance(item, str):
                continue
            # 形如 "--token=abc"
            for flag in SENSITIVE_FLAGS:
                prefix = f"{flag}="
                if item.lower().startswith(prefix):
                    value = item[len(prefix):]
                    if value:
                        found.append(value)
            # 形如 "--token" "abc"
            if item.lower() in SENSITIVE_FLAGS and i + 1 < len(args):
                nxt = args[i + 1]
                if isinstance(nxt, str) and nxt:
                    found.append(nxt)

    out: list[str] = []
    for value in found:
        value = value.strip()
        if len(value) >= MIN_SECRET_LEN and value not in out:
            out.append(value)
    # 长的先替换：避免 "abc" 把 "abcdef" 先切掉，留下 "def" 这种半截残留。
    out.sort(key=len, reverse=True)
    return out


def redact(text: str, secrets: list[str]) -> str:
    """把文本里的凭据替换成占位符（**纯函数**）。

    逐字替换而不是正则：凭据可能含正则元字符（`. ` `*` `?` `(`…），拼成 pattern 既易错
    又可能被恶意构造的值利用（ReDoS）。
    """
    if not text or not secrets:
        return text
    out = text
    for secret in secrets:
        if secret and secret in out:
            out = out.replace(secret, PLACEHOLDER)
    return out


def safe_label(server: Any) -> str:
    """给日志用的**不含凭据**的标识。

    URL 只保留 `scheme://host:port/path`（丢掉 userinfo 与整个 query）——
    这两处正是 HTTP 型 MCP 放凭据的地方。
    """
    name = str(getattr(server, "name", "") or "<unnamed>")
    url = str(getattr(server, "url", "") or "")
    if not url:
        return name
    try:
        parts = urlsplit(url)
    except ValueError:
        return name
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return f"{name} ({urlunsplit((parts.scheme, netloc, parts.path, '', ''))})"


def project_stdio_env(server: Any, base_env: dict[str, str]) -> dict[str, str]:
    """计算拉取 stdio server 时**传给子进程**的环境。

    `base_env` 应当已经过 `sandboxed_env()` 清理（宿主环境不进子进程）。这里只叠加该 server
    **自己声明**的 env —— 于是凭据只存在于那个子进程的环境里，不写回引擎进程的 `os.environ`。

    返回一个新 dict（不改动入参），便于调用方断言"引擎进程环境没被污染"。
    """
    env = dict(base_env or {})
    declared = getattr(server, "env", None)
    if isinstance(declared, dict):
        for key, value in declared.items():
            if isinstance(key, str) and isinstance(value, str):
                env[key] = value
    return env


# ── 进程级凭据注册表（收尾3：让脱敏成为**必经之路**）─────────────────────────

#: 运行时才知道的凭据（MCP server 的 env/url/args 值）。
#:
#: 为什么需要"注册"这一步：`redact(text, secrets)` 要求调用方**手里有 secrets**。而日志、
#: 工具结果、审计这些出站路径各自散落在不同模块，让每一处都去收集一次 MCP 凭据，迟早漏一处
#: （漏掉的那处就是泄漏）。把它们登记在一个进程级集合里，**既有的出站脱敏收口**
#: （`app/subagent/redact.py` 的 `redact_text`）就能自动覆盖 —— 不必再要求每个调用点记得处理。
#:
#: 只存**值**，不存来源（来源字符串本身可能含凭据）。进程内、不持久化、不回显。
_REGISTERED: set[str] = set()


def register_secrets(values: Any) -> int:
    """把已知凭据登记进进程级集合，返回**新增**的数量。

    过滤规则与 `collect_secrets` 一致（短值不收 —— 否则正常文本会被误替换）。
    """
    added = 0
    if values is None:
        return 0
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        return 0
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if len(value) < MIN_SECRET_LEN:
            continue
        if value not in _REGISTERED:
            _REGISTERED.add(value)
            added += 1
    return added


def registered_secrets() -> list[str]:
    """当前登记的全部凭据（**长的在前**：避免短值先命中把长值切成半截残留）。"""
    return sorted(_REGISTERED, key=len, reverse=True)


def reset_registered_secrets() -> None:
    """清空登记表（测试用；MCP 重连后也应当重新登记，避免留下已作废的旧凭据）。"""
    _REGISTERED.clear()
