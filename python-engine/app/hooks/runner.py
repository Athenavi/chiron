"""批 G：单个 hook 的沙箱执行（方案 03 §3.2 —— 复用 `plugin_runner.py` 的既有模式）。

**不新造沙箱**：与 `app/api/plugins.py:run_plugin_in_sandbox` 同一套隔离 ——
独立 subprocess（`sys.executable plugin_runner.py`）+ 子进程内 `setrlimit`
内存/CPU 限制 + `app/tools/code_guard.py` 的静态 AST 与运行时受控 builtins；
payload 走 stdin、结果走 stdout，宿主 env 由 `sandboxed_env()` 清空。
与插件链路的唯一差别是**用途**：这里跑的是生命周期 hook，不是插件。

契约（继承自 `plugin_runner.py`）：hook 脚本导出 `main(input) -> dict`；任何
失败都以 `{"success": false, "error": ...}` JSON 输出。本模块把这一切**收束成
永不抛异常**的 `HookOutcome`，让调用方（主流程）天然 fire-and-forget。

批 G+ 批次 2b（`docs/hook-protocol-design.md` §4.3 选项 **(b)**）新增 **`command` 形态**：
运维在**部署级声明文件**里给出的本地命令。它与 `python` 形态的差别只有三处，其余原语
（清环境 / RLIMIT / 截断 / 审计）**完全复用**：

1. 不走 `plugin_runner.py`，直接 `create_subprocess_exec(argv…)`（**不经过 shell**）；
2. 准入用**部署自备的 allowlist**（`hooks_command_allowlist`，**允许绝对路径**）——
   而不是那张**面向模型**的白名单：前者约束"模型挑的命令"，后者是"运维声明的命令"，
   两者信任级不同（§4.3 实测表）；
3. 判定语义按 DSH 约定：**退出码 2 = 阻断**（stderr 尾部作为原因），其余非 0 是
   **非阻断失败**（动作照走 + 留审计）。

批 G+ 批次 3 再加 **`webhook` 形态**：出站 POST 到运维声明的 URL。它是**超越 DSH** 的一环
（DSH 根本没有 http/webhook 形态），因此治理按同一套路收：独立开关默认关 + **目标 host
白名单**（默认空 = fail-closed）+ 复用 `app/tools/ssrf.py` 的出站守卫 + **载荷内容无关**
（不送 `tool_arguments` / `tool_result`，也不送租户身份）+ 响应体积上限。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import settings
from app.tools.sandbox import _normalize_exe, _rlimit_kwargs, sandboxed_env, truncate_execute_output

logger = logging.getLogger(__name__)

#: hook 子进程资源上限（hook 只做判定/通知，取与插件沙箱同量级即可）。
_HOOK_MAX_MEMORY_MB = 128
_HOOK_MAX_CPU_SECONDS = 5.0

#: python 形态的 stdout 体积上限，防 hook 刷屏撑爆宿主内存。
_MAX_OUTPUT_BYTES = 262_144  # 256 KiB

#: python-engine 根目录下的 `plugin_runner.py`（复用同一 runner，不复制一份）。
_RUNNER_PATH = Path(__file__).resolve().parents[2] / "plugin_runner.py"

#: **退出码约定**（DSH / Claude Code 兼容）：只有 2 表示阻断，其余非 0 一律是
#: "非阻断失败"（动作照走、失败记日志）。
BLOCK_EXIT_CODE = 2

#: 被准入检查拦下时 `HookOutcome.error` 的前缀 —— 与 `app/tools/sandbox.py` 同款约定，
#: 让审计能把"**拦住**"与"执行失败"分开，而不是靠上层猜。
COMMAND_BLOCKED_PREFIX = "command blocked"

#: `webhook` 形态被准入检查拦下时的前缀（同上，单独一个是为了让审计能区分两种形态）。
WEBHOOK_BLOCKED_PREFIX = "webhook blocked"

#: `webhook` 响应的体积上限（它只用来承载**判定**，不是数据通道）。
_MAX_WEBHOOK_BYTES = 64 << 10  # 64 KiB


@dataclass(frozen=True)
class HookOutcome:
    """一次 hook 执行的结果（供调用方判定阻断与落审计）。"""

    success: bool
    output: Any
    error: str | None
    exit_code: int | None
    duration_ms: int
    timed_out: bool
    #: 输出是否被**本层**截断（`command` 形态复用 `truncate_execute_output`；python 形态
    #: 是"超限即拒绝"而非截断，故恒为 False）。截断由产生截断的这一层给出。
    truncated: bool = False


def _elapsed_ms(started: float) -> int:
    return int((time.time() - started) * 1000)


async def _terminate(proc: asyncio.subprocess.Process | None) -> None:
    """超时兜底 —— 与 `plugin_runner` 的既有降级语义一致（kill + 回收）。"""
    if proc is None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


async def run_hook(
    *, name: str, code: str, context: dict[str, Any], timeout: float
) -> HookOutcome:
    """在独立子进程中执行一个 hook，返回结构化结果（**永不**向调用方抛异常）。"""
    payload = {
        "plugin_name": name,
        "code": code,
        "input": context,
        "max_memory_mb": _HOOK_MAX_MEMORY_MB,
        "max_cpu_seconds": _HOOK_MAX_CPU_SECONDS,
    }
    started = time.time()
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_RUNNER_PATH),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_RUNNER_PATH.parent),
            env=sandboxed_env(),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            timeout=timeout,
        )
    except TimeoutError:
        await _terminate(proc)
        return HookOutcome(
            success=False,
            output=None,
            error=f"hook timed out after {timeout}s",
            exit_code=None,
            duration_ms=_elapsed_ms(started),
            timed_out=True,
        )
    except OSError as exc:
        return HookOutcome(
            success=False,
            output=None,
            error=f"failed to start hook sandbox: {exc}",
            exit_code=None,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
        )

    exit_code = proc.returncode
    if len(stdout) > _MAX_OUTPUT_BYTES:
        return HookOutcome(
            success=False,
            output=None,
            error=f"hook output exceeds limit ({len(stdout)} > {_MAX_OUTPUT_BYTES} bytes)",
            exit_code=exit_code,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
        )

    try:
        parsed = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        # runner 崩溃（未输出 JSON）：fail-soft —— 记 stderr 供排查，不拖垮主流程。
        tail = stderr.decode("utf-8", errors="replace")[-1000:] if stderr else "no stderr"
        return HookOutcome(
            success=False,
            output=None,
            error=f"hook sandbox crashed (exit={exit_code}): {tail}",
            exit_code=exit_code,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
        )

    return HookOutcome(
        success=bool(parsed.get("success")),
        output=parsed.get("output"),
        error=parsed.get("error"),
        exit_code=exit_code,
        duration_ms=_elapsed_ms(started),
        timed_out=False,
    )


# ── `command` 形态（批次 2b）──────────────────────────────────────────────────

#: 部署自备 allowlist 的分隔符（逗号）。
_ALLOWLIST_SEP = ","


def _strip_wrapping_quotes(token: str) -> str:
    """剥掉 token 首尾成对的引号（Windows 上必须手动做，见 `split_command`）。"""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def split_command(command: str) -> tuple[list[str], str | None]:
    """把声明的命令文本拆成 argv（**永不经过 shell**）。返回 `(argv, 拒绝原因)`。

    为什么不直接上 shell：hook 命令是**运维声明的**，但仍不该多一个注入面 —— 用
    `create_subprocess_exec(*argv)` 直执后，管道 / 重定向 / 变量展开 / 通配符都不存在。
    需要 shell 特性的钩子请**显式**写 `bash -c "…"`（`bash` 本身要进 allowlist）。

    Windows 上关闭 shlex 的 POSIX 规则并**手动剥掉包裹引号**：POSIX 规则会把
    `"C:\\hooks\\a.exe"` 里的反斜杠当转义符吃掉，而选项 (b) 明确要支持**绝对路径**
    的钩子脚本（这是"让既有 Claude Code / Codex 脚本能直接用"的前提）。
    """
    text = (command or "").strip()
    if not text:
        return [], "empty command"
    posix = os.name != "nt"
    try:
        parts = shlex.split(text, posix=posix)
    except ValueError as exc:
        return [], f"invalid command syntax: {exc}"
    if not posix:
        parts = [_strip_wrapping_quotes(item) for item in parts]
    if not parts or not parts[0]:
        return [], "empty command"
    return parts, None


def _csv_entries(raw: str | None) -> list[str]:
    """逗号分隔的声明列表 → 去空白 / 去空项的条目列表（`command` 与 `webhook` 共用）。"""
    return [item.strip() for item in (raw or "").split(_ALLOWLIST_SEP) if item.strip()]


def _allowlist_entries() -> list[str]:
    """部署自备的可执行文件 allowlist（`settings.hooks_command_allowlist`，逗号分隔）。"""
    return _csv_entries(getattr(settings, "hooks_command_allowlist", ""))


def _exe_identity(token: str) -> str:
    """把一个可执行 token 归一成**可比较的身份**。

    - 绝对路径 ⇒ 解析后的绝对路径（两侧都走这里，符号链接/`..` 才不会一边解析一边不解析）；
    - 裸名 ⇒ 走 `sandbox._normalize_exe`（去平台后缀 + 小写）—— **刻意复用同一套规则**，
      同一件事写两遍必然漂移。
    """
    if os.path.isabs(token):
        try:
            return os.path.normcase(str(Path(token).resolve()))
        except OSError:  # pragma: no cover — 解析失败只可能是路径畸形
            return ""
    return _normalize_exe(token)


def command_policy_error(command: str) -> str | None:
    """`command` 形态的准入判定：返回拒绝原因，`None` 表示放行。

    三条闸口（任一不过即拒，**fail-closed**）：

    1. `hooks_allow_commands` —— 本形态的**独立开关**，默认关（它新增一个**运维级**
       可执行面，见 `docs/hook-protocol-design.md` §4.3 选项 (b)）；
    2. allowlist **为空 ⇒ 一条也不批准**（与插件命令白名单"未配置即全部拒绝"同款默认）；
    3. 可执行文件的身份必须落在 allowlist 内。

    注册时与**起进程之前**各判一次：注册时判是为了"配了却不生效"能当场看见，起进程前判
    是因为那才是真正的执行点（注释声称的拦截必须有断言打在它身上）。
    """
    if not getattr(settings, "hooks_allow_commands", False):
        return "command hooks are disabled (hooks_allow_commands=false)"
    argv, reason = split_command(command)
    if reason:
        return reason
    allowed = {identity for identity in (_exe_identity(item) for item in _allowlist_entries()) if identity}
    if not allowed:
        return "no hook command allowlist configured (hooks_command_allowlist is empty)"
    if _exe_identity(argv[0]) not in allowed:
        return f"executable not in hooks_command_allowlist: {argv[0]}"
    return None


def _command_cwd() -> str:
    """hook 命令的工作目录 = **声明文件所在目录**（运维面），无声明文件时回退进程 cwd。

    与 `run_in_sandbox` 把 cwd 锁进沙箱 workspace 不同：hook 命令是**运维声明的**
    （与 `docker-compose.yml`、沙箱白名单本身同一信任级），按相对路径找自己的脚本/配置
    是它的正常用法。
    """
    config_path = str(getattr(settings, "hooks_config_path", "") or "")
    if config_path:
        parent = Path(config_path).expanduser().parent
        if parent.is_dir():
            return str(parent)
    return os.getcwd()


def _stderr_tail(text: str) -> str:
    """取 stderr 尾部作为原因（stderr 才是"为什么"的通道，stdout 留给结构化结果）。"""
    return (text or "").strip()[-1000:]


async def run_command_hook(
    *, name: str, command: str, context: dict[str, Any], timeout: float
) -> HookOutcome:
    """在独立子进程中执行一个**运维声明**的本地命令 hook（批次 2b）。

    - **不走 shell**（`split_command` 的结果直接作为 argv）；
    - **准入在起进程之前**再判一次（`command_policy_error`）；
    - stdin 收事件上下文 JSON（DSH / Claude Code 约定）、stdout 收**可选**的结构化 JSON、
      stderr 作为原因通道；
    - **退出码 2 = 阻断** ⇒ 折成一个 `{"decision": "deny", ...}` 输出，于是上层的
      `_blocking_decision` 无需为本形态写第二套判定；
    - 复用隔离原语：`sandboxed_env()`（清环境）+ `_rlimit_kwargs()`（内存/CPU）+
      `truncate_execute_output()`（输出上限 + **显式标注**截断量）。

    与 `run_hook` 一样**永不向调用方抛异常**。
    """
    policy = command_policy_error(command)
    if policy:
        return HookOutcome(
            success=False,
            output=None,
            error=f"{COMMAND_BLOCKED_PREFIX}: {policy}",
            exit_code=None,
            duration_ms=0,
            timed_out=False,
        )
    argv, reason = split_command(command)
    if reason:  # pragma: no cover — `command_policy_error` 已判过同一条
        return HookOutcome(
            success=False,
            output=None,
            error=f"{COMMAND_BLOCKED_PREFIX}: {reason}",
            exit_code=None,
            duration_ms=0,
            timed_out=False,
        )

    started = time.time()
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_command_cwd(),
            env=sandboxed_env(),
            **_rlimit_kwargs(),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(context, ensure_ascii=False).encode("utf-8")),
            timeout=timeout,
        )
    except TimeoutError:
        await _terminate(proc)
        return HookOutcome(
            success=False,
            output=None,
            error=f"hook command timed out after {timeout}s",
            exit_code=None,
            duration_ms=_elapsed_ms(started),
            timed_out=True,
        )
    except OSError as exc:
        return HookOutcome(
            success=False,
            output=None,
            error=f"failed to start hook command: {exc}",
            exit_code=None,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
        )

    exit_code = proc.returncode
    stdout_text, stdout_truncated = truncate_execute_output(stdout.decode("utf-8", errors="replace"))
    stderr_text, stderr_truncated = truncate_execute_output(stderr.decode("utf-8", errors="replace"))
    truncated = stdout_truncated or stderr_truncated

    if exit_code == BLOCK_EXIT_CODE:
        return HookOutcome(
            success=True,
            output={"decision": "deny", "reason": _stderr_tail(stderr_text) or "blocked by hook command"},
            error=None,
            exit_code=exit_code,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
            truncated=truncated,
        )
    if exit_code != 0:
        tail = _stderr_tail(stderr_text) or "no stderr"
        return HookOutcome(
            success=False,
            output=None,
            error=f"hook command exited {exit_code}: {tail}",
            exit_code=exit_code,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
            truncated=truncated,
        )

    # 退出码 0：stdout 是**可选**的结构化判定通道（两种输入都接受，§4.4）。
    output: Any = None
    text = stdout_text.strip()
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # 不改变语义（退出码 0 仍算成功），但**不静默**：想把结果告诉引擎却写坏了 JSON，
            # 必须能从日志里看见（fail-open 但可见）。
            logger.warning("hook command %r exited 0 but stdout is not JSON: %s", name, text[:200])
        else:
            output = parsed
    return HookOutcome(
        success=True,
        output=output,
        error=None,
        exit_code=exit_code,
        duration_ms=_elapsed_ms(started),
        timed_out=False,
        truncated=truncated,
    )


# ── `webhook` 形态（批次 3）─────────────────────────────────────────────────


def webhook_target_error(url: str) -> str | None:
    """`webhook` 形态的**静态**准入判定：返回拒绝原因，`None` 表示放行。

    两条闸口（**纯静态、不触网**，因此注册时也能判 —— 注册时做 DNS 解析会让"离线部署"
    变成"配了却注册失败"，那是把两件事混在一起）：

    1. `hooks_allow_webhooks` —— 本形态的**独立开关**，默认关（它是一次数据出境面）；
    2. host 必须在 `hooks_webhook_allowlist` 内；**白名单为空 ⇒ 一个目标也不批准**。

    scheme / 端口 / IP 段 / DNS rebinding 由 `app/tools/ssrf.py` 的既有守卫在**执行时**判
    （`assert_safe_url`）—— 那是运行时事实，不是配置事实。
    """
    if not getattr(settings, "hooks_allow_webhooks", False):
        return "webhook hooks are disabled (hooks_allow_webhooks=false)"
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return f"invalid webhook url: {url!r}"
    allowed = {item.lower() for item in _csv_entries(getattr(settings, "hooks_webhook_allowlist", ""))}
    if not allowed:
        return "no hook webhook allowlist configured (hooks_webhook_allowlist is empty)"
    if host not in allowed:
        return f"host not in hooks_webhook_allowlist: {host}"
    return None


def webhook_payload(context: dict[str, Any]) -> dict[str, Any]:
    """**内容无关**的出站载荷。

    刻意**不**送 `tool_arguments` / `tool_result`（可能是租户内容）与
    `tenant_id` / `user_id` / `session_id`（租户标识）：把载荷或身份送到**运维配置的外部
    URL** 是一次**数据出境**决定，`docs/hook-protocol-design.md` §4.3 的拍板结论是
    **默认只送内容无关字段**。要送它们需要**再拍一次板**并单独加开关 —— 本批不做。
    """
    from app.audit_log import utc_timestamp

    payload: dict[str, Any] = {
        "event": str(context.get("event", "")),
        "ts": utc_timestamp(),
    }
    if context.get("tool_name"):
        payload["tool_name"] = str(context["tool_name"])
    if "ok" in context:
        payload["ok"] = bool(context["ok"])
    return payload


async def run_webhook_hook(
    *, name: str, url: str, context: dict[str, Any], timeout: float
) -> HookOutcome:
    """把一次 hook 事件**出站 POST** 给运维声明的 URL（批次 3）。

    与另两种形态的异同：

    * **判定语义一致**：2xx + `{"decision": "deny", "reason": ...}` ⇒ 阻断（同样折进
      `HookOutcome.output`，于是上层只有一套判定）；非 2xx / 传输失败 ⇒ **非阻断失败**
      （fail-open + 留审计）；
    * **不跟随重定向**：POST 本不该被重定向，且"跟随"会多一条绕过出口检查的路径
      （`assert_safe_url` 只在发起前判一次）—— 3xx 一律按非 2xx 处理；
    * **载荷内容无关**（见 `webhook_payload`）；
    * **响应体积有上限且流式计数**：它只承载判定，不是数据通道。
    """
    policy = webhook_target_error(url)
    if policy:
        return HookOutcome(
            success=False,
            output=None,
            error=f"{WEBHOOK_BLOCKED_PREFIX}: {policy}",
            exit_code=None,
            duration_ms=0,
            timed_out=False,
        )

    # 运行时事实在这里判：scheme / 端口 / DNS 解析 / IP 黑名单（内网与云元数据）。
    from app.tools.ssrf import assert_safe_url

    try:
        assert_safe_url(url)
    except ValueError as exc:
        return HookOutcome(
            success=False,
            output=None,
            error=f"{WEBHOOK_BLOCKED_PREFIX}: SSRF check failed: {exc}",
            exit_code=None,
            duration_ms=0,
            timed_out=False,
        )

    import httpx

    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream("POST", url, json=webhook_payload(context)) as resp:
                status = resp.status_code
                total = 0
                chunks: list[bytes] = []
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_WEBHOOK_BYTES:
                        return HookOutcome(
                            success=False,
                            output=None,
                            error=f"hook webhook response exceeds {_MAX_WEBHOOK_BYTES} bytes",
                            exit_code=status,
                            duration_ms=_elapsed_ms(started),
                            timed_out=False,
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
    except httpx.TimeoutException:
        return HookOutcome(
            success=False,
            output=None,
            error=f"hook webhook timed out after {timeout}s",
            exit_code=None,
            duration_ms=_elapsed_ms(started),
            timed_out=True,
        )
    except Exception as exc:  # noqa: BLE001 — 出站失败一律非阻断，但必留审计
        return HookOutcome(
            success=False,
            output=None,
            error=f"hook webhook transport error: {exc}",
            exit_code=None,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
        )

    text = body.decode("utf-8", errors="replace").strip()
    if not 200 <= status < 300:
        tail = text[-1000:] or "no body"
        return HookOutcome(
            success=False,
            output=None,
            error=f"hook webhook returned {status}: {tail}",
            exit_code=status,
            duration_ms=_elapsed_ms(started),
            timed_out=False,
        )

    output: Any = None
    if text:
        try:
            output = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(
                "hook webhook %r returned 2xx but its body is not JSON: %s", name, text[:200]
            )
    return HookOutcome(
        success=True,
        output=output,
        error=None,
        exit_code=status,
        duration_ms=_elapsed_ms(started),
        timed_out=False,
    )
