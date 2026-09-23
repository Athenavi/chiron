"""工具动作分级与参数归一化 —— 权限边界与循环检测的共同基础。

**为什么需要它**

"是否需要人工确认""是否需要二次校验""是否必须经服务端分发"，本质上取决于**动作的语义**，
而不是工具名：`shell_exec` 既能 `ls` 也能 `rm -rf`。而工具名会随插件 / Profile 变化，
用固定名单表达策略必然漂移 —— 现状 `guards.py` 里 `WRITE_TOOLS` 与 `DANGEROUS_TOOLS`
两套并列名单已经彼此不一致（例如 `browser_screenshot` 只在后者里）。

**分级**

| 级别 | 含义 | 处置 |
|---|---|---|
| ``read`` | 只读，无副作用 | 放行 |
| ``write`` | 本地副作用，可回滚（配合文件快照） | 人工确认 |
| ``delete`` | 破坏性 / 不可逆 | 人工确认 **+ 二次校验** |
| ``external`` | 触达外部业务系统 | 人工确认 **+ 二次校验**（P2 起改由 Tool Broker 分发） |

**fail-closed**

未声明级别的工具一律按 :data:`WRITE` 处理 —— 漏登记的后果是"多一次确认"，而不是"少一次"。
同理，命令类工具（`shell_exec` 等）本身能读能写能删，一律先按 :data:`DELETE` 起步，
只有明确判定为无害命令时才降级。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

READ = "read"
WRITE = "write"
DELETE = "delete"
EXTERNAL = "external"

VALID_LEVELS: frozenset[str] = frozenset({READ, WRITE, DELETE, EXTERNAL})

# ── 分级表 ────────────────────────────────────────────────────────────────

#: 只读：不产生副作用（本地读取 + 受控检索 + 走自家 provider 的分析类）
_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read_file", "read_image", "glob_files", "grep_files", "search_files", "file_analyzer",
        "git_status", "git_log", "git_diff",
        "kb_list", "kb_search", "rag_query", "knowledge",
        "memory_search", "recall",
        "skill_list", "skill_discover", "skill_run",
        "mode_list", "graph_templates", "agent_list", "agent_session_list",
        "read_subagent_result", "list_subagent_runs", "job_output", "stderr_drain",
        "requirement_validate", "task_decompose", "tech_design", "prd_generate",
        "vision_analyze", "speech_to_text",
        "tool",
    }
)

#: 本地写：产生副作用但**可回滚**（P1 的文件快照覆盖文件类；记忆/图/会话是本地状态）
_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "write_file", "edit_file",
        "git_commit", "git_branch",
        "graph_create", "agent_session_create",
        "remember", "mode_edit", "media_create",
        "image_generate", "text_to_speech", "skill_generate",
        # 委派类：它们内部会执行任意工具（子 Agent 有自己的栅栏），因此在父层至少要确认一次
        "subagent", "agent_dispatch", "code_agent", "workflow_run", "graph_run",
        # 重跑也是"再派一个作业"（会消耗 token 并复用原来的写权限），与 subagent 同级
        "rerun_subagent",
    }
)

#: 破坏性：不可逆（删除/终止/清除）
_DELETE_TOOLS: frozenset[str] = frozenset({"job_kill", "forget", "delete_file", "kb_delete"})

#: 触达外部系统（浏览器 / 网络 / 外部技能包）
_EXTERNAL_TOOLS: frozenset[str] = frozenset({"web_fetch", "web_search", "skill_install"})

#: 前缀规则：工具名随 action 变化（如 browser_navigate / browser_click）时用前缀兜底。
#: 规则先于精确表生效，避免"新增一个 browser_xxx 就漏登记"。
_PREFIX_LEVELS: tuple[tuple[str, str], ...] = (
    ("browser", EXTERNAL),
    ("web_", EXTERNAL),
    ("mcp_", EXTERNAL),
)

_TOOL_LEVELS: dict[str, str] = {}
for _names, _level in (
    (_READ_TOOLS, READ),
    (_WRITE_TOOLS, WRITE),
    (_DELETE_TOOLS, DELETE),
    (_EXTERNAL_TOOLS, EXTERNAL),
):
    for _name in _names:
        _TOOL_LEVELS[_name] = _level

#: 命令类工具：级别取决于**命令内容**，先按最严起步，再按内容判定
COMMAND_TOOLS: frozenset[str] = frozenset(
    {"shell_exec", "execute_command", "persistent_shell", "execute_python", "run_code"}
)

#: 无害命令白名单：命中即把命令类工具降为 write（仍需确认，但不走二次校验）。
_SAFE_CMD_RE = re.compile(
    r"^\s*(ls|dir|cat|type|head|tail|wc|pwd|echo|which|where|whoami|git\s+(status|log|diff|show)|"
    r"python\s+--version|go\s+version|node\s+--version|npm\s+--version)\b",
    re.IGNORECASE,
)

#: 破坏性命令：命中即**升级**为 delete（即使工具名中性）
_DESTRUCTIVE_CMD_RE = re.compile(
    r"(\brm\b|\brmdir\b|\bdel\b|\berase\b|\bunlink\b|\bshutdown\b|\bkill\b|"
    r"\bdrop\s+(table|database|schema)\b|\btruncate\b|\bdelete\s+from\b|"
    r"git\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+--force)|"
    r"format\b|mkfs\b|>\s*/dev/sd)",
    re.IGNORECASE,
)

_COMMAND_ARG_KEYS = ("command", "cmd", "script", "code", "source")


def tool_level(name: str, args: dict[str, Any] | None = None) -> str:
    """工具 + 参数 → 动作级别。

    未声明的工具返回 :data:`WRITE`（fail-closed）。命令类工具按**内容**判定：
    破坏性命令升级为 :data:`DELETE`，无害命令降为 :data:`WRITE`，其余保持 :data:`DELETE`。
    """
    base = _declared_level(name)
    if name in COMMAND_TOOLS:
        command = _command_text(args)
        if command and _DESTRUCTIVE_CMD_RE.search(command):
            return DELETE
        if command and _SAFE_CMD_RE.search(command):
            return WRITE
        # 命令为空（如 execute_python 只传 code 走别的键）或无法判定：保守停在 DELETE
        return DELETE
    return base


def _declared_level(name: str) -> str:
    level = _TOOL_LEVELS.get(name)
    if level is not None:
        return level
    for prefix, prefixed_level in _PREFIX_LEVELS:
        if name.startswith(prefix):
            return prefixed_level
    return WRITE


def _command_text(args: dict[str, Any] | None) -> str:
    if not isinstance(args, dict):
        return ""
    for key in _COMMAND_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def requires_confirmation(level: str, mode: str) -> bool:
    """按会话授权模式决定是否需要人工确认。

    - ``ask``：write / delete / external 都要确认；
    - ``auto``：只有 delete / external（"仅危险工具"）—— 与改造前 `DANGEROUS_TOOLS` 等价；
    - ``yolo``：都不确认（用户显式选择跳过），但参数级硬拦截与二次校验仍然生效。

    注：命令类工具在 :func:`tool_level` 里不会低于 ``write``，所以 auto 下也会被确认 ——
    这与改造前把 `shell_exec` 放进 `DANGEROUS_TOOLS` 的行为一致。
    """
    if mode == "yolo":
        return False
    if mode == "ask":
        return level in {WRITE, DELETE, EXTERNAL}
    # auto（含未知模式：保守按 auto 处理）
    return level in {DELETE, EXTERNAL}


def requires_second_check(level: str) -> bool:
    """是否需要**二次校验**（票据绑定 + 执行前复核）。

    只有不可逆（delete）与外部触达（external）需要 —— write 有文件快照可回滚，
    给每个写操作都加一道票据校验只会让正常流程变脆。
    """
    return level in {DELETE, EXTERNAL}


# ── 参数归一化与哈希（票据绑定 + 重复检测共用同一份实现）────────────────────

#: 值是路径的参数键：需要规范化，否则 `./a.txt` 与 `a.txt` 会被当成两次不同的调用
_PATH_ARG_KEYS: frozenset[str] = frozenset(
    {"path", "file", "filepath", "filename", "dir", "directory", "root", "cwd", "target", "dest", "source"}
)


def _normalize_path(value: str) -> str:
    path = value.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.rstrip("/") or path


def canonical_args(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """归一化参数：键排序 + 字符串去空白 + 路径规范化。

    目的是让"同一个意图的不同写法"得到同一个哈希 —— 否则重复检测与票据校验都能被
    改写一下参数就绕开（`{"path":"./a.txt"}` vs `{"path":"a.txt"}`）。
    """
    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    for key in sorted(args):
        value = args[key]
        if isinstance(value, str):
            value = value.strip()
            if key.lower() in _PATH_ARG_KEYS:
                value = _normalize_path(value)
        out[key] = value
    return out


def args_hash(name: str, args: dict[str, Any] | None) -> str:
    """(工具名, 归一化参数) 的稳定短哈希 —— 票据绑定与重复检测的判据。"""
    payload = json.dumps(
        {"name": name, "args": canonical_args(name, args)},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
