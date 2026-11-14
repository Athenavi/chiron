"""工具结果的分级压缩 —— **截断 ≠ 摘要**。

**现状的问题**

`runtime._truncate_text` 是 `head + "...(truncated N)..." + tail`。它对三类常见结果都不友好：

- **日志 / 命令输出**：错误行通常在中间，被挖掉；
- **长列表**：关键项在中间，被挖掉；
- **JSON**：结构信息在中间，被挖掉。

模型拿到一段被挖空的文本，比拿到"结构化的概要"更容易误判，或干脆重复调用同一个工具。

**本模块做什么**

1. 按**内容形态**选摘要策略（**不调 LLM** —— 每个工具调用都过一次 LLM 等于延迟与成本翻倍）；
2. 按**字节数分级**：小的原样进上下文，大的只放摘要 + 落盘引用（`result_ref`），
   模型需要细节时再取回 —— 与子 Agent 的 L0/L1/L2 范式一致，不另造一套。

本模块是**纯函数**（无 IO），落盘与取回见 ``app/agent/result_store.py``。

## 裁决是机器可读的（切口 6）

`compact()` 返回 `Compaction`，其中 `strategy` / `clip_reason` 是**机器可读码**而不是散文 ——
上游可以据此发事件、记审计、写评测断言，而不必去猜摘要文本里有没有"省略"两个字。
对位 deepagents 的取舍：**只有在真的发生裁剪/截断时才提示**（`clip_reason != "none"`）。

`summarize()` 保留（返回摘要文本），内部委托 `compact()` —— 既有调用方与测试逐字不变。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

#: 小于它 → 原样进上下文。与既有 `TOOL_RESULT_MAX_CHARS`（16K）对齐：
#: 原本就原样进上下文的结果不该因为引入摘要机制反而被压缩。
INLINE_LIMIT = 16 * 1024
#: 超过它 → 只放摘要 + 引用（**绝不进全文**）
SUMMARY_LIMIT = 256 * 1024

INLINE = "inline"
COMPACT = "compact"
REF_ONLY = "ref_only"

# ── 裁剪原因（机器可读；`CLIP_NONE` = 没有发生裁剪）─────────────────────────
CLIP_NONE = "none"
#: 摘要自身超出预算被截断（`_summarize_lines` / `_summarize_paths` 的兜底）
CLIP_BYTE_CAP = "byte_cap"
#: 走 head+tail：首尾保留、中段丢弃
CLIP_HEAD_TAIL = "head_tail"

# ── 摘要策略（机器可读）───────────────────────────────────────────────────
STRATEGY_JSON = "json"
STRATEGY_LINES = "lines"
STRATEGY_PATHS = "paths"
STRATEGY_HEAD_TAIL = "head_tail"

# ── 命令族（按**命令**定制摘要，而不是只看内容形态）────────────────────────
#
# 只看内容形态会误判：`pytest` 的输出是"行文本"，于是走 `_summarize_lines` 的
# 「开头 3 行 + 关键行 + 结尾 3 行」。可测试输出真正需要的只有三样 —— 失败用例名、
# 断言原因、末尾那行汇总（`1 failed, 3 passed in 2.1s`）；而 `_IMPORTANT_LINE_RE`
# 命中的 `error/warn` 在 pytest 里多半是**通过的用例**打印的日志，会把失败用例
# 挤出这 10 条配额。同理 `git diff` 需要的不是逐行内容而是"改了哪些文件、增删多少"，
# `docker ps` 需要的是一张表而不是首尾几行。
#
# 因此：**有命令上下文时优先按命令族摘要，没有时回落到内容形态**。
COMMAND_TEST = "cmd_test"
COMMAND_GIT_DIFF = "cmd_git_diff"
COMMAND_GIT_STATUS = "cmd_git_status"
COMMAND_BUILD = "cmd_build"
COMMAND_INSTALL = "cmd_install"
COMMAND_DOCKER = "cmd_docker"
COMMAND_LOG = "cmd_log"

#: 命令**首词** → 族。只认"命令自己在做什么"，不猜参数语义。
_COMMAND_FIRST_WORD: dict[str, str] = {
    "pytest": COMMAND_TEST,
    "py.test": COMMAND_TEST,
    "tox": COMMAND_TEST,
    "nox": COMMAND_TEST,
    "jest": COMMAND_TEST,
    "vitest": COMMAND_TEST,
    "mocha": COMMAND_TEST,
    "go": COMMAND_BUILD,  # `go test` 会在下面被特判成测试族
    "cargo": COMMAND_BUILD,
    "npm": COMMAND_BUILD,
    "pnpm": COMMAND_BUILD,
    "yarn": COMMAND_BUILD,
    "make": COMMAND_BUILD,
    "cmake": COMMAND_BUILD,
    "gradle": COMMAND_BUILD,
    "mvn": COMMAND_BUILD,
    "dotnet": COMMAND_BUILD,
    "tsc": COMMAND_BUILD,
    "docker": COMMAND_DOCKER,
    "docker-compose": COMMAND_DOCKER,
    "kubectl": COMMAND_DOCKER,
    "git": COMMAND_GIT_STATUS,  # 子命令决定细化到 diff / status
    "pip": COMMAND_INSTALL,
    "uv": COMMAND_INSTALL,
    "poetry": COMMAND_INSTALL,
    "apt": COMMAND_INSTALL,
    "apt-get": COMMAND_INSTALL,
    "brew": COMMAND_INSTALL,
}

#: 这些子命令本身就在跑测试（首词是包管理器/构建器时）。
_TEST_SUBCOMMANDS = frozenset({"test", "tests", "pytest", "jest", "vitest", "e2e"})
#: `npm run build` / `cargo build` 这类"子命令即动作"的词。
_BUILD_SUBCOMMANDS = frozenset({"build", "compile", "check", "typecheck", "lint", "tsc"})


def classify_command(command: str) -> str | None:
    """把一条 shell 命令归类到命令族；认不出返回 ``None``（回落内容形态摘要）。

    只看**首词与紧跟的子命令**：`go test ./...` 是测试、`go build ./...` 是构建；
    `git diff` 与 `git status` 的摘要需求完全不同。带环境变量前缀
    （`FOO=1 pytest -q`）时跳过前缀，否则首词永远是 `FOO=1`。
    """
    text = " ".join((command or "").split())
    if not text:
        return None
    parts = text.split(" ")
    index = 0
    while index < len(parts) and ("=" in parts[index] or parts[index] in {"sudo", "env"}):
        index += 1
    if index >= len(parts):
        return None
    first = parts[index].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    sub = parts[index + 1].lower() if index + 1 < len(parts) else ""

    if first == "git":
        if sub == "diff":
            return COMMAND_GIT_DIFF
        if sub in {"status", "st"}:
            return COMMAND_GIT_STATUS
        if sub == "log":
            return COMMAND_LOG
        return COMMAND_GIT_STATUS
    if first in {"go", "cargo", "npm", "pnpm", "yarn"} and sub in _TEST_SUBCOMMANDS:
        return COMMAND_TEST
    if first in {"npm", "pnpm", "yarn"} and sub == "run" and index + 2 < len(parts):
        if parts[index + 2].lower() in _TEST_SUBCOMMANDS:
            return COMMAND_TEST
        if parts[index + 2].lower() in _BUILD_SUBCOMMANDS:
            return COMMAND_BUILD
    family = _COMMAND_FIRST_WORD.get(first)
    if family == COMMAND_BUILD and sub in _TEST_SUBCOMMANDS:
        return COMMAND_TEST
    return family

#: 行文本里"值得留下"的行。head+tail 会让它们整段消失，而它们往往才是模型真正需要的。
_IMPORTANT_LINE_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|denied|timeout|panic|warning|fatal)\b"
    r"|错误|失败|异常|警告|报错"
)

#: 看起来像文件路径的行（用于"文件列表"的目录聚合）
_PATH_LINE_RE = re.compile(r"^[^\s]*[/\\][^\s]*$")


@dataclass(frozen=True)
class Compaction:
    """一次压缩的**裁决**（机器可读）。

    字段含义：

    * `text`：进上下文的文本（摘要，或原样）；
    * `tier`：`inline` / `compact` / `ref_only`（按字节数分级，见 `classify`）；
    * `strategy`：实际使用的摘要策略（`json` / `lines` / `paths` / `head_tail`）；
    * `clip_reason`：**是否发生了裁剪**以及为什么 —— `none` / `byte_cap` / `head_tail`。
      上游据此决定"要不要提示模型/用户"，而不是去散文里找关键词；
    * `original_bytes`：原文的 UTF-8 字节数（计量/审计用）。
    """

    text: str
    tier: str
    strategy: str
    clip_reason: str = CLIP_NONE
    original_bytes: int = 0


def classify(text: str) -> str:
    """按字节数分级。"""
    size = len(text.encode("utf-8"))
    if size <= INLINE_LIMIT:
        return INLINE
    if size <= SUMMARY_LIMIT:
        return COMPACT
    return REF_ONLY


def compact(text: str, *, budget_chars: int = 3000, command: str | None = None) -> Compaction:
    """按内容形态压缩，并返回**机器可读的裁决**（纯规则，不调 LLM）。

    ``command`` 非空时**先按命令族摘要**（见 `classify_command`）：测试输出的
    失败用例、`git diff` 的文件台账、构建错误，都比"开头 3 行 + 结尾 3 行"有用得多。
    认不出命令族或没给命令时，回落到按内容形态。
    """
    size = len(text.encode("utf-8"))
    tier = classify(text)

    family = classify_command(command) if command else None
    if family:
        out, clip = _summarize_command(family, text, budget_chars)
        return Compaction(out, tier, family, clip, size)

    stripped = text.lstrip()
    if stripped[:1] in "{[":
        parsed = _try_json(text)
        if parsed is not None:
            out, clip = _summarize_json(parsed, budget_chars)
            return Compaction(out, tier, STRATEGY_JSON, clip, size)

    lines = text.splitlines()
    if len(lines) >= 8:
        if _looks_like_path_list(lines):
            out, clip = _summarize_paths(lines, budget_chars)
            return Compaction(out, tier, STRATEGY_PATHS, clip, size)
        out, clip = _summarize_lines(lines, budget_chars)
        return Compaction(out, tier, STRATEGY_LINES, clip, size)

    out, clip = _head_tail(text, budget_chars)
    return Compaction(out, tier, STRATEGY_HEAD_TAIL, clip, size)


def summarize(
    text: str, *, budget_chars: int = 3000, command: str | None = None
) -> str:
    """按内容形态生成摘要（纯规则，不调 LLM）。

    只返回**文本**；需要裁决码（策略 / 是否裁剪）请用 `compact()`。
    """
    return compact(text, budget_chars=budget_chars, command=command).text


# ── 各形态的摘要（返回 `(文本, 裁剪原因)`）──────────────────────────────────


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 - 不是合法 JSON 就走行文本路径
        return None


def _summarize_json(value: Any, budget_chars: int) -> tuple[str, str]:
    """JSON：给**结构**而不是给片段 —— 顶层键、数组长度、少量样例。"""
    head = f"JSON {type(value).__name__}"
    if isinstance(value, dict):
        parts = [f"{head}（{len(value)} 个键）"]
        for key in list(value)[:20]:
            parts.append(f"  - {key}: {_shape(value[key])}")
            if sum(len(p) for p in parts) > budget_chars:
                parts.append("  …（其余键省略）")
                break
        return "\n".join(parts), CLIP_NONE
    if isinstance(value, list):
        parts = [f"JSON 数组（{len(value)} 项）"]
        for index, item in enumerate(value[:3]):
            parts.append(f"  - [{index}]: {_shape(item)}")
        if len(value) > 3:
            parts.append(f"  …（其余 {len(value) - 3} 项省略）")
        return "\n".join(parts), CLIP_NONE

    text = json.dumps(value, ensure_ascii=False, default=str)
    return _head_tail(f"{head}: {text}", budget_chars)


def _shape(value: Any) -> str:
    """单个值的"形状"描述：类型 + 规模 + 一点点内容。"""
    if isinstance(value, dict):
        return f"object({len(value)} keys)"
    if isinstance(value, list):
        preview = ", ".join(_short(v, 40) for v in value[:2])
        return f"array({len(value)}){f' e.g. {preview}' if preview else ''}"
    if isinstance(value, str):
        size = len(value.encode("utf-8"))
        return f"str({size}B) {_short(value, 80)}"
    return f"{type(value).__name__}={_short(value, 40)}"


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _summarize_lines(lines: list[str], budget_chars: int) -> tuple[str, str]:
    """行文本：前几行 + **匹配 error/warn 的行** + 尾几行 + 总数。

    中段的关键行单独挑出来，是这一档相对 head+tail 的核心改进。
    """
    total = len(lines)
    important = [
        (index + 1, line) for index, line in enumerate(lines) if _IMPORTANT_LINE_RE.search(line)
    ][:10]

    parts = [f"共 {total} 行；下面按「开头 / 关键行 / 结尾」摘要（非原文全文）"]
    parts.append("--- 开头 ---")
    parts.extend(lines[:3])
    if important:
        parts.append(f"--- 关键行（error/warn/失败，共命中 {len(important)} 条）---")
        parts.extend(f"[L{number}] {line}" for number, line in important)
    else:
        parts.append("--- 关键行：无 error/warn/失败 命中 ---")
    parts.append("--- 结尾 ---")
    parts.extend(lines[-3:])

    text = "\n".join(parts)
    if len(text) <= budget_chars:
        return text, CLIP_NONE
    return text[:budget_chars] + "\n…（摘要本身已截断）", CLIP_BYTE_CAP


def _summarize_paths(lines: list[str], budget_chars: int) -> tuple[str, str]:
    """文件列表：总数 + 目录聚合 + 少量样例。逐条列出对模型没有信息增益。"""
    entries = [line.strip() for line in lines if line.strip()]
    directories: dict[str, int] = {}
    for path in entries:
        normalized = path.replace("\\", "/")
        top = normalized.rsplit("/", 1)[0] if "/" in normalized else "."
        directories[top] = directories.get(top, 0) + 1

    parts = [f"文件列表：共 {len(entries)} 项，分布在 {len(directories)} 个目录"]
    for directory, count in sorted(directories.items(), key=lambda item: -item[1])[:15]:
        parts.append(f"  - {directory}/: {count} 项")
    parts.append("前 5 项样例：")
    parts.extend(f"  {path}" for path in entries[:5])

    text = "\n".join(parts)
    if len(text) <= budget_chars:
        return text, CLIP_NONE
    return text[:budget_chars] + "\n…（摘要本身已截断）", CLIP_BYTE_CAP


def _looks_like_path_list(lines: list[str]) -> bool:
    sample = [line for line in lines if line.strip()][:20]
    if len(sample) < 8:
        return False
    hits = sum(1 for line in sample if _PATH_LINE_RE.match(line.strip()))
    return hits / len(sample) >= 0.8


def _head_tail(text: str, budget_chars: int) -> tuple[str, str]:
    """兜底：保留 head + tail（与既有 `_truncate_text` 一致）。"""
    if len(text) <= budget_chars:
        return text, CLIP_NONE
    head = text[: budget_chars // 2]
    tail = text[-(budget_chars // 2) :]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n…（省略 {dropped} 字符）…\n{tail}", CLIP_HEAD_TAIL


# ── 命令族摘要 ──────────────────────────────────────────────────────────────
#
# 每个函数签名为 ``(text, budget_chars) -> (摘要, 裁剪原因)``，由 `_summarize_command`
# 分派；认不出的族回落到内容形态摘要。

#: 测试框架的失败用例行：pytest(`FAILED x::y - ...`)、go test(`--- FAIL: T`)、
#: jest/vitest(`✕ name` / `FAIL path`)、unittest(`FAIL: test_x (...)`)。
_TEST_FAIL_RE = re.compile(r"^(?:FAILED|FAIL:|FAIL|--- FAIL:|✕|×|✗|●)\s*\S")
#: pytest 的断言原因行（`E   AssertionError: assert 1 == 2`）。
_TEST_REASON_RE = re.compile(r"^E\s{1,4}\S")
#: 汇总行：`1 failed, 3 passed in 2.1s` / `ok  pkg  0.12s` / `PASS`。
_TEST_SUMMARY_RE = re.compile(
    r"(?i)\b\d+\s+(?:passed|failed|errors?|skipped|warnings?|deselected)\b"
    r"|^=+\s*.*(?:passed|failed|error|no tests ran).*\s*=+$"
    r"|^(?:ok|PASS|FAIL|Tests:\s)"
)
#: 构建/编译错误行（含 TS 编号与 linker 报错）。
_BUILD_ERROR_RE = re.compile(
    r"(?i)(?:\berror\b|\bfailed\b|error\[[A-Z]?\d+\]|undefined reference"
    r"|cannot find|no such file|\bTS\d{4}\b|\bfatal\b|\bpanic:)"
)
_DIFF_FILE_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)")


def _fit(parts: list[str], budget_chars: int) -> tuple[str, str]:
    """把已拼好的摘要行收进预算；超了就按字节截断并如实标注。"""
    text = "\n".join(parts)
    if len(text) <= budget_chars:
        return text, CLIP_NONE
    return text[:budget_chars] + "\n…（摘要本身已截断）", CLIP_BYTE_CAP


def _summarize_command(
    family: str, text: str, budget_chars: int
) -> tuple[str, str]:
    """按命令族摘要；该族摘要不适用（结构对不上）时回落内容形态。"""
    summarizer = _COMMAND_SUMMARIZERS.get(family)
    if summarizer is not None:
        result = summarizer(text, budget_chars)
        if result is not None:
            return result
    lines = text.splitlines()
    if len(lines) >= 8:
        return _summarize_lines(lines, budget_chars)
    return _head_tail(text, budget_chars)


def _summarize_test(text: str, budget_chars: int) -> tuple[str, str] | None:
    """测试输出：**失败用例 + 断言原因 + 汇总行**。

    这三样才是"接着该修哪个"。`_IMPORTANT_LINE_RE` 在测试输出上会误伤 ——
    通过的用例打印的 `WARNING`/`error` 日志会把失败用例挤出配额。
    """
    lines = text.splitlines()
    failures: list[str] = []
    reasons: list[str] = []
    summary: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if _TEST_FAIL_RE.match(line):
            failures.append(line)
        elif _TEST_REASON_RE.match(line):
            reasons.append(line)
        elif _TEST_SUMMARY_RE.search(line) and len(line) < 300:
            summary.append(line)

    if not failures and not summary and not reasons:
        return None  # 不像测试输出（可能是被包了一层的非测试命令）

    parts = [f"共 {len(lines)} 行测试输出；失败用例 {len(failures)} 条"]
    if failures:
        parts.append("--- 失败用例 ---")
        parts.extend(failures[:15])
        if len(failures) > 15:
            parts.append(f"…（另有 {len(failures) - 15} 条失败用例）")
    if reasons:
        parts.append("--- 断言原因 ---")
        parts.extend(reasons[:10])
        if len(reasons) > 10:
            parts.append(f"…（另有 {len(reasons) - 10} 行）")
    if summary:
        parts.append("--- 汇总 ---")
        parts.extend(list(dict.fromkeys(summary))[-3:])
    return _fit(parts, budget_chars)


def _summarize_git_diff(text: str, budget_chars: int) -> tuple[str, str] | None:
    """`git diff`：**改了哪些文件、各增删多少**，再给少量 hunk 头定位。

    逐行内容进上下文既贵又没用 —— 模型要决定的是"下一步看哪个文件的那一段"。
    """
    lines = text.splitlines()
    stats: dict[str, list[int]] = {}
    order: list[str] = []
    hunks: list[str] = []
    current: str | None = None
    for line in lines:
        match = _DIFF_FILE_RE.match(line)
        if match:
            current = match.group(2)
            if current not in stats:
                stats[current] = [0, 0]
                order.append(current)
            continue
        if line.startswith("@@"):
            if len(hunks) < 12:
                hunks.append(line.strip())
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            stats[current][0] += 1
        elif line.startswith("-") and not line.startswith("---"):
            stats[current][1] += 1

    if not order:
        return None  # 例如 `git diff --stat`，交给内容形态摘要

    added = sum(v[0] for v in stats.values())
    removed = sum(v[1] for v in stats.values())
    parts = [f"diff 改动 {len(order)} 个文件，共 +{added}/-{removed}"]
    for path in order[:25]:
        add, remove = stats[path]
        parts.append(f"  - {path}  (+{add}/-{remove})")
    if len(order) > 25:
        parts.append(f"…（另有 {len(order) - 25} 个文件）")
    if hunks:
        parts.append("--- 前几个改动位置（@@ 头，可据此用 read_file 定位）---")
        parts.extend(hunks)
    return _fit(parts, budget_chars)


def _summarize_git_status(text: str, budget_chars: int) -> tuple[str, str] | None:
    """`git status`：按状态归类计数 + 样例路径。"""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    buckets: dict[str, list[str]] = {}
    for line in lines:
        if line.startswith("##"):
            buckets.setdefault("分支", []).append(line[2:].strip())
            continue
        code = line[:2].strip() or "?"
        buckets.setdefault(code, []).append(line[3:].strip() if len(line) > 3 else line)
    parts = [f"git status 共 {len(lines)} 行，按状态归类："]
    for code, items in buckets.items():
        parts.append(f"  - [{code}] {len(items)} 项")
        parts.extend(f"      {item}" for item in items[:5])
        if len(items) > 5:
            parts.append(f"      …（另有 {len(items) - 5} 项）")
    return _fit(parts, budget_chars)


def _summarize_build(text: str, budget_chars: int) -> tuple[str, str] | None:
    """构建/编译：**错误行** + 末尾结论。告警不是这里的重点。"""
    lines = text.splitlines()
    errors = [
        (index + 1, line.strip())
        for index, line in enumerate(lines)
        if _BUILD_ERROR_RE.search(line)
    ]
    if not errors:
        return None  # 没有错误：交给通用摘要（可能是正常的构建日志）
    parts = [f"共 {len(lines)} 行构建输出；命中错误 {len(errors)} 条"]
    parts.append("--- 错误行 ---")
    parts.extend(f"[L{number}] {line}" for number, line in errors[:25])
    if len(errors) > 25:
        parts.append(f"…（另有 {len(errors) - 25} 条错误）")
    parts.append("--- 结尾 ---")
    parts.extend(lines[-3:])
    return _fit(parts, budget_chars)


def _summarize_install(text: str, budget_chars: int) -> tuple[str, str] | None:
    """依赖安装：结论行（成功/失败）+ 错误行。"""
    lines = text.splitlines()
    verdict = [
        line.strip()
        for line in lines
        if re.search(
            r"(?i)(successfully installed|already satisfied|no matching distribution"
            r"|ERROR:|Could not find|conflict|deprecated)",
            line,
        )
    ]
    errors = [
        line.strip()
        for line in lines
        if _BUILD_ERROR_RE.search(line) or "ERROR:" in line
    ]
    if not verdict and not errors:
        return None
    parts = [f"共 {len(lines)} 行安装输出"]
    if verdict:
        parts.append("--- 结论 ---")
        parts.extend(list(dict.fromkeys(verdict))[:8])
    if errors:
        parts.append("--- 错误 ---")
        parts.extend(list(dict.fromkeys(errors))[:12])
    return _fit(parts, budget_chars)


def _summarize_docker(text: str, budget_chars: int) -> tuple[str, str] | None:
    """`docker`/`kubectl`：**表头 + 前若干行**；日志类走错误行提取。"""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) < 8:
        return None
    header = lines[0]
    if "\t" in header or re.search(r"\s{2,}", header):
        # 类似 `CONTAINER ID   IMAGE   ...` / `NAME   READY   STATUS`
        parts = [f"共 {len(lines) - 1} 行数据（表头：{header.strip()[:120]}）"]
        parts.extend(lines[1:16])
        if len(lines) > 16:
            parts.append(f"…（另有 {len(lines) - 16} 行）")
        return _fit(parts, budget_chars)
    # 不是表格 ⇒ 当作日志（`docker logs` / `kubectl logs`）
    return _summarize_logs(text, budget_chars)


def _summarize_logs(text: str, budget_chars: int) -> tuple[str, str] | None:
    """日志：错误/告警行 + 首尾。时间戳密集时按顺序保留最早的几条与最后的几条。"""
    lines = text.splitlines()
    important = [
        (index + 1, line.rstrip())
        for index, line in enumerate(lines)
        if _IMPORTANT_LINE_RE.search(line)
    ]
    if not important:
        return None
    parts = [f"共 {len(lines)} 行日志；命中 error/warn 等 {len(important)} 条"]
    parts.append("--- 开头 ---")
    parts.extend(line.rstrip() for line in lines[:3])
    parts.append("--- 关键行 ---")
    parts.extend(f"[L{number}] {line}" for number, line in important[:20])
    if len(important) > 20:
        parts.append(f"…（另有 {len(important) - 20} 条关键行）")
    parts.append("--- 结尾 ---")
    parts.extend(line.rstrip() for line in lines[-5:])
    return _fit(parts, budget_chars)


#: 命令族 → 摘要器。返回 ``None`` 表示"该族不适用"，由 `_summarize_command` 回落。
_COMMAND_SUMMARIZERS = {
    COMMAND_TEST: _summarize_test,
    COMMAND_GIT_DIFF: _summarize_git_diff,
    COMMAND_GIT_STATUS: _summarize_git_status,
    COMMAND_BUILD: _summarize_build,
    COMMAND_INSTALL: _summarize_install,
    COMMAND_DOCKER: _summarize_docker,
    COMMAND_LOG: _summarize_logs,
}
