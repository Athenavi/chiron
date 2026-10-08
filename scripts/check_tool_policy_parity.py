#!/usr/bin/env python3
"""Go ↔ Python 判定表的同构检查（**两族**：工具动作分级 + 对话模式）。

背景
----
服务端（Go，``internal/api/tool_policy.go``）是**权威判定**，引擎侧（Python，
``python-engine/app/agent/tool_policy.py``）是同构的**保守镜像**（给前端展示级别 +
本地快速拒绝）。两者语言不同、无法共享代码，只能各写一份 —— 于是两边都写着
"改动其一必须同步另一个"，但这条不变量**此前没有任何机械化断言**，并且**已经漂移**
（``list_subagent_runs`` / ``read_tool_result`` / ``rerun_subagent`` 三处）。

**第二族（2026-10-09 补）**：对话模式。``docs/session-runtime-spec.md`` §3 声称
Go 的 ``validAgentModes``（``internal/api/session_runtime.go``）与引擎
``app/agent/modes.py`` 的 ``AgentMode`` **对齐** —— 同一种"跨语言各写一份"的结构，
此前同样**没有断言**（漂移的表现会很隐蔽：前端能选一个引擎不认的模式，
或引擎支持的模式在服务端被 400 掉）。

**第三族（2026-10-09 补）**：**模型服务提供商目录**。``docs/service-providers.md`` §1/§10 写着
"新增提供商 = 在 `llmProviderCatalog` 追加一项 **+ 同步 Python 兜底目录**，否则网关认得、引擎不认"
—— 又是"跨语言各写一份"，此前同样没有断言（漂移的表现是"目录里有、调用时没有"）。

本脚本把那些注释变成断言：按源码解析这几张表，逐项比对集合。
不做运行期的事（不 import 任何一侧），因此 Go 侧与 Python 侧都能跑。

**关于文件名**：本脚本已覆盖三族（分级表 / 对话模式 / 提供商目录），名字里的 "tool_policy"
是历史遗留 —— 共同的不变量其实是"**两侧各写一份的表必须同构**"。

用法
----
    python scripts/check_tool_policy_parity.py

退出码 0 = 两侧一致；1 = 有漂移（打印差异）。
规格化：仓库根（本文件的上两级）。

维护提示：断言失败时**不要**改这里的解析规则去迁就漂移 —— 要么补齐那一侧，
要么在两侧同时把某个名字移进"有意保留"的注释区（并在
``python-engine/tests/test_tool_policy_coverage.py`` 里点名）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GO_FILE = ROOT / "internal" / "api" / "tool_policy.go"
PY_FILE = ROOT / "python-engine" / "app" / "agent" / "tool_policy.py"
GO_MODES_FILE = ROOT / "internal" / "api" / "session_runtime.go"
PY_MODES_FILE = ROOT / "python-engine" / "app" / "agent" / "modes.py"
GO_CATALOG_FILE = ROOT / "internal" / "api" / "llm_providers.go"
PY_CATALOG_FILE = ROOT / "python-engine" / "app" / "providers" / "catalog.py"
GO_CHAOS_FILE = ROOT / "internal" / "api" / "ent_chaos_handler.go"
PY_CHAOS_FILE = ROOT / "python-engine" / "app" / "chaos" / "injector.py"
GO_TOOLS_MODE_FILE = ROOT / "internal" / "api" / "mode.go"
PY_TOOLS_MODE_FILE = ROOT / "python-engine" / "app" / "agent" / "guards.py"
TS_TOOLS_MODE_FILE = ROOT / "frontend-vue" / "src" / "views" / "ChatView.vue"
#: 第八族：子 Agent run 的**终态集合**。Go 侧是取消端点的幂等判定（`subagent_cancel.go`），
#: 引擎侧是指标标签白名单（`subagent/store.py`）—— 两处注释都写着"与引擎侧保持一致"。
GO_TERMINAL_FILE = ROOT / "internal" / "api" / "subagent_cancel.go"
PY_TERMINAL_FILE = ROOT / "python-engine" / "app" / "subagent" / "store.py"
#: 引擎侧第二处终态集合（"哪些终态要回传给父会话"），与 `_TERMINAL_STATUSES` 必须同集。
PY_REPORT_FILE = ROOT / "python-engine" / "app" / "subagent" / "reporting.py"

#: **跨语言共享的 Redis 键前缀**。两侧各写一份字面量，且**各自都有测试钉住自己的那一份** ——
#: 危险正在这里：改了 Go 侧、被自己的测试拦下、顺手把 Go 的测试也改了，**Python 侧却忘了改**
#: ⇒ 两边测试都绿，漂移照样上线（跨实例协作静默失效：归属查不到、取消回执没人认领）。
#:
#: 条目是 ``(键, Go 文件, Go 正则, Python 文件, Python 正则)``：
#:
#: * **必须比对"赋值/调用"这种代码形态，不能只比裸字面量** —— 这些键在两侧的**模块 docstring
#:   里也写着**（`affinity.py` / `run_registry.py` 都有 `{REDIS_KEY_PREFIX}engine:run:{...}`），
#:   只比裸字面量会被注释满足 ⇒ 改名照样漏过（2026-10-09 实测踩到）。
#: * **必须容忍空白** —— Go 的 `const` 块是 gofmt 对齐的（`runRecordPrefix   = "..."`），
#:   写死单空格的片段会假红（同日实测踩到）。
SHARED_REDIS_KEY_PREFIXES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "subagent:run:",
        "internal/engine/run_affinity.go",
        r'subagentRunPrefix\s*=\s*"subagent:run:"',
        "python-engine/app/subagent/affinity.py",
        r'OWNER_KEY_PREFIX\s*=\s*"subagent:run:"',
    ),
    (
        "subagent:cancel:ack:",
        "internal/api/subagent_cancel.go",
        r'db\.RedisKey\("subagent:cancel:ack:"\)',
        "python-engine/app/subagent/affinity.py",
        r'ACK_KEY_PREFIX\s*=\s*"subagent:cancel:ack:"',
    ),
    (
        "engine:instance:",
        "internal/engine/run_affinity.go",
        r'instanceKeyPrefix\s*=\s*"engine:instance:"',
        "python-engine/app/subagent/remote.py",
        r'rkey\("engine:instance:"\)',
    ),
    # 生产者/消费者形态：引擎写、网关读（`run_affinity.go` 只 `Get`）。
    # 一侧改名 ⇒ 另一侧读不到 ⇒ 回退"无映射"（**静默**降级），所以更要拦。
    (
        "engine:run:",
        "internal/engine/run_affinity.go",
        r'runRecordPrefix\s*=\s*"engine:run:"',
        "python-engine/app/run_registry.py",
        r'rkey\("engine:run:"\)',
    ),
)

#: **跨语言共享的数值常量**（名字不同、值必须相同）。同样各写一份、同样各自有测试 ——
#: 同样有"只改一侧并顺手改该侧测试"的陷阱。这里直接比 `变量 = 值` 的字面出现。
SHARED_CONSTANTS: tuple[tuple[str, str, str, str, str], ...] = (
    # (Go 变量, 值, Go 文件, Python 变量, Python 文件)
    (
        "defaultBranchKeepTail",
        "4",
        "internal/session/manager_branch.go",
        "DEFAULT_KEEP_TAIL",
        "python-engine/app/context/branch_condense.py",
    ),
    (
        "minCondenseMessages",
        "3",
        "internal/session/manager_branch.go",
        "MIN_COMPRESSIBLE_MESSAGES",
        "python-engine/app/context/branch_condense.py",
    ),
)

LEVELS = ("READ", "WRITE", "DELETE", "EXTERNAL")
_NAME_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"')


def _strip_comment(line: str, marker: str) -> str:
    """去掉行内注释。工具名里不会出现 marker，因此不必处理字符串内的 marker。"""
    idx = line.find(marker)
    return line[:idx] if idx >= 0 else line


def parse_go_table(text: str) -> tuple[dict[str, set[str]], set[str]]:
    """解析 ``add(ToolLevelX, ...)``（常量是 ``ToolLevelRead`` 这种驼峰）与 ``commandTools``。"""
    table: dict[str, set[str]] = {lvl: set() for lvl in LEVELS}
    command: set[str] = set()

    lines = [_strip_comment(raw, "//") for raw in text.splitlines()]
    current: str | None = None
    depth = 0
    for line in lines:
        if current is not None:
            depth += line.count("(") - line.count(")")
            table[current].update(_NAME_RE.findall(line))
            if depth <= 0:
                current = None
            continue
        match = re.search(r"\badd\(ToolLevel(Read|Write|Delete|External)\s*,", line)
        if match:
            current = match.group(1).upper()
            depth = line.count("(") - line.count(")")
            table[current].update(_NAME_RE.findall(line[match.end():]))
            if depth <= 0:
                current = None

    block = re.search(r"var commandTools = map\[string\]bool\{(.*?)\n\}", text, re.S)
    if block:
        body = "\n".join(_strip_comment(raw, "//") for raw in block.group(1).splitlines())
        command = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*true', body))
    return table, command


def parse_py_table(text: str) -> tuple[dict[str, set[str]], set[str]]:
    """解析 ``_X_TOOLS: frozenset(...)`` 与 ``COMMAND_TOOLS: frozenset(...)``。

    同一个按括号深度推进的状态机同时覆盖多行块与单行块（``_DELETE_TOOLS`` 是单行）。
    """
    table: dict[str, set[str]] = {lvl: set() for lvl in LEVELS}
    command: set[str] = set()

    lines = [_strip_comment(raw, "#") for raw in text.splitlines()]
    bucket: set[str] | None = None
    depth = 0
    for line in lines:
        if bucket is not None:
            depth += line.count("(") - line.count(")")
            bucket.update(_NAME_RE.findall(line))
            if depth <= 0:
                bucket = None
            continue
        match = re.match(
            r"(_?(?:READ|WRITE|DELETE|EXTERNAL)_TOOLS|COMMAND_TOOLS)\s*:.*?frozenset\(",
            line.strip(),
        )
        if not match:
            continue
        key = match.group(1)
        bucket = command if key == "COMMAND_TOOLS" else table[key.lstrip("_").split("_")[0]]
        depth = line.count("(") - line.count(")")
        bucket.update(_NAME_RE.findall(line[line.index("frozenset(") :]))
        if depth <= 0:
            bucket = None
    return table, command


def parse_go_modes(text: str) -> set[str]:
    """解析 ``var validAgentModes = map[string]bool{...}``（服务端认可的对话模式）。"""
    block = re.search(r"var validAgentModes\s*=\s*map\[string\]bool\{(.*?)\}", text, re.S)
    if not block:
        return set()
    return set(re.findall(r'"([a-z][a-z0-9_]*)"\s*:\s*true', block.group(1)))


def parse_py_modes(text: str) -> tuple[set[str], set[str]]:
    """返回 `(AgentMode 枚举取值, _BASE_MODES 实际覆盖的取值)`。

    两个都解析，是因为 `docs/session-runtime-spec.md` §3 说的是"与 `_BASE_MODES` 对齐"：
    只比枚举的话，"**加了枚举成员却忘了写 base 配置**"这种漂移仍然漏网 —— 那种情况下
    模式只有在被真正选中时才会以 `KeyError` 暴露（`_BASE_MODES[AgentMode(mode)]`）。
    """
    block = re.search(
        r"class AgentMode\([^)]*\):(.*?)(?=\nclass |\n_BASE_MODES|\Z)", text, re.S
    )
    pairs = (
        re.findall(r'(?m)^\s+([A-Z][A-Z0-9_]*)\s*=\s*"([a-z][a-z0-9_]*)"', block.group(1))
        if block
        else []
    )
    values = {value for _name, value in pairs}
    name_to_value = dict(pairs)

    base_block = re.search(r"_BASE_MODES[^=]*=\s*\{(.*?)\n\}", text, re.S)
    covered: set[str] = set()
    if base_block:
        for name in re.findall(r"(?m)^\s+AgentMode\.([A-Z][A-Z0-9_]*)\s*:", base_block.group(1)):
            covered.add(name_to_value.get(name, name.lower()))
    return values, covered


def parse_go_catalog(text: str) -> set[str]:
    """解析 ``llmProviderCatalog`` 里每个 preset 的 ``ID``（服务端目录）。"""
    block = re.search(
        r"llmProviderCatalog\s*=\s*\[\]llmProviderPreset\{(.*?)\n\}", text, re.S
    )
    if not block:
        return set()
    return set(re.findall(r'(?m)^\s*ID:\s*"([a-z0-9-]+)"', block.group(1)))


def parse_py_catalog(text: str) -> set[str]:
    """解析引擎兜底目录 ``catalog.py`` 里的 ``"id"``（Python 侧）。"""
    return set(re.findall(r'"id":\s*"([a-z0-9-]+)"', text))


def parse_go_bool_map(text: str, name: str) -> set[str]:
    """解析 ``var <name> = map[string]bool{...}`` 里为 true 的键。"""
    block = re.search(rf"{name}\s*=\s*map\[string\]bool\{{(.*?)\}}", text, re.S)
    return set(re.findall(r'"([a-z_]+)"\s*:\s*true', block.group(1))) if block else set()


def parse_py_frozenset(text: str, name: str) -> set[str]:
    """解析 ``<name> = frozenset({...})`` 里的字符串元素。"""
    block = re.search(rf"{name}\s*=\s*frozenset\(\{{(.*?)\}}\)", text, re.S)
    return set(re.findall(r'"([a-z_]+)"', block.group(1))) if block else set()


def parse_py_tuple(text: str, name: str) -> set[str]:
    """解析 ``<name> = ("a", "b", ...)`` 里的字符串元素（容忍多行）。"""
    block = re.search(rf"{name}\s*=\s*\((.*?)\)", text, re.S)
    return set(re.findall(r'"([a-z_]+)"', block.group(1))) if block else set()


def parse_go_tools_modes(text: str) -> set[str]:
    """解析 ``mode.go``：``ModeX = "value"`` 常量 + ``validModes`` 的键 ⇒ 取值集合。"""
    consts = dict(re.findall(r'(?m)^\s*(Mode[A-Za-z0-9_]*)\s*=\s*"([a-z]+)"', text))
    block = re.search(r"validModes\s*=\s*map\[string\]bool\{(.*?)\}", text, re.S)
    if not block:
        return set()
    return {
        consts.get(key, key)
        for key in re.findall(r"([A-Za-z0-9_]+)\s*:\s*true", block.group(1))
    }


def parse_py_session_modes(text: str) -> set[str]:
    """解析 ``guards.py``：``SESSION_MODE_X = "value"`` 常量 + ``_VALID_SESSION_MODES`` 的成员。"""
    consts = dict(re.findall(r'(?m)^(SESSION_MODE_[A-Z]+)\s*=\s*"([a-z]+)"', text))
    block = re.search(r"_VALID_SESSION_MODES\s*=\s*frozenset\(\{(.*?)\}\)", text, re.S)
    if not block:
        return set()
    return {
        consts.get(name, name)
        for name in re.findall(r"([A-Z][A-Z0-9_]*)", block.group(1))
    }


def parse_ts_tools_modes(text: str) -> set[str]:
    """解析前端 ``toolsMode = ref<'ask' | 'auto' | 'yolo'>(...)`` 的联合类型。"""
    match = re.search(r"toolsMode\s*=\s*ref<([^>]+)>", text)
    return set(re.findall(r"'([a-z]+)'", match.group(1))) if match else set()


def _code_lines(text: str) -> str:
    """只保留**非注释行**（粗粒度：跳过以 ``//`` / ``#`` / ``*`` / ``/*`` 开头的行）。

    为什么需要：本轮（2026-10-09）实测发现"字面量 / 赋值是否出现"这类判据会被
    **注释与 docstring 满足** —— 键名在模块 docstring 里也写着，于是改名后守卫仍然绿。
    对这类判据，先剥注释再匹配；判据本身则**锚定代码形态**并**容忍空白**。
    """
    keep = [
        line
        for line in text.splitlines()
        if not line.strip().startswith(("//", "#", "*", "/*"))
    ]
    return "\n".join(keep)


def main() -> int:
    if not GO_FILE.exists() or not PY_FILE.exists():
        print(f"FAIL: 找不到策略表文件（{GO_FILE} / {PY_FILE}）", file=sys.stderr)
        return 1

    go_table, go_command = parse_go_table(GO_FILE.read_text(encoding="utf-8"))
    py_table, py_command = parse_py_table(PY_FILE.read_text(encoding="utf-8"))

    problems: list[str] = []
    for level in LEVELS:
        go, py = go_table[level], py_table[level]
        if go != py:
            only_go = sorted(go - py)
            only_py = sorted(py - go)
            problems.append(
                f"{level}: 只在 Go {only_go or '—'}；只在 Python {only_py or '—'}"
            )
    if go_command != py_command:
        problems.append(
            f"COMMAND: 只在 Go {sorted(go_command - py_command) or '—'}；"
            f"只在 Python {sorted(py_command - go_command) or '—'}"
        )

    # 第二族：对话模式（docs/session-runtime-spec.md §3 声称两侧对齐）
    go_modes = (
        parse_go_modes(GO_MODES_FILE.read_text(encoding="utf-8"))
        if GO_MODES_FILE.exists()
        else set()
    )
    py_modes, py_modes_covered = (
        parse_py_modes(PY_MODES_FILE.read_text(encoding="utf-8"))
        if PY_MODES_FILE.exists()
        else (set(), set())
    )
    if not go_modes or not py_modes:
        problems.append(
            f"MODES: 解析为空（Go {sorted(go_modes) or '—'} / Python {sorted(py_modes) or '—'}）"
            " —— 表名或文件位置变了，请更新本脚本的解析规则"
        )
    elif go_modes != py_modes:
        problems.append(
            f"MODES: 只在 Go {sorted(go_modes - py_modes) or '—'}；"
            f"只在 Python {sorted(py_modes - go_modes) or '—'}"
        )
    if py_modes and py_modes != py_modes_covered:
        problems.append(
            f"MODES: 引擎枚举有但 `_BASE_MODES` 没有配置 "
            f"{sorted(py_modes - py_modes_covered) or '—'}（选中该模式会 KeyError）"
        )

    # 第三族：模型服务提供商目录（docs/service-providers.md §1/§10 要求两侧同步）
    go_catalog = (
        parse_go_catalog(GO_CATALOG_FILE.read_text(encoding="utf-8"))
        if GO_CATALOG_FILE.exists()
        else set()
    )
    py_catalog = (
        parse_py_catalog(PY_CATALOG_FILE.read_text(encoding="utf-8"))
        if PY_CATALOG_FILE.exists()
        else set()
    )
    if not go_catalog or not py_catalog:
        problems.append(
            f"CATALOG: 解析为空（Go {len(go_catalog)} / Python {len(py_catalog)} 条）"
            " —— 表名或文件位置变了，请更新本脚本的解析规则"
        )
    elif go_catalog != py_catalog:
        problems.append(
            f"CATALOG: 只在 Go {sorted(go_catalog - py_catalog) or '—'}；"
            f"只在 Python {sorted(py_catalog - go_catalog) or '—'}"
        )

    # 第四族：故障注入的**作用面与故障类型**（ent_chaos_handler.go 写着"必须一致"，
    # 且失败模式很隐蔽：不一致 ⇒ 创建出的实验**永远不会生效**，即当初那个"假注入"）。
    go_chaos_text = (
        GO_CHAOS_FILE.read_text(encoding="utf-8") if GO_CHAOS_FILE.exists() else ""
    )
    py_chaos_text = (
        PY_CHAOS_FILE.read_text(encoding="utf-8") if PY_CHAOS_FILE.exists() else ""
    )
    for label, go_name, py_name in (
        ("TARGETS", "chaosSupportedTargets", "SUPPORTED_TARGETS"),
        ("FAULTS", "chaosSupportedFaults", "MIDDLEWARE_FAULT_TYPES"),
    ):
        go_set = parse_go_bool_map(go_chaos_text, go_name)
        py_set = parse_py_frozenset(py_chaos_text, py_name)
        if not go_set or not py_set:
            problems.append(
                f"CHAOS {label}: 解析为空（Go {sorted(go_set) or '—'} / "
                f"Python {sorted(py_set) or '—'}） —— 表名或文件位置变了"
            )
        elif go_set != py_set:
            problems.append(
                f"CHAOS {label}: 只在 Go {sorted(go_set - py_set) or '—'}；"
                f"只在 Python {sorted(py_set - go_set) or '—'}"
            )

    # 第五族：跨语言共享的 Redis 键前缀（比对**代码形态的正则**，且先剥注释）
    for literal, go_rel, go_pat, py_rel, py_pat in SHARED_REDIS_KEY_PREFIXES:
        go_path, py_path = ROOT / go_rel, ROOT / py_rel
        go_text = _code_lines(go_path.read_text(encoding="utf-8")) if go_path.exists() else ""
        py_text = _code_lines(py_path.read_text(encoding="utf-8")) if py_path.exists() else ""
        if not re.search(go_pat, go_text):
            problems.append(f"KEY {literal!r}: Go 侧 {go_rel} 匹配不到 /{go_pat}/（改名了？）")
        if not re.search(py_pat, py_text):
            problems.append(f"KEY {literal!r}: Python 侧 {py_rel} 匹配不到 /{py_pat}/（改名了？）")

    # 第六族：跨语言共享的数值常量（同样：剥注释 + 容忍空白）
    for go_var, value, go_rel, py_var, py_rel in SHARED_CONSTANTS:
        go_path, py_path = ROOT / go_rel, ROOT / py_rel
        go_text = _code_lines(go_path.read_text(encoding="utf-8")) if go_path.exists() else ""
        py_text = _code_lines(py_path.read_text(encoding="utf-8")) if py_path.exists() else ""
        if not re.search(rf"\b{go_var}\s*=\s*{value}\b", go_text):
            problems.append(
                f"CONST {go_var}: {go_rel} 匹配不到 `{go_var} = {value}`（值被改了？）"
            )
        if not re.search(rf"\b{py_var}\s*=\s*{value}\b", py_text):
            problems.append(
                f"CONST {py_var}: {py_rel} 匹配不到 `{py_var} = {value}`（值被改了？）"
            )

    # 第七族：**工具授权模式（ask/auto/yolo）** —— 唯一一条**三语言**约定
    # （Go `mode.go` ↔ 引擎 `guards.py` ↔ 前端 `ChatView.vue` 的联合类型）。
    # 注意它与第二族的"对话模式"是**两张不同的表**：这里管"要不要问用户"，
    # 那边管"用哪套工具/人设档案"。用**集合比对**而非"字面量出现"，因为
    # "只在一侧新增一个模式"正是最需要拦的方向。
    go_tm = (
        parse_go_tools_modes(GO_TOOLS_MODE_FILE.read_text(encoding="utf-8"))
        if GO_TOOLS_MODE_FILE.exists()
        else set()
    )
    py_tm = (
        parse_py_session_modes(PY_TOOLS_MODE_FILE.read_text(encoding="utf-8"))
        if PY_TOOLS_MODE_FILE.exists()
        else set()
    )
    ts_tm = (
        parse_ts_tools_modes(TS_TOOLS_MODE_FILE.read_text(encoding="utf-8"))
        if TS_TOOLS_MODE_FILE.exists()
        else set()
    )
    if not go_tm or not py_tm or not ts_tm:
        problems.append(
            f"TOOLS_MODE: 解析为空（Go {sorted(go_tm) or '—'} / Python {sorted(py_tm) or '—'} / "
            f"TS {sorted(ts_tm) or '—'}） —— 文件或写法变了，请更新本脚本的解析规则"
        )
    elif not (go_tm == py_tm == ts_tm):
        problems.append(
            f"TOOLS_MODE: 三侧不一致 —— Go {sorted(go_tm)} / Python {sorted(py_tm)} / TS {sorted(ts_tm)}"
        )

    # 第八族：**子 Agent run 的终态集合**（Go 取消端点的幂等判定 ↔ 引擎的指标标签白名单）。
    #
    # 漂移的代价是**假成功**：Go 少了某个终态，取消一个**已完成**的 run 时就不会走
    # `not_running` 幂等分支，而是照发取消广播 —— 前端显示"已请求停止"而状态永不变化，
    # 正是 `subagent_cancel.go` 那段注释说"此前无条件 accepted 是典型假成功"的那类问题。
    #
    # 2026-10-09 实测：Go 侧漏了 `partial`（引擎 `subagent_runner.py` 在"有产出的失败"时
    # **真的会写** `status = "partial"`），而两侧注释都写着"与引擎侧保持一致"。
    go_term = (
        parse_go_bool_map(GO_TERMINAL_FILE.read_text(encoding="utf-8"), "terminalRunStatuses")
        if GO_TERMINAL_FILE.exists()
        else set()
    )
    py_term = (
        parse_py_frozenset(PY_TERMINAL_FILE.read_text(encoding="utf-8"), "_TERMINAL_STATUSES")
        if PY_TERMINAL_FILE.exists()
        else set()
    )
    # 引擎侧还有第二处"终态集合"：`reporting.REPORTABLE`（哪些终态要回传给父会话）。
    # 它与 `_TERMINAL_STATUSES` 必须**同集** —— 少了某项 ⇒ 该状态的 run 永远不被汇报
    # （2026-10-09：`partial` 两处都漏，其中这一处的后果是"有产出的部分完成永不上报"）。
    py_report = (
        parse_py_tuple(PY_REPORT_FILE.read_text(encoding="utf-8"), "REPORTABLE")
        if PY_REPORT_FILE.exists()
        else set()
    )
    if not go_term or not py_term:
        problems.append(
            f"TERMINAL: 解析为空（Go {sorted(go_term) or '—'} / Python {sorted(py_term) or '—'}）"
            " —— 表名或文件位置变了，请更新本脚本的解析规则"
        )
    elif go_term != py_term:
        problems.append(
            f"TERMINAL: 只在 Go {sorted(go_term - py_term) or '—'}；"
            f"只在 Python {sorted(py_term - go_term) or '—'}（少一个 ⇒ 取消已终态的 run 会假成功）"
            " —— 本族**权威在引擎**（状态是引擎写的，Go 只做幂等判定）"
        )
    if py_term and not py_report:
        problems.append(
            "TERMINAL REPORTABLE: 解析为空 —— `reporting.REPORTABLE` 的写法变了，请更新本脚本"
        )
    elif py_term and py_report != py_term:
        problems.append(
            f"TERMINAL REPORTABLE: 与 `_TERMINAL_STATUSES` 不一致 —— "
            f"只在 REPORTABLE {sorted(py_report - py_term) or '—'}；"
            f"只在终态白名单 {sorted(py_term - py_report) or '—'}"
            "（漏项 ⇒ 该状态的 run 永远不会回传给父会话）"
        )

    total = sum(len(v) for v in go_table.values())
    if problems:
        print("FAIL: Go 与 Python 的判定/目录/键约定已漂移（两侧必须同构）：")
        for problem in problems:
            print("  - " + problem)
        print("\n同步规则见两个文件开头的注释；权威侧是 Go。")
        return 1

    print(
        f"OK: 两侧同构 —— read/write/delete/external 共 {total} 条，"
        f"命令类 {len(go_command)} 个，对话模式 {len(go_modes)} 个"
        f"（{'/'.join(sorted(go_modes))}），提供商目录 {len(go_catalog)} 条，"
        f"故障注入 作用面 {len(parse_go_bool_map(go_chaos_text, 'chaosSupportedTargets'))} "
        f"/ 类型 {len(parse_go_bool_map(go_chaos_text, 'chaosSupportedFaults'))}，"
        f"共享键前缀 {len(SHARED_REDIS_KEY_PREFIXES)} 个，共享常量 {len(SHARED_CONSTANTS)} 个，"
        f"工具授权模式 {len(go_tm)} 个（{'/'.join(sorted(go_tm))}，三语言一致），"
        f"子 Agent 终态 {len(go_term)} 个（{'/'.join(sorted(go_term))}）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
