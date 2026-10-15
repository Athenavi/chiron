#!/usr/bin/env python3
"""本机直跑的环境档位与自适应（多配置 + 参数选环境 + 缺失时自动检测）。

## 为什么需要它

同一个 `.env` 被**两种运行方式**共用：

* `docker compose` —— 里面 `postgres` / `sandbox-lb` 是**容器网络**的名字；
* `python run.py` —— **本机直跑**，那些名字解析不到。

于是本机启动时：网关拿 `postgresql://...@postgres:5432/...` 去连 ⇒ 解析失败 ⇒
fail-closed 拒绝启动；引擎拿 `http://sandbox-service:8001` 去探测 ⇒ 同样失败 ⇒ 拒绝启动。
（实测证据见 `logs/gateway.stdout.log`、`logs/python-engine.stderr.log`。）

## 三条原则（刻意为之）

1. **只改不可达的**：目标可达就**原样保留** —— 绝不覆盖一份本来能用的配置；
2. **只做等价替换**：容器名 → `127.0.0.1` 指向的是同一份数据的本地入口；
3. **可审计**：每处改动都返回一行中文说明，启动时打印；`--no-adapt` 可整体关掉。

不做危险推断：例如**不猜** Redis 密码 —— 只在"目标根本不要求认证"时才清空它。

## 两种用法

* **显式档位**：`run.py start --env local` ⇒ 叠加 `.env.local`（`.env*` 已被 .gitignore 忽略）；
* **自动检测**：不给 `--env` ⇒ 本模块探测当前环境，能直连就按本机语义改写。
"""

from __future__ import annotations

import socket
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

#: 容器网络里的名字 ⇒ 本机直跑时的等价入口。
#:
#: 只映射**本项目 compose 里真实出现过的服务名**（见 docker-compose.yml）。
#: 不写通配规则：映射表越短越容易核对。
CONTAINER_ALIASES: dict[str, str] = {
    "postgres": "127.0.0.1",
    "redis": "127.0.0.1",
    "sandbox-lb": "127.0.0.1",
    "sandbox-service": "127.0.0.1",
}

#: 本机直跑时，哪些 key 指向"可能需要改写的地址"。
_ADDRESS_KEYS = ("POSTGRES_DSN", "REDIS_URL", "SANDBOX_SERVICE_URL", "REDIS_ADDR", "DATABASE_URL")

_PROBE_TIMEOUT = 1.0

#: 未指定档位时**自动发现**的档位名（即 `.env.local`）。
#:
#: 这是 `.env.local` 的通用约定（Next.js / Vite 都自动加载它）—— 用户把它当作"本机覆盖"
#: 写下来，就不该再要求他额外记得传一次开关。
DEFAULT_PROFILE = "local"


def _tcp_reachable(host: str, port: int, timeout: float = _PROBE_TIMEOUT) -> bool:
    """能否在本机连上 host:port（判断"这个地址在当前环境通不通"）。"""
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _split_host_port(value: str) -> tuple[str, int | None]:
    """从 URL 或 `host:port` 里取出主机与端口。取不出返回 ("", None)。"""
    if not value:
        return "", None
    if "://" in value:
        try:
            parts = urlsplit(value)
        except ValueError:
            return "", None
        return parts.hostname or "", parts.port
    host, _, port = value.partition(":")
    if not host:
        return "", None
    try:
        return host, int(port) if port else None
    except ValueError:
        return host, None


def _redis_auth_required(host: str, port: int, timeout: float = _PROBE_TIMEOUT) -> bool | None:
    """`PING` 一下 Redis，判断目标**是否要求认证**。

    返回 True（要认证）/ False（不要求）/ None（连不上或答非所问）。
    `PING` 是只读命令，无副作用 —— 这是"不猜密码"的前提：只按目标自己的回答决定。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"PING\r\n")
            data = sock.recv(256)
    except OSError:
        return None
    if not data:
        return None
    head = data.split(b"\r\n", 1)[0].decode("utf-8", "replace")
    if head.startswith("+PONG"):
        return False
    if head.startswith("-NOAUTH") or head.startswith("-ERR Client sent AUTH"):
        return True
    return None


def _rewrite_host(value: str, new_host: str) -> str:
    """把 URL / `host:port` 里的主机名换成 new_host（保留其余部分）。"""
    if "://" in value:
        parts = urlsplit(value)
        userinfo = ""
        if parts.username:
            userinfo = parts.username
            if parts.password:
                userinfo += f":{parts.password}"
            userinfo += "@"
        netloc = f"{userinfo}{new_host}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    _, _, port = value.partition(":")
    return f"{new_host}:{port}" if port else new_host


def load_profile(base_dir: Path, profile: str) -> dict[str, str]:
    """读取 `.env.<profile>`（不存在则返回空 dict）。

    `_profile` 一律走 `name` 的**白名单字符**校验，防止 `.env.<name>` 里的 name 带
    路径分隔符而读到别处（`--env ../../etc/passwd` 这种）。
    """
    if not profile:
        return {}
    if not all(ch.isalnum() or ch in "-_" for ch in profile):
        raise ValueError(f"invalid profile name: {profile!r} (letters, digits, - and _ only)")
    path = base_dir / f".env.{profile}"
    if not path.exists():
        raise FileNotFoundError(f"profile file not found: {path.name}")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def detect_profile(env: dict[str, str]) -> str:
    """自动检测当前环境：能直连容器名 ⇒ `docker`，否则 ⇒ `local`。

    判据是**实测可达性**而不是任何标记文件 —— 标记可能过期，而"能不能连上"是当下的事实。
    """
    for key in ("POSTGRES_DSN", "REDIS_URL", "SANDBOX_SERVICE_URL"):
        value = env.get(key, "") or ""
        host, port = _split_host_port(value)
        if host == "127.0.0.1" or host == "localhost":
            return "local"
        if host and host not in CONTAINER_ALIASES:
            # 指向真实主机名/公网地址（生产托管库）⇒ 不当作本机环境改写。
            return "external"
        if host and port and _tcp_reachable(host, port):
            return "docker"
    return "local"


def adapt(env: dict[str, str]) -> list[str]:
    """把"在本机不可达的地址"改写成等价的本机入口；返回逐条说明。

    **就地修改** env（因为它就是要拿去注入子进程的那份），并返回说明列表。
    目标可达 ⇒ 一个字都不改。
    """
    notes: list[str] = []

    # ① 地址类：容器名在本机连不上 ⇒ 改写为 127.0.0.1（仅当本机确实有服务在听）。
    for key in _ADDRESS_KEYS:
        value = env.get(key, "") or ""
        if not value:
            continue
        host, port = _split_host_port(value)
        if not host or host not in CONTAINER_ALIASES:
            continue
        if port and _tcp_reachable(host, port):
            continue  # 通得了就保持原样
        local = CONTAINER_ALIASES[host]
        if port and not _tcp_reachable(local, port):
            notes.append(f"{key}: {host}:{port} 不可达，且本机 {local}:{port} 也没有服务在听 —— 未改写")
            continue
        env[key] = _rewrite_host(value, local)
        notes.append(f"{key}: {host} → {local}（容器名在本机不可达）")

    # ② Redis 密码：目标**不要求认证**而本地配了密码 ⇒ 清空（否则认证必失败）。
    #    只在探测明确回答"不要求认证"时才动手 —— 答不上来就保持原样。
    password = env.get("REDIS_PASSWORD", "") or ""
    if password:
        url = env.get("REDIS_URL", "") or ""
        host, port = _split_host_port(url) if url else ("", None)
        if host and port is None:
            port = 6379
        if host:
            needs = _redis_auth_required(host, port or 6379)
            if needs is False:
                env["REDIS_PASSWORD"] = ""
                notes.append("REDIS_PASSWORD: 目标 Redis 未启用认证 → 已清空（否则 AUTH 必被拒）")

    # ③ sandbox：后端是 service 且服务不可达 ⇒ 单机开发退回进程内隔离。
    backend = (env.get("SANDBOX_BACKEND", "") or "service").lower()
    if backend == "service":
        url = env.get("SANDBOX_SERVICE_URL", "") or "http://sandbox-service:8001"
        host, port = _split_host_port(url)
        if host and port and not _tcp_reachable(host, port):
            env["SANDBOX_BACKEND"] = "local"
            notes.append(
                "SANDBOX_BACKEND: service 不可达 → local（仅限单机开发；多副本部署必须修好 sandbox 服务）"
            )

    return notes


def apply_profile(
    base_dir: Path,
    env: dict[str, str],
    *,
    profile: str | None = None,
    adapt_auto: bool = True,
) -> list[str]:
    """把档位与自适应应用到 env（供 `run.py` 调用）。返回逐条说明。

    * 给了 `profile` ⇒ 叠加 `.env.<profile>`（**显式档位优先**，不再自动改写地址）；
    * 没给 ⇒ 自动检测（`detect_profile`），本机环境才做改写。
    """
    notes: list[str] = []

    # 档位来源：显式指定的优先；否则**自动发现 `.env.local`**。没有它才退回
    # "自动检测 + 只改写地址"。
    #
    # 为什么必须自动发现：只做地址改写是**不够的** —— 它能修容器主机名，却修不了凭据。
    # 实测就栽在这：`postgres` 已被改写成 `127.0.0.1`，但用户名仍是 .env 里的 chiron_app，
    # 而本机只有 postgres 角色 ⇒ `password authentication failed for user "chiron_app"`，
    # 网关照样 fail-closed 拒绝启动（见 logs/gateway.stdout.log）。
    chosen = profile
    if not chosen and (base_dir / f".env.{DEFAULT_PROFILE}").exists():
        chosen = DEFAULT_PROFILE
    if chosen:
        try:
            overrides = load_profile(base_dir, chosen)
        except (FileNotFoundError, ValueError) as exc:
            raise RuntimeError(f"profile {chosen}: {exc}") from exc
        # **整份应用**（含显式空值）：档位文件里写出来的键就是用户的明确意图，而
        # "清空某个变量"（`REDIS_PASSWORD=`）只有这样才能表达 —— 过滤空值会让它静默失效。
        env.update(overrides)
        notes.append(f"已加载档位 .env.{chosen}（覆盖 {len(overrides)} 项）")
        return notes

    if not adapt_auto:
        return notes
    detected = detect_profile(env)
    if detected == "external":
        return notes  # 指向托管服务 ⇒ 什么都不动
    notes.extend(adapt(env))
    if notes:
        notes.insert(0, f"未指定档位，自动检测为 {detected} 环境并自适应")
    return notes


def _main() -> int:
    """`python -m local_env_profile` —— 走一遍**真实**的档位/自适应逻辑，但不启动任何服务。

    排查用：看到什么结果，启动时就按什么结果走（两者共用 `apply_profile`）。
    """
    import json
    import re

    base = Path(__file__).parent.resolve()
    env: dict[str, str] = {}
    env_file = base / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    before = dict(env)
    notes = apply_profile(base, env)
    print("profile / adaptation:")
    for note in notes or ["(none — 没有档位文件，且 .env 里的地址在本机都可达)"]:
        print(f"  - {note}")
    changed = {k: env[k] for k in env if env.get(k) != before.get(k)}
    # 打印前**必须脱敏**：changed 里可能带 POSTGRES_DSN（URL 里含密码）。
    safe = {
        k: (re.sub(r"://[^@/]*@", "://***@", v) if isinstance(v, str) and "://" in v else v)
        for k, v in changed.items()
    }
    print("resulting values (secrets hidden):", json.dumps(safe, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
