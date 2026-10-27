"""关键词检索与答案拼装（不依赖 LLM）。

分工：

1. :func:`keywords` 把用户问句切成关键词。中文没有分词依赖可用，这里做的是
   **2-gram 展开**：``"知识库怎么建"`` → ``知识 / 识库 / 库怎 / 怎么 / 么建``。配合
   ``ILIKE`` 能覆盖大部分"子串能命中"的问法，且不需要引入分词库。
2. SQL 侧只负责**取回候选**（ILIKE OR + LIMIT），排序与截断在
   :func:`rank_chunks` 里做纯 Python 打分，方便单测、也不用往 PG 里装扩展。
3. :func:`build_answer` 用命中的片段直接拼一段答案 —— 契约允许不调 LLM，因此这里
   只做"带编号的摘录"，不生成任何模型没说过的话。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

MAX_TERMS = 8
SNIPPET_CHARS = 200
ANSWER_CHARS = 1200
CANDIDATE_FACTOR = 20

# 英文/数字当作一个词；连续中文整段取出后再做 2-gram 展开
_SEGMENT = re.compile(r"[0-9A-Za-z_]+|[\u4e00-\u9fff]+")
_CJK_LONG = 4


def keywords(query: str, max_terms: int = MAX_TERMS) -> list[str]:
    """把问句切成去重后的关键词（保持出现顺序，最多 ``max_terms`` 个）。"""
    terms: list[str] = []
    for segment in _SEGMENT.findall(query or ""):
        if segment.isascii():
            term = segment.lower()
            if len(term) >= 2:
                terms.append(term)
            continue
        if len(segment) <= _CJK_LONG:
            terms.append(segment)
        else:
            terms.extend(segment[i : i + 2] for i in range(len(segment) - 1))

    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        unique.append(term)
        if len(unique) >= max_terms:
            break
    return unique


def hit_count(text: str, terms: Sequence[str]) -> int:
    """``terms`` 在 ``text`` 里出现的总次数（大小写不敏感，纯计数）。"""
    lowered = (text or "").lower()
    return sum(lowered.count(term.lower()) for term in terms if term)


def rank_chunks(rows: Iterable[Mapping[str, Any]], terms: Sequence[str], top_k: int = 5) -> list[dict[str, Any]]:
    """按命中次数给候选片段排序，返回前 ``top_k`` 条（同分保持原顺序）。

    命中 0 次的候选直接丢掉 —— SQL 的 ILIKE 只保证"至少一个词出现过"，但 Python 侧
    同样能看出来；这样调用方（含 FakeStore 之类的实现）即使放宽了 SQL 也不会得到噪音。
    """
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        text = str(row.get("content") or "")
        score = hit_count(text, terms)
        if score <= 0:
            continue
        scored.append(
            (
                -score,
                index,
                {
                    "doc_id": str(row.get("doc_id") or ""),
                    "chunk_id": str(row.get("id") or ""),
                    "seq": int(row.get("seq") or 0),
                    "content": text,
                    "score": score,
                },
            )
        )
    scored.sort(key=lambda item: (item[0], item[1]))
    limit = max(0, top_k)
    return [item[2] for item in scored[:limit]]


def clip(text: str, limit: int = SNIPPET_CHARS) -> str:
    """压成单行并截断（citation 的 ``snippet`` 字段用）。"""
    one_line = " ".join((text or "").split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"


def build_answer(hits: Sequence[Mapping[str, Any]], max_chars: int = ANSWER_CHARS) -> str:
    """把命中片段拼成带编号的摘录；没有命中就明确说没有。"""
    if not hits:
        return "本地知识库里没有检索到相关内容。"

    parts: list[str] = []
    used = 0
    for number, hit in enumerate(hits, start=1):
        body = " ".join(str(hit.get("content") or "").split())
        if not body:
            continue
        block = f"[{number}] {body}"
        if parts and used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    if not parts:
        return "本地知识库里没有检索到相关内容。"
    return "\n\n".join(parts)


def search_payload(rows: Iterable[Mapping[str, Any]], query: str, top_k: int = 5) -> dict[str, Any]:
    """检索结果 → ``{answer, citations}``。

    ``citations`` 的每项固定是 ``{doc_id, chunk_id, snippet}``（契约要求的字段名，
    不要私自加改名），``snippet`` 是压成单行后的摘录。
    """
    hits = rank_chunks(rows, keywords(query), top_k)
    citations = [
        {"doc_id": hit["doc_id"], "chunk_id": hit["chunk_id"], "snippet": clip(hit["content"])} for hit in hits
    ]
    return {"answer": build_answer(hits), "citations": citations}
