"""sandbox — per-user 运行环境隔离（A 层：代码级 root/cwd 锁定）

所有 agent 文件/命令工具强制在 `SANDBOX_ROOT/{tenant}/{user}/workspace/` 内
执行，模型无法接触宿主文件系统（解决：思考/输出暴露服务器真实路径）。

- get_sandbox_dir() / workspace_dir()：由 contextvars（user/tenant）推导
- safe_join()：相对路径 clamp 到 workspace，拒绝 ../ 与绝对路径逃逸
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 沙箱根环境变量名（多副本部署必须指向共享卷；启动校验见 app.main.verify_sandbox_root）
SANDBOX_ROOT_ENV = "SANDBOX_ROOT"

#: Windows 下用 cmd /c 包裹执行前，拦截参数中的 shell 元字符
#: （这些字符经 shlex.split 保留后传给 cmd /c 会被解释为 shell 语法，绕过白名单）
_SHELL_META = re.compile(r"[|&;`$()<>#\n]")


def sandbox_root() -> Path:
    """沙箱根：默认置于进程 cwd 上两级（项目外），避免 workspace 路径
    泄露服务器项目结构（S 安全修复：cwd 泄露项目/python-engine 前缀）。

    注意：默认值是**进程本地路径**，仅适用于单机开发。多副本部署必须显式设置
    SANDBOX_ROOT 指向共享卷（NFS/PVC），否则同一用户的任务落到不同副本时
    看不到彼此写入的文件（agent 文件工作流"间歇性失忆"）。
    """
    env = os.environ.get(SANDBOX_ROOT_ENV)
    if env:
        return Path(env).resolve()
    # 无论 cwd 是项目根还是子目录（python-engine），上两级都到项目外
    return Path(os.getcwd()).resolve().parent.parent / "chiron-sandbox"


def get_sandbox_dir() -> Path:
    """当前 user 的沙箱根（由 context 推导，自动创建）。"""
    from app.tools.context import get_tenant_id, get_user_id

    tenant = get_tenant_id() or "default"
    user = get_user_id() or "anonymous"
    d = sandbox_root() / tenant / user
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("sandbox dir create failed: %s", e)
    return d


def workspace_dir() -> Path:
    """agent 文件操作的唯一根目录。"""
    d = get_sandbox_dir() / "workspace"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("workspace dir create failed: %s", e)
    return d


def safe_join(rel: str) -> Path:
    """将相对路径 clamp 到 workspace，拒绝逃逸（绝对路径/../）。"""
    base = workspace_dir().resolve()
    target = (base / rel).resolve()
    if target != base and not str(target).startswith(str(base) + os.sep):
        raise ValueError(f"path escapes sandbox: {rel}")
    return target


def sandboxed_env() -> dict[str, str]:
    """执行环境：清理敏感/宿主变量，仅保留基础 PATH，并把用户目录变量
    重定向到沙箱内，防止 `$HOME`/`~` 泄漏宿主路径（S 安全修复）。

    注意：PYTHONPATH 和 PYTHONHOME 被显式排除（不在 allow 集合中），
    防止沙箱子进程通过 import 加载宿主模块。
    """
    allow = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
    }
    env = {k: v for k, v in os.environ.items() if k in allow}
    # 显式清除 PYTHONPATH/PYTHONHOME，防止沙箱子进程加载宿主模块
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONSTARTUP", None)
    env["HOME"] = str(workspace_dir())
    env["USERPROFILE"] = str(workspace_dir())
    env["TEMP"] = str(get_sandbox_dir())
    env["TMP"] = str(get_sandbox_dir())
    return env


# 被禁止的 shell 逃逸模式（S 安全修复：cwd 锁定无法阻止命令内绝对路径访问宿主）
# 注意：生产部署为 Linux/alpine（见 Dockerfile），必须覆盖 Unix 绝对路径。
_ESCAPE_PATTERNS = [
    r"[A-Za-z]:[\\/]",  # Windows 盘符绝对路径 (C:\ 或 C:/)
    r"\\\\",  # UNC 路径
    r"(^|[^A-Za-z0-9_.])(\.\.)[\\/]",  # 父目录跳转 ..\ 或 ../
    r"\bcd\b[^&|;]*[A-Za-z]:[\\/]",  # cd 到绝对路径
    # Unix/Linux 逃逸：绝对路径（/xxx，2+ 字符路径段或单独 /）、~ 家目录、$HOME 变量。
    # 以空格/引号/分号开头界定，避免误伤相对路径（a/b）、URL（http://）
    # 与 Windows cmd 单字符开关（/b /d /s）。
    r"(?:^|[\s\"';])(?:/(?:[A-Za-z0-9_.-]{2,}|$)|~/?|\$\{?HOME\}?\b)",
    # 云元数据 / 内网地址（curl/wget 等 SSRF 常见目标）
    r"(?:^|[\s\"';])(?:curl|wget)\s+https?://(?:169\.254\.169\.254|127\.0\.0\.1|localhost)",
]

# 允许在沙箱内执行的命令白名单（仅允许安全的 Python/数据操作命令）
_ALLOWED_EXECUTABLES: set[str] = {
    # Python 解释器
    "python",
    "python3",
    # Git 版本控制（经 _ensure_sandbox_repo 校验，不会穿透宿主仓库）
    "git",
    # 安全的基础命令
    "echo",
    "ls",
    "dir",
    "cat",
    "type",
    "head",
    "tail",
    "wc",
    "find",
    "grep",
    "sort",
    "uniq",
    "cut",
    "tr",
    "tee",
}


def _normalize_exe(name: str) -> str:
    """规范化可执行文件名：取 basename，去平台后缀，小写。"""
    name = Path(name).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _parse_command(command: str) -> tuple[list[str], str | None]:
    """将命令字符串解析为 [executable, *args]，校验白名单。

    返回 (args, error_reason)。error_reason 非 None 表示被拒绝。
    """
    command = command.strip()
    if not command:
        return [], "empty command"

    try:
        parts = shlex.split(command)
    except ValueError as e:
        return [], f"invalid command syntax: {e}"

    if not parts:
        return [], "empty command"

    exe_name = _normalize_exe(parts[0])

    # 白名单校验（python/python3 已在 _ALLOWED_EXECUTABLES 中，
    # sys.executable 完整路径由 _normalize_exe 规范化后统一走白名单校验）
    if exe_name not in _ALLOWED_EXECUTABLES:
        return [], f"executable not allowed: {exe_name}"

    return parts, None


def _has_escape(command: str) -> str | None:
    """检测命令是否含逃逸模式，命中返回原因。"""
    import re

    for pat in _ESCAPE_PATTERNS:
        m = re.search(pat, command)
        if m:
            return pat
    return None


async def run_in_sandbox(command: str, timeout: int = 120) -> dict[str, Any]:
    """在沙箱 workspace 内执行命令（direct exec，不经过 shell），
    cwd 锁定 + 环境清理 + 逃逸拦截 + 命令白名单。

    使用 create_subprocess_exec 替代 create_subprocess_shell，
    彻底消除 shell 元字符注入（管道/重定向/变量展开/通配符等）。
    """
    import asyncio.subprocess

    # 第一层：正则逃逸拦截（绝对路径/父目录/SSRF）
    esc = _has_escape(command)
    if esc:
        return {
            "error": "command blocked: absolute-path / parent-directory access is not allowed in sandbox",
            "reason": esc,
        }

    # 第二层：解析命令 + 白名单校验
    args, reason = _parse_command(command)
    if reason:
        return {"error": f"command blocked: {reason}"}

    prog = args[0]
    rest = args[1:]

    # 对 git 命令额外限制：禁止 clone/fetch/pull/push 等网络操作，仅允许本地操作
    git_restricted = {"clone", "fetch", "pull", "push", "remote", "submodule", "archive", "ls-remote"}
    if _normalize_exe(prog) == "git" and rest:
        subcmd = rest[0] if rest and not rest[0].startswith("-") else ""
        if subcmd in git_restricted:
            return {"error": f"git command blocked: '{subcmd}' is not allowed in sandbox"}

    # Windows: echo/dir/type 等是 cmd.exe 内置命令，无独立可执行文件，
    # create_subprocess_exec 直执会 WinError 2；用 cmd /c 包裹执行。
    # 安全措施：在包裹前检测 args 中是否包含 shell 元字符（见模块级 _SHELL_META）。
    exe_name = _normalize_exe(prog)
    if sys.platform == "win32" and exe_name not in ("python", "python3"):
        for part in args:
            if _SHELL_META.search(part):
                return {"error": f"command blocked: shell meta-character in argument: {part!r}"}
        prog, rest = os.environ.get("COMSPEC", "cmd.exe"), ["/d", "/s", "/c", *args]

    proc = await asyncio.create_subprocess_exec(
        prog,
        *rest,
        cwd=str(workspace_dir()),
        env=sandboxed_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": "timeout", "timeout": timeout}
    return {
        "exit_code": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }
