#!/usr/bin/env python3
"""Chiron 初始化助手（scripts/init.py）。

Chiron 没有安装向导：所有部署参数都在 .env / 环境变量里，数据库 schema 由迁移负责，
系统管理员是「第一个通过 /register 注册的账号」。本脚本把这些手工步骤收拢成一条命令：

  1) 生成部署级主密钥 APP_SECRET（可选一并生成 JWT_SECRET / INTERNAL_TOKEN）
  2) 配置 PostgreSQL / Redis 连接串，并做 TCP / 驱动连通性自检
  3) 安全写回 .env：保留其它配置项与注释，写前自动备份
  4) 可选执行数据库迁移（alembic upgrade head）

用法::

    python scripts/init.py                          # 交互式向导
    python scripts/init.py --check                  # 只做自检（排障 / CI）
    python scripts/init.py --set POSTGRES_DSN=postgresql://user:pwd@localhost:5432/chiron
    python scripts/init.py --set REDIS_ADDR=localhost:6379 --migrate
    python scripts/init.py --env-file ./deploy/.env --check
    python scripts/init.py --rotate-app-secret      # 轮换主密钥（危险，见向导提示）

注意：APP_SECRET 用于加密后台「系统设置」中的敏感项（LLM/S3/支付密钥、redis/pg 密码）。
轮换它会令既有密文无法解密，只能回到后台重新录入。
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
MIN_SECRET_LEN = 32

KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


# ───────────────────────────── 输出 ─────────────────────────────

def info(msg: str) -> None:
    print(msg)


def ok(msg: str) -> None:
    print(f"[ OK ] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ─────────────────────────── .env 读写 ───────────────────────────

def read_env_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def parse_env(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        m = KEY_RE.match(line)
        if not m:
            continue
        raw = m.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        out[m.group(1)] = raw
    return out


def format_value(value: str) -> str:
    if value == "":
        return ""
    if re.search(r"[\s#]", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def upsert(lines: list[str], key: str, value: str) -> list[str]:
    """替换首个出现的 KEY= 行（丢弃重复键），不存在则追加到末尾。"""
    new_line = f"{key}={format_value(value)}"
    out: list[str] = []
    seen = False
    for line in lines:
        m = KEY_RE.match(line)
        if m and m.group(1) == key:
            if not seen:
                out.append(new_line)
                seen = True
            continue
        out.append(line)
    if not seen:
        # 仅当上一行是注释时才留一个空行；连续新增的键保持紧凑。
        if out and out[-1].lstrip().startswith("#"):
            out.append("")
        out.append(new_line)
    return out


def write_env(path: Path, lines: list[str], dry_run: bool = False) -> bool:
    body = "\n".join(lines).rstrip("\n") + "\n"
    if dry_run:
        info(f"--- {path} (dry-run，未写入) ---")
        info(body)
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.parent / f"{path.name}.bak.{stamp}"
        shutil.copy2(path, backup)
        info(f"已备份原文件 -> {backup.name}")
    path.write_text(body, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return "(未设置)"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}（{len(value)} 字符）"


def mask_dsn(dsn: str) -> str:
    """隐藏连接串密码。"""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "(无法解析)"
    if not parts.password:
        return dsn
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
    return parts._replace(netloc=netloc).geturl()


# ─────────────────────────── 连通性自检 ───────────────────────────

def tcp_check(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, str(exc)


def dsn_target(dsn: str) -> tuple[str | None, int, str]:
    parts = urlsplit(dsn)
    host = parts.hostname
    port = parts.port or 5432
    return host, port, (parts.path or "").lstrip("/")


def check_postgres(dsn: str) -> bool:
    if not dsn:
        fail("POSTGRES_DSN 未配置")
        return False
    parts = urlsplit(dsn)
    if parts.scheme not in ("postgres", "postgresql"):
        fail("POSTGRES_DSN 必须以 postgresql:// 开头")
        return False
    host, port, database = dsn_target(dsn)
    if not host:
        fail("POSTGRES_DSN 缺少主机名")
        return False
    if not database:
        warn("POSTGRES_DSN 未指定数据库名（路径部分）")
    reachable, why = tcp_check(host, port)
    if not reachable:
        fail(f"PostgreSQL {host}:{port} 不可达：{why}")
        return False
    ok(f"PostgreSQL {host}:{port} TCP 可达（database={database or '?'}）")
    driver = _try_postgres_driver(dsn)
    if driver is None:
        info("      未安装 psycopg/psycopg2，跳过账号密码校验"
             "（可 python -m pip install -r requirements-migrate.txt）")
    elif driver is False:
        fail("账号/密码或库名校验失败（见上方驱动报错）")
        return False
    else:
        ok("PostgreSQL 账号密码校验通过")
    return True


def _try_postgres_driver(dsn: str) -> bool | None:
    """返回 True=连上, False=连接失败, None=没有可用驱动。"""
    for module in ("psycopg", "psycopg2"):
        try:
            mod = __import__(module)
        except ImportError:
            continue
        try:
            conn = mod.connect(dsn)
            conn.close()
            return True
        except Exception as exc:  # noqa: BLE001 - 驱动异常类型随实现而异
            info(f"      {module} 报错：{exc}")
            return False
    return None


def check_redis(env: dict[str, str]) -> bool:
    addr = env.get("REDIS_ADDR") or ""
    url = env.get("REDIS_URL") or ""
    if not addr and url:
        parts = urlsplit(url)
        addr = f"{parts.hostname}:{parts.port or 6379}"
    if not addr:
        warn("Redis 未配置（REDIS_ADDR / REDIS_URL 均未设置，将按默认 localhost:6379 启动）")
        return True
    host, _, port_s = addr.rpartition(":")
    port = int(port_s) if port_s.isdigit() else 6379
    reachable, why = tcp_check(host or "localhost", port)
    if not reachable:
        fail(f"Redis {host or 'localhost'}:{port} 不可达：{why}")
        return False
    ok(f"Redis {host or 'localhost'}:{port} TCP 可达")
    return True


# ─────────────────────────── 数据库迁移 ───────────────────────────

def run_migrations(dsn: str, env_path: Path = DEFAULT_ENV_PATH) -> bool:
    env = os.environ.copy()
    env["DATABASE_DSN"] = dsn
    if not env.get("APP_SECRET"):
        file_env = parse_env(read_env_lines(env_path))
        if file_env.get("APP_SECRET"):
            env["APP_SECRET"] = file_env["APP_SECRET"]
    cmd = [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"]
    info("执行：" + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
    except FileNotFoundError as exc:
        fail(f"无法执行迁移：{exc}")
        return False
    if proc.returncode == 0:
        ok("数据库迁移完成（alembic upgrade head）")
        return True
    fail("数据库迁移失败。若缺少依赖请先执行："
         "python -m pip install -r requirements-migrate.txt")
    return False


def build_dsn(host: str, port: str, user: str, password: str, database: str) -> str:
    cred = quote(user, safe="")
    if password:
        cred += ":" + quote(password, safe="")
    return f"postgresql://{cred}@{host}:{port}/{database}"


def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{text}{suffix}: ").strip()
    return value or default


def prompt_password(text: str) -> str:
    try:
        return getpass.getpass(text + ": ")
    except (EOFError, getpass.GetPassWarning):
        return input(text + ": ")


# ─────────────────────────── 向导 ───────────────────────────

def show_status(env: dict[str, str]) -> None:
    banner("当前状态")
    info(f"项目根目录：{PROJECT_ROOT}")
    info(f".env 路径  ：{DEFAULT_ENV_PATH}"
         f"{'' if DEFAULT_ENV_PATH.exists() else '（不存在，将在保存时创建）'}")
    info(f"APP_SECRET ：{mask(env.get('APP_SECRET', ''))}")
    info(f"JWT_SECRET ：{mask(env.get('JWT_SECRET', ''))}")
    info(f"INTERNAL_TOKEN：{mask(env.get('INTERNAL_TOKEN', ''))}")
    info(f"POSTGRES_DSN：{mask_dsn(env['POSTGRES_DSN']) if env.get('POSTGRES_DSN') else '(未设置)'}")
    info(f"REDIS_ADDR ：{env.get('REDIS_ADDR', '(未设置)')}")
    info(f"CHIRON_DATA_DIR：{env.get('CHIRON_DATA_DIR', '(默认)')}")


def wizard(env_path: Path) -> int:
    lines = read_env_lines(env_path)
    changed = False

    while True:
        env = parse_env(lines)
        show_status(env)
        banner("操作菜单")
        info("  1) 生成/补齐密钥（APP_SECRET、JWT_SECRET、INTERNAL_TOKEN）")
        info("  2) 配置 PostgreSQL 连接（POSTGRES_DSN）")
        info("  3) 配置 Redis 连接（REDIS_ADDR / REDIS_PASSWORD）")
        info("  4) 设置数据目录（CHIRON_DATA_DIR）")
        info("  5) 依赖连通性自检")
        info("  6) 执行数据库迁移（alembic upgrade head）")
        info("  s) 保存并退出      q) 放弃修改退出")
        choice = input("请选择: ").strip().lower()

        if choice in ("s", ""):
            if not changed:
                info("没有需要保存的修改。")
                return 0
            if not _validate_before_save(parse_env(lines)):
                if input("仍要保存？(y/N): ").strip().lower() != "y":
                    continue
            write_env(env_path, lines)
            ok(f"已写入 {env_path}")
            _next_steps()
            return 0

        if choice == "q":
            info("已放弃修改。")
            return 1

        if choice == "1":
            lines, changed = _step_keys(lines, changed)
        elif choice == "2":
            lines, changed = _step_postgres(lines, changed)
        elif choice == "3":
            lines, changed = _step_redis(lines, changed)
        elif choice == "4":
            path = prompt("数据目录（留空=使用默认）", parse_env(lines).get("CHIRON_DATA_DIR", ""))
            if path:
                lines = upsert(lines, "CHIRON_DATA_DIR", str(Path(path).expanduser()))
                changed = True
        elif choice == "5":
            env = parse_env(lines)
            banner("连通性自检")
            check_postgres(env.get("POSTGRES_DSN", ""))
            check_redis(env)
        elif choice == "6":
            dsn = parse_env(lines).get("POSTGRES_DSN", "")
            if not dsn:
                fail("请先配置 POSTGRES_DSN。")
            else:
                if not check_postgres(dsn):
                    warn("数据库自检未通过，迁移可能失败。")
                run_migrations(dsn, env_path)
        else:
            warn(f"无效选项：{choice}")


def _step_keys(lines: list[str], changed: bool) -> tuple[list[str], bool]:
    env = parse_env(lines)
    rotate = env.get("APP_SECRET", "") != "" and input(
        "APP_SECRET 已存在，是否轮换（会令已加密的后台敏感配置无法解密）？(y/N): "
    ).strip().lower() == "y"

    pairs = [("APP_SECRET", 32), ("JWT_SECRET", 32), ("INTERNAL_TOKEN", 32)]
    for key, nbytes in pairs:
        existing = env.get(key, "")
        if existing and not rotate:
            info(f"{key} 已设置，保留（{mask(existing)}）")
            continue
        lines = upsert(lines, key, secrets.token_urlsafe(nbytes))
        changed = True
        ok(f"{key} 已生成")
    if rotate:
        warn("APP_SECRET 已轮换：请回后台「系统设置」重新录入 LLM/S3/支付等敏感配置。")
    info("提示：未显式设置时，JWT_SECRET / INTERNAL_TOKEN 会由 APP_SECRET 派生，"
         "但多副本部署建议显式固定。")
    return lines, changed


def _step_postgres(lines: list[str], changed: bool) -> tuple[list[str], bool]:
    env = parse_env(lines)
    current = env.get("POSTGRES_DSN", "")
    if current:
        info(f"当前：{mask_dsn(current)}")
    info("直接粘贴完整 DSN（留空=按字段逐项录入）")
    dsn = input("POSTGRES_DSN: ").strip()
    if not dsn:
        parts = urlsplit(current) if current else None
        default_user = (parts.username or "postgres") if parts else "postgres"
        host = prompt("主机", parts.hostname if parts and parts.hostname else "localhost")
        port = prompt("端口", str(parts.port) if parts and parts.port else "5432")
        user = prompt("用户名", default_user)
        database = prompt("数据库名", (parts.path.lstrip("/") if parts else "") or "chiron")
        password = prompt_password("密码（输入不回显）")
        dsn = build_dsn(host, port, user, password, database)
    else:
        parts = urlsplit(dsn)
        if parts.scheme not in ("postgres", "postgresql"):
            fail("DSN 必须以 postgresql:// 开头")
            return lines, changed
        if parts.password and not _password_is_encoded(parts.password):
            warn("密码含特殊字符时必须是 URL 编码形式（如 @ -> %40），否则连接会解析失败。")
    if not check_postgres(dsn):
        if input("自检未通过，仍要保存？(y/N): ").strip().lower() != "y":
            return lines, changed
    lines = upsert(lines, "POSTGRES_DSN", dsn)
    ok("POSTGRES_DSN 已就绪")
    return lines, True


def _password_is_encoded(password: str) -> bool:
    return not re.search(r"[/@:?#\s]", password)


def _step_redis(lines: list[str], changed: bool) -> tuple[list[str], bool]:
    env = parse_env(lines)
    addr = prompt("Redis 地址 host:port", env.get("REDIS_ADDR", "localhost:6379"))
    lines = upsert(lines, "REDIS_ADDR", addr)
    password = prompt_password("Redis 密码（无密码直接回车）")
    if password:
        lines = upsert(lines, "REDIS_PASSWORD", password)
    check_redis(parse_env(lines))
    return lines, True


def _validate_before_save(env: dict[str, str]) -> bool:
    problems = []
    secret = env.get("APP_SECRET", "")
    if len(secret) < MIN_SECRET_LEN:
        problems.append(f"APP_SECRET 缺失或短于 {MIN_SECRET_LEN} 字符（网关会拒绝启动）")
    if not env.get("POSTGRES_DSN"):
        problems.append("POSTGRES_DSN 未设置（网关会拒绝启动）")
    if problems:
        for p in problems:
            fail(p)
        return False
    ok("必填项检查通过（APP_SECRET / POSTGRES_DSN）")
    return True


def _next_steps() -> None:
    banner("下一步")
    info("1) 执行数据库迁移（若尚未执行）：")
    info("     python -m pip install -r requirements-migrate.txt")
    info("     python -m alembic upgrade head")
    info("2) 启动服务：python run.py start   （或 docker compose up -d）")
    info("3) 打开前端到 /register 注册第一个账号 —— 它会自动成为系统管理员（owner）。")
    info("4) 裸机 / 本地开发：请在 .env 设 SANDBOX_BACKEND=local —— 引擎默认把工具执行")
    info("   下沉到独立受限容器（docker compose 的 sandbox-service），本地没有该容器时")
    info("   启动探测失败会**拒绝启动**（fail-closed）；或设 DEGRADED_MODE=true 允许回退。")


# ─────────────────────────── CLI ───────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Chiron 初始化助手：生成主密钥、配置 .env、自检依赖、执行迁移。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_PATH),
                        help="要读写的 .env 路径（默认：项目根 .env）")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="非交互写入/覆盖某个配置项，可重复")
    parser.add_argument("--check", action="store_true",
                        help="只做自检（必填项 + PostgreSQL/Redis 连通性），不写文件")
    parser.add_argument("--migrate", action="store_true",
                        help="写完后执行 alembic upgrade head")
    parser.add_argument("--rotate-app-secret", action="store_true",
                        help="强制轮换 APP_SECRET（危险：既有加密配置将无法解密）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容")
    parser.add_argument("--yes", action="store_true", help="跳过确认提示")
    args = parser.parse_args(argv)

    env_path = Path(args.env_file).expanduser()
    lines = read_env_lines(env_path)
    server_mode = args.check or args.set or args.rotate_app_secret

    if not server_mode:
        if not sys.stdin.isatty() and not args.yes:
            fail("非交互环境：请使用 --check / --set KEY=VALUE（或 --yes 继续向导）。")
            return 2
        return wizard(env_path)

    changed = False
    if args.rotate_app_secret:
        if not args.yes:
            warn("轮换 APP_SECRET 后，后台「系统设置」里已加密的敏感项必须重新录入。")
            if input("确认轮换？(y/N): ").strip().lower() != "y":
                return 1
        for key in ("APP_SECRET", "JWT_SECRET", "INTERNAL_TOKEN"):
            lines = upsert(lines, key, secrets.token_urlsafe(32))
            ok(f"{key} 已轮换")
        changed = True

    for item in args.set:
        if "=" not in item:
            fail(f"--set 需要 KEY=VALUE 形式：{item}")
            return 2
        key, value = item.split("=", 1)
        keys = parse_env(lines)
        if key in ("APP_SECRET", "JWT_SECRET", "INTERNAL_TOKEN") and key in keys and not args.yes:
            warn(f"{key} 已存在值，将被覆盖（会话/密文可能失效）。")
            if input("继续？(y/N): ").strip().lower() != "y":
                continue
        lines = upsert(lines, key.strip(), value.strip())
        ok(f"{key} 已更新")
        changed = True

    # 缺失密钥自动补齐（非 --check 场景）
    if not args.check:
        env = parse_env(lines)
        for key in ("APP_SECRET", "JWT_SECRET", "INTERNAL_TOKEN"):
            if not env.get(key):
                lines = upsert(lines, key, secrets.token_urlsafe(32))
                ok(f"{key} 已生成")
                changed = True

    env = parse_env(lines)
    banner("自检")
    env_ok = _validate_before_save(env)
    env_ok = check_postgres(env.get("POSTGRES_DSN", "")) and env_ok
    check_redis(env)

    if args.check:
        return 0 if env_ok else 1

    if changed:
        write_env(env_path, lines, dry_run=args.dry_run)
        if args.dry_run:
            info("（--dry-run：未真正写入）")
        else:
            ok(f"已写入 {env_path}")

    status = 0
    if args.migrate:
        if not check_postgres(env.get("POSTGRES_DSN", "")):
            fail("数据库不可达，跳过迁移。")
            return 1
        status = 0 if run_migrations(env.get("POSTGRES_DSN", ""), env_path) else 1

    if not env_ok:
        status = status or 1
    _next_steps()
    return status


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        fail("已中断。")
        sys.exit(130)
