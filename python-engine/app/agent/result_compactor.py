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


def compact(text: str, *, budget_chars: int = 3000) -> Compaction:
    """按内容形态压缩，并返回**机器可读的裁决**（纯规则，不调 LLM）。"""
    size = len(text.encode("utf-8"))
    tier = classify(text)

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


def summarize(text: str, *, budget_chars: int = 3000) -> str:
    """按内容形态生成摘要（纯规则，不调 LLM）。

    只返回**文本**；需要裁决码（策略 / 是否裁剪）请用 `compact()`。
    """
    return compact(text, budget_chars=budget_chars).text


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
