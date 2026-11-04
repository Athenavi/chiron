#!/usr/bin/env python3
"""Chiron 一键体验栈（scripts/demo_up.py）。

把「配 8 个必填变量 + 手动跑迁移 + 构建 3 个镜像」收成一条命令：

    python scripts/demo_up.py

它做的事：

  1) 生成 `.env.demo`（随机 APP_SECRET / PostgreSQL 与 Redis 密码，权限 0600）
  2) `docker compose -f docker-compose.demo.yml --env-file .env.demo up -d --build`
  3) 等各服务健康后打印访问地址

密钥**必须随机**而不是写死：本栈把 3000 端口发布到宿主机。若 APP_SECRET 是仓库里
公开的常量，任何能访问该端口的人都能用它签发任意租户的 JWT —— 而把 demo 跑在有
公网 IP 的机器上是常态。

停止 / 彻底清空（含数据库卷）：

    python scripts/demo_up.py --down
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.demo.yml"
ENV_FILE = PROJECT_ROOT / ".env.demo"
DEFAULT_PORT = 3000


def info(msg: str) -> None:
    print(msg)


def ok(msg: str) -> None:
    print(f"[OK] {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)


def banner(title: str) -> None:
    print()
    print("=" * 62)
    print(f"  {title}")
    print("=" * 62)


# ───────────────────────────── Docker 探测 ─────────────────────────────

def compose_command() -> list[str] | None:
    """返回可用的 compose 调用方式（v2 插件优先，其次独立 v1 二进制）。"""
    docker = shutil.which("docker")
    if docker:
        probe = subprocess.run(
            [docker, "compose", "version"],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode == 0:
            return [docker, "compose"]
    legacy = shutil.which("docker-compose")
    if legacy:
        return [legacy]
    return None


# ───────────────────────────── .env.demo ─────────────────────────────

def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    lines = [
        "# 由 scripts/demo_up.py 生成 —— 本文件含随机密钥，已在 .gitignore 内。",
        "# 不要提交；`python scripts/demo_up.py --down` 后可连同数据卷一起丢弃。",
        "",
    ]
    lines += [f"{k}={v}" for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        # 含密钥，尽量收紧权限（Windows 上 chmod 语义有限，失败不影响使用）
        path.chmod(0o600)
    except OSError:
        pass


def ensure_env(port: int, reuse: bool) -> dict[str, str]:
    """生成或复用 `.env.demo`。已有密钥一律保留 —— 换掉会让既有密文无法解密。"""
    existing = read_env(ENV_FILE)

    def keep_or_new(key: str, factory) -> str:
        if reuse and existing.get(key):
            return existing[key]
        return factory()

    postgres_password = keep_or_new("POSTGRES_PASSWORD", lambda: secrets.token_urlsafe(24))
    values = {
        # ≥32 字符且非弱值（Go/Python 两侧都有弱值黑名单，示例值会被拒绝启动）
        "APP_SECRET": keep_or_new("APP_SECRET", lambda: secrets.token_urlsafe(32)),
        "POSTGRES_USER": existing.get("POSTGRES_USER") or "chiron",
        "POSTGRES_DB": existing.get("POSTGRES_DB") or "chiron",
        "POSTGRES_PASSWORD": postgres_password,
        "REDIS_PASSWORD": keep_or_new("REDIS_PASSWORD", lambda: secrets.token_urlsafe(24)),
        # 容器内地址（compose 网络内的服务名），不是宿主机地址
        "POSTGRES_DSN": (
            f"postgresql://{existing.get('POSTGRES_USER') or 'chiron'}:"
            f"{postgres_password}@postgres:5432/{existing.get('POSTGRES_DB') or 'chiron'}"
        ),
        "DEMO_HTTP_PORT": str(port),
    }
    # JWT_SECRET / INTERNAL_TOKEN 刻意留空：由 APP_SECRET 派生，Go 与 Python 算法一致。
    # 显式留空而不是不写，是为了让人一眼看出"这里故意没配"。
    values["JWT_SECRET"] = existing.get("JWT_SECRET", "")
    values["INTERNAL_TOKEN"] = existing.get("INTERNAL_TOKEN", "")
    # 模型密钥从当前 shell 透传（可为空：能启动、能看界面，配了才能真跑对话）
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "LLM_API_KEY", "GOPROXY"):
        if os.environ.get(key):
            values[key] = os.environ[key]

    write_env(ENV_FILE, values)
    return values


# ───────────────────────────── 子命令 ─────────────────────────────

def run_up(port: int, build: bool, wait: bool, reuse_env: bool) -> int:
    compose = compose_command()
    if compose is None:
        fail("找不到 docker compose。请先安装 Docker Desktop / Docker Engine + compose 插件。")
        return 1
    if not COMPOSE_FILE.exists():
        fail(f"找不到 {COMPOSE_FILE}")
        return 1

    values = ensure_env(port, reuse=reuse_env)
    ok(f"已写入 {ENV_FILE.name}（随机密钥，权限 0600）")

    cmd = [*compose, "-f", str(COMPOSE_FILE), "--env-file", str(ENV_FILE), "up", "-d"]
    if build:
        cmd.append("--build")
    if wait:
        cmd += ["--wait", "--wait-timeout", "600"]

    banner("构建并启动（首次需拉取依赖，可能数分钟）")
    info("  " + " ".join(cmd))
    print()
    code = subprocess.run(cmd, cwd=PROJECT_ROOT, check=False).returncode
    if code != 0:
        fail("启动失败。排障：")
        info(f"  {' '.join(compose)} -f {COMPOSE_FILE} --env-file {ENV_FILE} logs --tail=80")
        return code

    banner("已启动")
    info(f"  打开浏览器：http://localhost:{port}")
    info("  注册的第一个账号自动成为系统管理员（owner）。")
    info("")
    info("  想真的对话，需要先配模型密钥 —— 二选一：")
    info("    * 环境变量传入后重跑本脚本：")
    info("        ANTHROPIC_API_KEY=sk-... python scripts/demo_up.py")
    info("    * 或登录后在「系统设置 → 服务提供商」里填。")
    info("")
    info("  停止（保留数据）：  docker compose -f docker-compose.demo.yml --env-file .env.demo down")
    info("  彻底清空（删数据卷）：python scripts/demo_up.py --down")
    return 0


def run_down() -> int:
    compose = compose_command()
    if compose is None:
        fail("找不到 docker compose。")
        return 1
    if not ENV_FILE.exists():
        fail(f"找不到 {ENV_FILE.name}，说明本机还没跑过 demo。")
        return 1
    cmd = [*compose, "-f", str(COMPOSE_FILE), "--env-file", str(ENV_FILE),
           "down", "-v", "--remove-orphans"]
    info("  " + " ".join(cmd))
    code = subprocess.run(cmd, cwd=PROJECT_ROOT, check=False).returncode
    if code == 0:
        ok("已停止并删除数据卷（数据库内容已丢弃）")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Chiron 一键体验栈：起 PostgreSQL + Redis + 自动迁移 + 网关/引擎/前端",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"对外 HTTP 端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--no-build", action="store_true",
                        help="跳过镜像构建，直接用已有镜像")
    parser.add_argument("--no-wait", action="store_true",
                        help="不等待服务健康（后台起完即返回）")
    parser.add_argument("--fresh-secrets", action="store_true",
                        help="重新生成密钥（会让按旧 APP_SECRET 加密的后台配置失效）")
    parser.add_argument("--prepare-only", action="store_true",
                        help="只生成 .env.demo，不启动容器（用于自查/CI）")
    parser.add_argument("--down", action="store_true",
                        help="停止并删除全部容器与数据卷")
    args = parser.parse_args()

    if args.down:
        return run_down()

    if args.prepare_only:
        ensure_env(args.port, reuse=not args.fresh_secrets)
        ok(f"已写入 {ENV_FILE}")
        return 0

    return run_up(
        port=args.port,
        build=not args.no_build,
        wait=not args.no_wait,
        reuse_env=not args.fresh_secrets,
    )


if __name__ == "__main__":
    sys.exit(main())
