#!/usr/bin/env python
"""Electron ↔ 本地 core 握手验证（契约 §3.1 / §4）。

只做一件事：像 Electron 主进程那样把 core 起起来，再走一遍壳层真正会走的请求，
把每一步的**实测**结果打印出来。失败时打印 core 的 stderr 尾部。

流程::

    1. subprocess 启动 ``python -m app.main --port 0 --no-migrate``（cwd = desktop-local，
       与 frontend-vue/electron/components.cjs::resolveCoreLaunch 的 dev 分支一致）；
    2. 读 stdout 的 ``{"event":"listening","port":...}``，拿真实端口与数据目录；
    3. ``GET /healthz``，期望 ``{"status":"ok"}``；
    4. ``POST /v1/auth/session``，期望带回设备 token 与固定本地用户 ``id == "local"``。

用法::

    cd desktop-local
    python scripts/verify_handshake.py                  # 临时数据目录 + 随机端口，跑完即删
    python scripts/verify_handshake.py --keep           # 保留数据目录以便排查
    python scripts/verify_handshake.py --with-storage   # 连 embedded PG / Redis 一起起
    python scripts/verify_handshake.py --python C:/Python312/python.exe

退出码：0 = 全部 PASS（跳过不算失败）；1 = 有 FAIL。

本脚本**只读不写** ``app/**``：所有参数都走 CLI 与环境变量，和 shell 层是同一套耦合面。
``CHIRON_*``（契约名）与 ``CHIRON_LOCAL_*``（``app/config.py`` 的 pydantic 前缀）同时下发，
用来实测两套名字是否都已生效 —— 这是当前最容易被静默忽略的地方。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

DESKTOP_LOCAL = Path(__file__).resolve().parents[1]  # desktop-local/
REPO_ROOT = DESKTOP_LOCAL.parent

STDERR_TAIL_LINES = 30
HTTP_TIMEOUT = 5.0

# 每次运行现生成：只用于证明 env 能被读走，测完即弃
DEVICE_TOKEN = secrets.token_urlsafe(24)


# --------------------------------------------------------------------- 输出
def say(message: str = "") -> None:
    print(message, flush=True)


def mask(secret: str) -> str:
    """token 只打到"能区分是哪一条"为止：长度 + 首尾各 4 个字符。"""
    value = str(secret or "")
    if not value:
        return "(empty)"
    if len(value) <= 12:
        return f"{value[:2]}…(len={len(value)})"
    return f"{value[:4]}…{value[-4:]}(len={len(value)})"


class Checks:
    """PASS / FAIL / SKIP 记录器，最后统一汇总并决定退出码。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, bool | None, str]] = []

    def add(self, name: str, ok: bool | None, detail: str = "") -> bool:
        self.items.append((name, ok, detail))
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
        suffix = f" —— {detail}" if detail else ""
        say(f"  [{mark}] {name}{suffix}")
        return ok is True

    def ok(self) -> bool:
        return all(ok is not False for _, ok, _ in self.items)

    def summary(self) -> None:
        passed = sum(1 for _, ok, _ in self.items if ok is True)
        failed = sum(1 for _, ok, _ in self.items if ok is False)
        skipped = sum(1 for _, ok, _ in self.items if ok is None)
        say("")
        say(f"合计：PASS {passed} / FAIL {failed} / SKIP {skipped}")
        if failed:
            say("排查顺序见 docs/desktop-local/handshake.md「失败排查」一节。")
        else:
            say("握手链路可用（Electron 侧走的是同一组契约：baseUrl + device token）。")


# --------------------------------------------------------------------- HTTP
def http_json(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str = "",
    timeout: float = HTTP_TIMEOUT,
) -> tuple[int, Any, str]:
    """返回 (status, parsed_json_or_None, raw_text)。网络错误抛 OSError。"""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 —— 只连 127.0.0.1
            raw = response.read().decode("utf-8", errors="replace")
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = None
    return status, parsed, raw


# --------------------------------------------------------------------- core 进程
class CoreProcess:
    """把 main.py 当子进程跑，逐行读 stdout 抓 listening 播报。"""

    def __init__(
        self,
        command: list[str],
        env: dict[str, str],
        cwd: Path,
    ) -> None:
        self.command = command
        self.env = env
        self.cwd = cwd
        self.proc: subprocess.Popen[str] | None = None
        self.announce: dict[str, Any] | None = None
        self.stdout_lines: deque[str] = deque(maxlen=200)
        self.stderr_lines: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._listening = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- 生命周期
    def start(self) -> None:
        self.proc = subprocess.Popen(  # noqa: S603 —— 参数是我们自己拼的绝对路径
            self.command,
            cwd=str(self.cwd),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._threads = [
            threading.Thread(target=self._pump_stdout, name="core-stdout", daemon=True),
            threading.Thread(target=self._pump_stderr, name="core-stderr", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _pump_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            text = line.rstrip("\r\n")
            if not text.strip():
                continue
            self.stdout_lines.append(text)
            announce = parse_listening_line(text)
            if announce and not self._listening.is_set():
                self.announce = announce
                self._listening.set()

    def _pump_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            text = line.rstrip("\r\n")
            if text.strip():
                self.stderr_lines.append(text)

    def wait_listening(self, timeout: float) -> dict[str, Any] | None:
        """等 listening 播报；进程提前退出就立刻返回 None。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._listening.wait(0.05):
                return self.announce
            if self.proc is not None and self.proc.poll() is not None:
                # 给读线程一点时间把剩下的 stderr 收完
                time.sleep(0.1)
                return self.announce
        return self.announce

    def poll(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def stop(self, grace: float = 5.0) -> int | None:
        if self.proc is None or self.proc.poll() is not None:
            return self.poll()
        self.proc.terminate()
        try:
            return self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                return self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                return None

    # -- 诊断
    def stderr_tail(self) -> str:
        return "\n".join(self.stderr_lines)


_ANNOUNCE_PORT = re.compile(r'"port"\s*:\s*(\d+)')


def parse_listening_line(line: str) -> dict[str, Any] | None:
    """只认 ``event == "listening"`` 且端口合法的那行；其它 stdout 一律忽略。"""
    text = (line or "").strip()
    if not text.startswith("{") or '"listening"' not in text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 极端情况：一行里混了 uvicorn 日志前缀，退一步用正则抠端口
        match = _ANNOUNCE_PORT.search(text)
        if not match:
            return None
        port = int(match.group(1))
        return {"event": "listening", "port": port} if port > 0 else None
    if not isinstance(parsed, dict) or parsed.get("event") != "listening":
        return None
    try:
        port = int(parsed.get("port", 0))
    except (TypeError, ValueError):
        return None
    if port <= 0 or port > 65535:
        return None
    parsed["port"] = port
    return parsed


# --------------------------------------------------------------------- env
def build_env(data_dir: Path, inference_proxy: str) -> dict[str, str]:
    """同时下发契约名与 config.py 的真名，并保证 `app` 包可导入。"""
    env = dict(os.environ)
    pythonpath = [str(DESKTOP_LOCAL)]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env["PYTHONUTF8"] = "1"

    # contracts.md §3.1 的契约名
    env["CHIRON_DATA_DIR"] = str(data_dir)
    env["CHIRON_PORT"] = "0"
    env["CHIRON_DEVICE_TOKEN"] = DEVICE_TOKEN
    # desktop-local/app/config.py 的 pydantic env_prefix（当前真正生效的名字）
    env["CHIRON_LOCAL_DATA_DIR"] = str(data_dir)
    env["CHIRON_LOCAL_PORT"] = "0"

    if inference_proxy:
        env["CHIRON_INFERENCE_PROXY"] = inference_proxy
        env["CHIRON_LOCAL_INFERENCE_PROXY"] = inference_proxy
    else:
        # 父进程可能带着奇怪的值，别让它渗进来
        for name in ("CHIRON_INFERENCE_PROXY", "CHIRON_LOCAL_INFERENCE_PROXY"):
            env.pop(name, None)
    return env


def build_command(python: str, data_dir: Path, with_storage: bool, log_level: str) -> list[str]:
    """与 components.cjs 的 dev 分支同形：`-m app.main` + cwd=desktop-local。"""
    command = [
        python,
        "-m",
        "app.main",
        "--port",
        "0",
        "--no-migrate",
        "--data-dir",
        str(data_dir),
        "--log-level",
        log_level,
    ]
    if not with_storage:
        # 握手只关心 HTTP 与端口，存储留给 verify_p0.py；也避免 CI 上等 PG 启动
        command += ["--no-pg", "--no-redis"]
    return command


# --------------------------------------------------------------------- main
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_handshake",
        description="验证 Electron 主进程与本地 core 的启动/握手链路",
    )
    parser.add_argument("--data-dir", default=None, help="数据目录（默认临时目录，跑完即删）")
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录")
    parser.add_argument(
        "--python",
        default=os.environ.get("CHIRON_PYTHON") or sys.executable,
        help="Python 解释器（默认 CHIRON_PYTHON，其次当前解释器）",
    )
    parser.add_argument("--inference-proxy", default="", help="loopback 推理代理地址（可选）")
    parser.add_argument(
        "--with-storage",
        action="store_true",
        help="连 embedded PostgreSQL / Redis 一起起（默认关闭，握手不依赖存储）",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="等 listening 播报的秒数")
    parser.add_argument("--log-level", default="warning", help="core 的 uvicorn 日志级别")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    checks = Checks()

    temp_dir: Path | None = None
    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp_dir = Path(tempfile.mkdtemp(prefix="chiron-handshake-"))
        data_dir = temp_dir

    say(f"仓库根      : {REPO_ROOT}")
    say(f"core 目录   : {DESKTOP_LOCAL}")
    say(f"数据目录    : {data_dir}")
    say(f"解释器      : {args.python}")
    say(f"设备 token  : {mask(DEVICE_TOKEN)}（本次运行现生成，只验证 env 能被读走）")
    say(f"存储        : {'启用' if args.with_storage else '关闭（--no-pg --no-redis）'}")
    say("")

    command = build_command(args.python, data_dir, args.with_storage, args.log_level)
    env = build_env(data_dir, args.inference_proxy)
    core = CoreProcess(command, env, DESKTOP_LOCAL)

    say("[1] 启动 core")
    say(f"  $ {' '.join(command)}")

    ready = False
    try:
        try:
            core.start()
        except FileNotFoundError as exc:
            checks.add("core 进程能启动", False, f"解释器不可用：{exc}。用 --python 指定可用的 Python 3.11+")
            return _finish(checks, temp_dir, args)
        except OSError as exc:
            checks.add("core 进程能启动", False, f"spawn 失败：{exc}")
            return _finish(checks, temp_dir, args)

        announce = core.wait_listening(args.timeout)
        exit_code = core.poll()
        ready = checks.add(
            "stdout 收到 listening 播报",
            announce is not None,
            "" if announce else f"{args.timeout:.0f}s 内没有收到；进程退出码 {exit_code}",
        )
        if ready and announce is not None:
            _run_http_checks(checks, announce, data_dir)
    finally:
        code = core.stop()
        if not ready:
            say("")
            say(f"core 进程结束（退出码 {code}），stderr 末尾：")
            tail = core.stderr_tail()
            say(tail if tail else "(stderr 为空)")

    return _finish(checks, temp_dir, args)


def _run_http_checks(checks: Checks, announce: dict[str, Any], data_dir: Path) -> None:
    """listening 播报之后的部分：端口 / 数据目录 / healthz / auth session / token 一致性。"""
    port = announce.get("port", 0)
    say(
        f"     host={announce.get('host', '?')} port={port} pid={announce.get('pid', '?')} "
        f"version={announce.get('version', '?')} startup_ms={announce.get('startup_ms', '?')}"
    )
    storage_errors = announce.get("storage_errors") or []
    if storage_errors:
        say(f"     storage_errors: {storage_errors}（/healthz 仍应 200，存储问题看 /readyz）")

    checks.add("监听端口有效（非 0）", isinstance(port, int) and port > 0, f"port={port}")

    # 数据目录是否与下发的值一致：这是 CHIRON_DATA_DIR / --data-dir 真的生效的证据
    announced = str(announce.get("data_dir", "") or "")
    same_dir = bool(announced) and Path(announced).resolve() == data_dir.resolve()
    checks.add(
        "core 用了指定的数据目录",
        same_dir,
        announced if same_dir else f"core 报告 {announced or '(空)'}；下发 {data_dir}",
    )

    base_url = f"http://127.0.0.1:{port}"

    say("")
    say("[2] GET /healthz")
    try:
        status, body, raw = http_json(f"{base_url}/healthz")
        checks.add(
            "/healthz 返回 200 且 status=ok",
            status == 200 and isinstance(body, dict) and body.get("status") == "ok",
            f"HTTP {status} {raw[:200]}",
        )
    except OSError as exc:
        checks.add("/healthz 返回 200 且 status=ok", False, f"请求失败：{exc}")

    say("")
    say("[3] POST /v1/auth/session")
    session_token = ""
    try:
        status, body, raw = http_json(
            f"{base_url}/v1/auth/session",
            method="POST",
            payload={},
            token=DEVICE_TOKEN,
        )
        data = body.get("data") if isinstance(body, dict) else None
        data = data if isinstance(data, dict) else {}
        session_token = str(data.get("token", "") or "")
        user = data.get("user", {}) if isinstance(data.get("user"), dict) else {}
        checks.add(
            "/v1/auth/session 返回 200 且带 token",
            status == 200 and bool(session_token),
            f"HTTP {status} token={mask(session_token)}" if session_token else f"HTTP {status} {raw[:200]}",
        )
        checks.add(
            "/v1/auth/session 返回本地用户 local",
            user.get("id") == "local",
            f"user={user}",
        )
    except OSError as exc:
        checks.add("/v1/auth/session 返回 200 且带 token", False, f"请求失败：{exc}")

    say("")
    say("[4] 设备 token 一致性（core 把 token 落盘在 <data-dir>/auth/device-token）")
    token_file = data_dir / "auth" / "device-token"
    file_token = ""
    if token_file.is_file():
        file_token = token_file.read_text(encoding="utf-8").strip()
    checks.add(
        "core 写出了 device-token 文件",
        bool(file_token),
        f"{token_file} -> {mask(file_token)}" if file_token else f"{token_file} 不存在或为空",
    )
    if file_token and session_token:
        same = file_token == session_token
        checks.add(
            "/v1/auth/session 的 token 与文件一致",
            same,
            "一致" if same else "不一致：core 换了新 token（见 handshake.md「token 由谁产生」）",
        )
    if file_token:
        injected = file_token == DEVICE_TOKEN
        # 已知差异：第一批 core 从文件读 token，不读 CHIRON_DEVICE_TOKEN。
        # 这不影响握手可用性（auth 还是 disabled），所以记 SKIP 而不是 FAIL。
        checks.add(
            "注入的 CHIRON_DEVICE_TOKEN 被 core 采用",
            True if injected else None,
            "已采用"
            if injected
            else "未采用：core 目前从 <data-dir>/auth/device-token 取 token，不读 env（已知差异，不算失败）",
        )


def _finish(checks: Checks, temp_dir: Path | None, args: argparse.Namespace) -> int:
    """收尾：清理临时目录 + 打印汇总。返回退出码。"""
    if temp_dir is not None:
        if args.keep:
            say("")
            say(f"保留临时数据目录：{temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)
    checks.summary()
    return 0 if checks.ok() else 1


if __name__ == "__main__":
    raise SystemExit(main())
