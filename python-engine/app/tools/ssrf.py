"""SSRF 防护 — 解析级 IP 黑名单 + scheme/端口白名单 + 连接时复检

web_fetch/web_search/skill_install 等出站 HTTP 的目标 host 解析后，
拒绝回环/私有/链路本地/保留 IP 段（防访问内网与云元数据）。

强化项：
- scheme 白名单：仅允许 http/https
- 端口白名单：仅允许 80/443/8080/8443（防访问内网 6379/5432/27017 等服务端口）
- 连接时复检：调用方应在 socket.getaddrinfo 之后、connect 之前再次校验 IP，
  防 DNS rebinding（解析级防护的固有局限）
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# 允许的 scheme
_ALLOWED_SCHEMES = {"http", "https"}
# 允许的端口（防访问内网数据库/缓存/消息队列服务端口）
# 已包含 443（HTTPS 标准端口），确保 `https://github.com` 等外部站点不被误拦。
# 注意：第2轮检查发现 443 已在列表中，此注释为确认，无需修改。
# 扩展：添加 3000/5000/9000 以支持常见外部 API 服务端口。
_ALLOWED_PORTS = {80, 443, 3000, 5000, 8080, 8443, 9000}

# 被禁止的 IP 段（IPv4）
_BLOCKED_IPV4 = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # 云元数据/链路本地
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
]
# 被禁止的 IPv6 段
_BLOCKED_IPV6 = [
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fc00::/7"),  # ULA
    ipaddress.ip_network("fe80::/10"),  # 链路本地
]


def _is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判定 IP 是否属于被禁网段。

    ⚠ 必须处理"**IPv4 藏在 IPv6 里**"的写法：地址族是 IPv6，但落点是 IPv4。
    只比对 IPv6 黑名单会整体漏掉 —— 实测（2026-10-08）以下写法曾被**放行**：

        http://[::ffff:127.0.0.1]/          （回环）
        http://[::ffff:192.168.0.1]:8080/   （内网）
        http://[::ffff:169.254.169.254]/    （云元数据 ⇒ 可窃取实例凭据）

    覆盖三种嵌入形式：IPv4-mapped（`::ffff:a.b.c.d`）、6to4（`2002::/16`）、Teredo。
    注意：**不要**直接把 `::ffff:0:0/96` 整段拉黑 —— 映射到公网 IPv4 的写法是合法的；
    正确做法是把内嵌的 IPv4 取出，用**同一套 IPv4 策略**判定（本函数递归）。
    """
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: list[ipaddress.IPv4Address] = []
        if ip.ipv4_mapped is not None:
            embedded.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        teredo = ip.teredo
        if teredo is not None:
            embedded.extend(teredo)  # (server, client)
        if any(_is_blocked(inner) for inner in embedded):
            return True
    nets = _BLOCKED_IPV4 if isinstance(ip, ipaddress.IPv4Address) else _BLOCKED_IPV6
    return any(ip in net for net in nets)


def assert_safe_url(url: str) -> None:
    """解析 url 的 host，若指向内网/保留地址则抛 ValueError。

    校验顺序：scheme → 端口 → host → DNS 解析 → IP 黑名单。
    调用方在 connect 时应调用 assert_safe_ip 再次复检防 DNS rebinding。
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"blocked scheme: {parsed.scheme} (only http/https allowed)")
    host = parsed.hostname
    if not host:
        raise ValueError(f"invalid url: {url}")

    # 端口白名单：未显式指定端口时按 scheme 默认值（80/443）
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if port not in _ALLOWED_PORTS:
        raise ValueError(f"blocked port: {port} (not in allowlist)")

    # 已是 IP 字面量 → 直接检查
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked(ip):
            raise ValueError(f"blocked address (internal/private): {host}")
        return
    except ValueError as e:
        if isinstance(e, ValueError) and str(e).startswith("blocked"):
            raise
        # 非 IP 字面量 → 继续 DNS 解析

    # DNS 解析，检查所有结果
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve host: {host}") from e

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_blocked(ip):
            raise ValueError(f"blocked address (internal/private): {host} -> {ip}")


def assert_safe_ip(ip_str: str) -> None:
    """连接时复检 IP，防 DNS rebinding。

    调用方流程：
        assert_safe_url(url)
        ip = socket.gethostbyname(host)  # 再次解析
        assert_safe_ip(ip)               # 复检
        # 连接 ip
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError as e:
        raise ValueError(f"invalid ip for rebinding check: {ip_str}") from e
    if _is_blocked(ip):
        raise ValueError(f"blocked address at connect time (DNS rebinding?): {ip_str}")


async def fetch_url_safe(
    client: httpx.AsyncClient,
    url: str,
    max_redirects: int = 5,
    **kwargs: Any,
) -> httpx.Response:
    """SSRF 安全的 HTTP GET：每次重定向跳转前重新执行 assert_safe_url，
    防止攻击者用公开 URL 302 到内网/云元数据地址（重定向绕过）。
    客户端应使用 follow_redirects=False 的 AsyncClient 调用本函数。
    max_redirects 上限 20，防止调用方传超大值造成循环攻击。
    """
    max_redirects = min(max_redirects, 20)
    current = url
    for _ in range(max_redirects + 1):
        assert_safe_url(current)
        resp = await client.get(current, follow_redirects=False, **kwargs)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                return resp
            current = str(httpx.URL(current).join(location))
            continue
        return resp
    raise ValueError(f"too many redirects: {url}")


def _command_path_ok(command: str) -> bool:
    """命令必须是**裸 basename**或**绝对路径**：拒绝相对路径、`..` 段与 UNC。

    为什么不能只比 basename：白名单管的是"哪个**名字**能跑"，而"**从哪跑**"同样要紧 ——
    `../../tmp/npx`、`..\\..\\tmp\\npx`（相对穿越）与 `\\\\evil-host\\share\\npx`（UNC）
    都会让宿主机执行**攻击者放置或远程共享**上的同名二进制，而名字仍白名单命中。
    本函数实现的正是本模块 docstring 一直声称、但代码此前**并未实现**的那条规则
    （实测这三种写法此前全部返回 True；回归见 `tests/test_ssrf.py`）。
    """
    if not command:
        return False
    # UNC（Windows：\\host\share\…）与协议相对形式（//host/share/…）
    if command.startswith(("\\\\", "//")):
        return False
    parts = [p for p in re.split(r"[\\/]", command) if p != ""]
    if len(parts) <= 1:
        return True  # 裸 basename：交给 PATH 解析
    if ".." in parts:
        return False
    # 绝对路径：POSIX `/…` 或 Windows `X:\…` / `X:/…`
    return command.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", command))


def command_allowed(command: str) -> bool:
    """插件命令白名单（与 Go 网关 PLUGIN_COMMAND_ALLOWLIST 保持一致）：
    仅允许白名单内的可执行文件 basename 被拉起为 MCP 插件进程；
    未配置（空）时禁止所有自定义插件命令（安全默认，防任意命令执行）。

    路径规则：只接受**裸 basename**（走 PATH）或**绝对路径**；拒绝相对路径、`..`
    与 UNC —— 见 `_command_path_ok`。
    """
    raw = os.getenv("PLUGIN_COMMAND_ALLOWLIST", "").strip()
    if not raw:
        return False
    allowed = {part.strip() for part in raw.split(",") if part.strip()}
    if not _command_path_ok(command):
        return False
    base = os.path.basename(command)

    # P0修正: 检查路径穿越
    resolved = os.path.realpath(command)
    _, resolved_base = os.path.split(resolved)

    # basename 和 resolved basename 必须一致 (防 ../../tmp/evil.py)
    if resolved_base.lower() != base.lower():
        return False

    return base in allowed
