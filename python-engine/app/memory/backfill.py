"""给迁移 `0019` 之前的老 `memory_summaries` 行补向量（M13 的存量回填，⑦）。

**为什么需要它**：迁移 `0019` 给 `memory_summaries` 加了 `embedding double precision[]`
（`0020` 又加了 `embedding_model` / `embedding_dim`），而**写入路径此后都带向量**
（`SummaryStore.save_summary` 的 INSERT 就有这三列）⇒ 只有**加列之前**写下的老行是 NULL，
语义判重与语义召回对它们没有输入。

**口径只认一处**：待嵌文本用 `SummaryEntry.embed_text`（`consolidator.py` 里那条注释记着教训
—— 曾经两处都自称"topics + 正文"，落库的却是两个**互不可比**的向量空间）。这里**不自己拼**，
构造 `SummaryEntry` 再读它的属性。

**默认 dry-run**：先报"有多少行缺向量"，人看过再决定真跑 —— 每条一次 embedding 调用是**花钱**的
（与"新增机制默认什么都不做"同一条纪律）。

**幂等**：只挑 `embedding IS NULL`；跑第二遍不写任何东西（除非又出现了新的 NULL 行）。
**不静默**：考虑了多少 / 补了多少 / 失败几条都回给调用方，失败逐条带 id。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.memory.layers import SummaryEntry

logger = logging.getLogger(__name__)

#: 一次跑多少条（默认）。每条一次 embedding 调用 ⇒ 这是**花钱的上限**，不是性能参数。
DEFAULT_LIMIT = 200


@dataclass
class BackfillResult:
    """一次回填的结果。`dry_run=True` 时只有 `considered` 有意义。"""

    considered: int = 0
    filled: int = 0
    failed: int = 0
    dry_run: bool = True
    errors: list[str] = field(default_factory=list)


async def backfill_summary_vectors(
    *,
    pool: Any,
    embedder: Any,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = True,
) -> BackfillResult:
    """给缺向量的老摘要补向量。返回处理计数（**不抛**：一条失败不该中断整批）。

    Args:
        pool: asyncpg 连接池（`fetch` / `execute`）。
        embedder: `async (text) -> list[float] | None` —— 与 `Consolidator` 同一个嵌入函数。
        limit: 本批最多处理多少条。
        dry_run: 只数不写（默认）。真跑要显式传 `False`。
    """
    result = BackfillResult(dry_run=dry_run)
    rows = await pool.fetch(
        """
        SELECT id, tenant_id, user_id, session_id, content, topics
          FROM memory_summaries
         WHERE embedding IS NULL
         ORDER BY created_at ASC
         LIMIT $1
        """,
        int(limit),
    )
    result.considered = len(rows)
    if dry_run or not rows:
        return result

    from app.memory.vector_backend import active_embedding_model

    for row in rows:
        entry = SummaryEntry(
            id=str(row["id"]),
            tenant_id=int(row["tenant_id"] or 0),
            user_id=str(row["user_id"]),
            session_id=str(row["session_id"] or ""),
            content=str(row["content"] or ""),
            # `topics` 是 jsonb：驱动可能给 list，也可能给 JSON 文本 ⇒ 两种都接住。
            topics=_topics_of(row["topics"]),
        )
        try:
            vector = await embedder(entry.embed_text)
        except Exception as exc:  # noqa: BLE001 — 嵌入失败按这条失败处理，继续下一条
            result.failed += 1
            result.errors.append(f"embed {entry.id}: {exc}")
            continue
        if not vector:
            # 嵌入函数说"这次算不出来"（网关不可用 / 未配）—— 不写 NULL 覆盖、不计成功。
            result.failed += 1
            result.errors.append(f"embed {entry.id}: empty vector")
            continue
        try:
            await pool.execute(
                """
                UPDATE memory_summaries
                   SET embedding = $1, embedding_model = $2, embedding_dim = $3
                 WHERE id = $4 AND embedding IS NULL
                """,
                list(vector),
                active_embedding_model(),
                len(vector),
                entry.id,
            )
            result.filled += 1
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            result.errors.append(f"update {entry.id}: {exc}")

    if result.failed:
        logger.warning(
            "summary vector backfill: %d filled, %d failed（前 3 条：%s）",
            result.filled,
            result.failed,
            "; ".join(result.errors[:3]),
        )
    return result


def _topics_of(raw: Any) -> list[str]:
    """`topics` 列是 jsonb：驱动可能已解成 list，也可能是 JSON 文本（或 None）。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str):
        import json

        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [str(t) for t in parsed] if isinstance(parsed, list) else []
    return []
