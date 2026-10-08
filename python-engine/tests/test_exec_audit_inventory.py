"""不变量：**每条起进程的执行路径都必须有审计落点**（或显式登记为"不需要"）。

为什么要有这条机械断言：2026-10-08 的一次对抗性核查发现——`shell_exec` / `run_code` /
`persistent_shell` 都写 `exec_audit.jsonl`，而**后台命令 `tool_job` 与 git 工具完全没有痕迹**，
偏偏后台命令是最难事后观察的一条。这类缺口靠"看代码"发现不了，只能靠**枚举**。

本测试枚举 `app/` 下所有起进程的位置，并要求每一处都能归入下面三类之一：

1. `AUDITED`：该文件自己写审计（exec / hooks / plugin）；
2. `AUDITED_BY_CALLER`：执行点本身不写，但其**唯一调用方**在每次执行前后都写了
   （必须写清是哪个文件）；
3. `EXEMPT`：**不是"agent 执行的命令"**，理由必须写明（例如只跑固定参数的只读命令）。

新增一处起进程的代码而没归类 ⇒ 本测试失败。`KNOWN_UNAUDITED` 里是**已知缺口**
（不是豁免）：它们在路线图里作为待办跟踪，修好后应移入前列。
"""

from __future__ import annotations

import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: 起进程的写法（子进程 / 异步子进程）
_SPAWN = re.compile(r"create_subprocess_exec|create_subprocess_shell|subprocess\.(Popen|run|check_output|call)")
#: 注释与字符串字面量（三引号优先）——先剥掉再匹配，否则"黑名单里列举这些名字"的文件
#: （如 `tools/code_guard.py` 把 `subprocess.run` 写成字符串）会被误判成起进程。
_STRING_OR_COMMENT = re.compile(
    r'"""(?:.|\n)*?"""'
    r"|'''(?:.|\n)*?'''"
    r'|"(?:[^"\\]|\\.)*"'
    r"|'(?:[^'\\]|\\.)*'"
    r"|#[^\n]*"
)

#: 文件（相对 app/，POSIX 分隔符）→ (归类理由, 该文件里**必须出现**的审计标记)
#: 标记按**原文**匹配：plugin audit 的落点就是一句 `"plugin_audit.jsonl"` 字面量。
AUDITED = {
    "tools/sandbox.py": ("shell_exec：写 exec_audit", "record_execution"),
    "tools/run_code.py": ("run_code：写 exec_audit", "record_execution"),
    "tools/terminal.py": ("persistent_shell：写 exec_audit", "record_execution"),
    "tools/job_runner.py": ("tool_job（后台命令）：每个终态都写 exec_audit（含取消分支）", "record_execution"),
    "tools/git_tools.py": ("git 工具：写 exec_audit（含超时路径）", "record_execution"),
    "api/plugins.py": ("插件安装/测试：调用 record_plugin 写 plugin_audit.jsonl", "record_plugin"),
    "mcp/client.py": (
        "运行时拉起 MCP 插件进程：写 plugin audit（spawn 成功与失败各一条）",
        "record_plugin",
    ),
    "skill/manager.py": (
        "技能安装路径拉起 MCP stdio 进程：写 plugin audit（含 command-not-found 与通用失败）",
        "record_plugin",
    ),
}

AUDITED_BY_CALLER = {
    "hooks/runner.py": "hooks/manager.py 在执行前后调用 audit.record_hook（4 个终态）",
}

EXEMPT = {
    "agent/prompt_engine.py": "只跑**固定参数**的 git 只读命令（rev-parse/status/log），无外部输入",
}

#: 已知缺口（**不是豁免**）：目前为空 —— `mcp/client.py` / `skill/manager.py` 已通过
#: `app/plugins/audit.py` 接入 plugin audit。新增缺口请登记于此并在路线图里跟踪。
KNOWN_UNAUDITED: dict[str, str] = {}


def _spawn_sites() -> dict[str, tuple[str, str]]:
    """返回 {相对路径: (原文, 剥掉注释/字符串后的代码)}，只含真的起进程的文件。"""
    sites: dict[str, tuple[str, str]] = {}
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        raw = path.read_text(encoding="utf-8")
        code = _STRING_OR_COMMENT.sub("", raw)
        if _SPAWN.search(code):
            sites[path.relative_to(APP).as_posix()] = (raw, code)
    return sites


def test_every_spawn_site_is_classified() -> None:
    sites = _spawn_sites()
    classified = set(AUDITED) | set(AUDITED_BY_CALLER) | set(EXEMPT) | set(KNOWN_UNAUDITED)

    unclassified = sorted(set(sites) - classified)
    assert not unclassified, (
        "以下位置会起进程但没有归类 —— 请判断它是否需要审计，"
        "然后加入 AUDITED / AUDITED_BY_CALLER / EXEMPT（写明理由）或 KNOWN_UNAUDITED：\n  "
        + "\n  ".join(unclassified)
    )

    stale = sorted(classified - set(sites))
    assert not stale, f"归类表里有已经不再起进程的条目（请删除）：{stale}"


def test_audited_sites_really_have_an_audit_marker() -> None:
    """`AUDITED` 里的文件必须真的能看到声明的审计调用 —— 防止归错类（把缺口写成"已修"）。"""
    sites = _spawn_sites()
    missing = [
        f"{name}（缺 {marker}）"
        for name, (_reason, marker) in AUDITED.items()
        if marker not in sites[name][0]
    ]
    assert not missing, f"这些文件被标为 AUDITED 但文件里找不到声明的审计调用：{missing}"


def test_known_gaps_are_not_silently_audited_or_exempt() -> None:
    """已知缺口不许同时出现在"已审计/豁免"里 —— 否则两份记录会互相矛盾。"""
    overlap = (set(KNOWN_UNAUDITED) & (set(AUDITED) | set(AUDITED_BY_CALLER) | set(EXEMPT)))
    assert not overlap, f"同一文件同时被标为已知缺口与已处理：{sorted(overlap)}"
