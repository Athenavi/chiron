"""Memory Service 门面 — 四层记忆架构统一入口。

分层与数据源：

- **L1 SessionMetaStore**：会话级簿记（``session_meta_store``，进程内存）。
- **L2 store（``user_memory_entries``）**：用户长期记忆条目，记忆页管理的对象。
  由 ``ProfileStore`` 提供；字段含 embedding / access_count / status，因此支持语义检索、
  命中即引用记账与归档。
- **L2 profile_card（``user_memory_profile``）**：用户个性化设置（提示词档案卡）。
  与记忆条目职责不同：前者是引擎提炼的偏好，后者是用户显式管理的长期记忆。
- **L3 summary_store + consolidator**：对话摘要与巩固 pipeline。

冲突裁决、智能整理与语义检索都在本层实现 —— 这几件事需要同时读 L2 条目与 L3 摘要，
放在 API 层会让「记忆页」与「引擎注入」两条链路各写一套口径。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.memory.layers import (
    SLOT_LABELS,
    EntryChannel,
    MemoryConflict,
    ProfileItem,
    ProfileUpdateResult,
    RecalledItem,
    RecallResult,
    Scope,
    SessionContext,
    SlotType,
    SourceType,
    cosine_similarity,
    recency_decay,
    rerank_score,
)
from app.memory.profile import new_entry_id
from app.memory.profile_card import ProfileCard
from app.memory.session_meta import SessionMetaStore

logger = logging.getLogger(__name__)

# 来源展示名。与前端 frontend-vue/src/api/memory.ts 的 source 取值一一对应。
SOURCE_LABELS: dict[str, str] = {
    "user_confirmed": "用户确认",
    "derived": "对话提炼",
    "tool_written": "工具写入",
}

# 语义检索的相似度下限。低于此值的命中是噪声 —— 宁可返回空，也不要「什么都能搜到」。
SIMILARITY_THRESHOLD = 0.25

# 近重复判定阈值：超过即认为两条记忆说的是同一件事。
NEAR_DUPLICATE_THRESHOLD = 0.95


def _iso(value: Any) -> str | None:
    """把 datetime / 时间戳统一成 ISO 字符串。

    前端直接对时间字段做 ``.slice(0, 10)`` 取日期，传 float 时间戳会在浏览器里抛
    TypeError —— 本模块所有出口统一走这里，就是为了不再出现这类"接口能返回但页面
    渲染即崩"的字段。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        iso: str = value.isoformat()
        return iso
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    return str(value)


def _slot_value(slot: Any) -> str:
    return slot.value if isinstance(slot, SlotType) else str(slot)


def _source_value(source: Any) -> str:
    return source.value if isinstance(source, SourceType) else str(source)


@dataclass
class OrganizeResult:
    """一次智能整理的产出统计。"""

    backfilled: int = 0  # 补齐向量
    merged: int = 0  # 近重复合并
    archived: int = 0  # 陈旧归档
    evicted: int = 0  # 超限淘汰
    errors: list[str] = field(default_factory=list)


class MemoryService:
    """四层记忆架构门面类。

    ``store`` / ``embedder`` 是 L2 记忆条目的读写与向量化入口；``profile_card``
    仅用于个性化设置的档案卡（``update_profile`` / ``forget``）。两者都可缺省 ——
    引擎在 PostgreSQL 或 Redis 不可用时会构造一个「只有部分能力」的实例，
    此时相关方法显式失败（fail loud），而不是静默返回空结果。
    """

    def __init__(
        self,
        store: Any = None,
        embedder: Any = None,
        summary_store: Any = None,
        consolidator: Any = None,
        session_meta_store: SessionMetaStore | None = None,
        producer: Any = None,
        profile_card: ProfileCard | None = None,
        conflict_manager: Any = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._summary_store = summary_store
        self._consolidator = consolidator
        self._session_meta = session_meta_store
        self._producer = producer
        self._profile_card = profile_card
        self._conflict_manager = conflict_manager or getattr(
            profile_card, "_conflict_manager", None
        )
        # 待裁决冲突：冲突是「用户确认过的值被派生值覆盖」的待办事项，生命周期短
        # （要么被裁决要么被否认），进程内保存足够。多副本下各实例只看见自己产生的
        # 冲突 —— 裁决入口本就由产生它的那次请求所属实例服务。
        self._conflicts: dict[str, dict[str, Any]] = {}
        # 整理任务的单飞行状态：同一 (tenant, user) 不允许并发整理。
        self._organize_state: dict[tuple[str, str], dict[str, Any]] = {}
        self._organize_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}

    # ── 生命周期钩子 ──────────────────────────────────────────────────

    async def on_session_start(
        self,
        session_id: str,
        tenant_id: str,
        user_id: str,
        entry_channel: EntryChannel = "web",
        mode: str = "agent",
    ) -> SessionContext:
        """会话开始时调用：建 L1 簿记 + 预取 L2 个性化档案卡。"""
        meta = None
        if self._session_meta is not None:
            meta = self._session_meta.create(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                entry_channel=entry_channel,
                mode=mode,
            )

        profile_cached = False
        if self._profile_card is not None:
            try:
                items = await self._profile_card.get_profile(tenant_id, user_id)
                profile_cached = len(items) > 0
            except Exception as e:  # 档案卡不可用不该阻断会话建立
                logger.warning("L2 profile prefetch failed: %s", e)

        logger.info(
            "Session started: %s (user=%s, tenant=%s, profile_cached=%s)",
            session_id,
            user_id,
            tenant_id,
            profile_cached,
        )
        return SessionContext(
            meta=meta,
            profile_cached=profile_cached,
            summaries_prefetched=0,
        )

    async def on_turn_complete(
        self,
        session_id: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        total_tokens: int = 0,
        max_tokens: int = 8192,
    ) -> None:
        """回合完成时调用：L1 记账 + token 预算检测 + L3 异步入队。"""
        store = self._session_meta
        meta = store.get(session_id) if store else None
        if meta and store is not None:
            meta.mark_turn_complete(tokens_in, tokens_out)
            store.update(session_id)

        if meta and total_tokens > 0 and max_tokens > 0:
            usage_ratio = total_tokens / max_tokens
            if usage_ratio >= 0.8:
                logger.info(
                    "Token budget reached %.0f%%, triggering compaction for session=%s",
                    usage_ratio * 100,
                    session_id,
                )
                meta.mark_degraded("compaction_triggered")
                if store is not None:
                    store.update(session_id)

        if meta and self._producer:
            await self._enqueue_consolidate(
                tenant_id=meta.tenant_id,
                user_id=meta.user_id,
                session_id=session_id,
                turn_count=meta.turn_count,
            )

        logger.debug(
            "Turn completed: session=%s, tokens_in=%d, tokens_out=%d",
            session_id,
            tokens_in,
            tokens_out,
        )

    async def on_session_end(self, session_id: str) -> None:
        """会话结束时调用：L3 会话级 rollup 入队 + 丢弃 L1 簿记。"""
        meta = self._session_meta.get(session_id) if self._session_meta else None
        if meta and self._producer:
            await self._enqueue_rollup(
                tenant_id=meta.tenant_id,
                user_id=meta.user_id,
                session_id=session_id,
            )
        if self._session_meta is not None:
            self._session_meta.delete(session_id)
        logger.info("Session ended: %s", session_id)

    # ── L2 记忆条目 ──────────────────────────────────────────────────

    def _require_store(self) -> Any:
        if self._store is None:
            raise RuntimeError("L2 memory store unavailable (PostgreSQL required)")
        return self._store

    def _require_profile_card(self) -> Any:
        if self._profile_card is None:
            raise RuntimeError("L2 profile card unavailable (PostgreSQL required)")
        return self._profile_card

    async def _embed(self, text: str) -> list[float] | None:
        """生成向量，失败返回 None（fail-soft）。

        向量只影响检索排序，不该因为嵌入服务抖动就阻断写入 —— 条目照样入库，
        整理时再补齐（见 ``organize_now`` 的 backfilled）。
        """
        if self._embedder is None or not text.strip():
            return None
        try:
            embedding: list[float] | None = await self._embedder(text)
            return embedding
        except Exception as e:
            logger.warning("embed failed (entry saved without vector): %s", e)
            return None

    @staticmethod
    def _entry_dict(entry: Any) -> dict[str, Any]:
        """MemoryEntry → API 契约（字段名与前端 MemoryEntry 类型一一对应）。"""
        slot = _slot_value(entry.slot)
        source = _source_value(entry.source)
        return {
            "id": entry.id,
            "slot": slot,
            "slot_label": SLOT_LABELS.get(slot, slot),
            "key": entry.item_key,
            "value": entry.item_value,
            "confidence": entry.confidence,
            "source": source,
            "source_label": SOURCE_LABELS.get(source, source),
            "has_embedding": entry.embedding is not None,
            "access_count": entry.access_count,
            "last_accessed_at": _iso(entry.last_accessed_at),
            "status": entry.status,
            "created_at": _iso(entry.created_at),
            "updated_at": _iso(entry.updated_at),
        }

    async def list_entries(
        self,
        tenant_id: str,
        user_id: str,
        include_archived: bool = False,
        slot: str | None = None,
    ) -> dict[str, Any]:
        """列出记忆条目，并按槽位给出计数（前端 tab 计数依赖 counts）。"""
        store = self._require_store()
        items = await store.list(
            tenant_id, user_id, include_archived=include_archived, slot=slot
        )
        counts: dict[str, int] = {s: 0 for s in SLOT_LABELS}
        for item in items:
            key = _slot_value(item.slot)
            counts[key] = counts.get(key, 0) + 1
        return {
            "entries": [self._entry_dict(i) for i in items],
            "counts": counts,
            "total": len(items),
        }

    async def upsert(
        self,
        tenant_id: str,
        user_id: str,
        slot: str,
        key: str,
        value: str,
        confidence: int = 50,
        source: str = "user_confirmed",
    ) -> dict[str, Any]:
        """创建 / 更新一条 L2 记忆条目。

        返回 ``{created, entry, conflict?, evicted?, duplicate_of?}``：

        - ``conflict``：旧的用户确认值将被派生值覆盖 —— 不直接覆盖，而是登记待裁决。
        - ``evicted``：触达条目上限时淘汰的低价值条目数。
        - ``duplicate_of``：命中的近重复条目（仅提示，不自动合并；合并交给智能整理）。
        """
        try:
            slot_val = SlotType(slot).value
        except ValueError:
            raise ValueError(f"invalid slot: {slot}") from None
        try:
            source_val = SourceType(source).value
        except ValueError:
            raise ValueError(f"invalid source: {source}") from None

        item_key = (key or "").strip()
        if not item_key:
            raise ValueError("key required")
        item_value = "" if value is None else str(value)
        if not item_value.strip():
            raise ValueError("value required")
        confidence = max(0, min(100, int(confidence)))

        store = self._require_store()
        existing = await store.get_by_key(tenant_id, user_id, slot_val, item_key)

        # 冲突：已确认的值被派生值覆盖。派生值再准也不该悄悄推翻用户确认过的东西，
        # 所以先登记，等人在记忆页裁决（keep_old / adopt_new / manual）。
        conflict: dict[str, Any] | None = None
        if (
            existing is not None
            and _source_value(existing.source) == "user_confirmed"
            and source_val != "user_confirmed"
        ):
            conflict = await self._register_conflict(
                tenant_id=tenant_id,
                user_id=user_id,
                slot=slot_val,
                key=item_key,
                old_value=existing.item_value,
                new_value=item_value,
                source=source_val,
            )

        embedding = await self._embed(f"{item_key}: {item_value}")

        # 近重复提示：写之前先找一次，避免同一件事被记两遍。
        duplicate_of = None
        if embedding:
            duplicate_of = await self._find_near_duplicate(
                tenant_id, user_id, embedding, exclude_key=item_key, slot=slot_val
            )

        entry = await store.insert(
            _new_memory_entry(
                tenant_id=tenant_id,
                user_id=user_id,
                slot=slot_val,
                key=item_key,
                value=item_value,
                confidence=confidence,
                source=source_val,
                embedding=embedding,
            )
        )

        resp: dict[str, Any] = {
            "created": existing is None,
            "entry": self._entry_dict(entry),
        }
        if conflict is not None:
            resp["conflict"] = conflict
        if duplicate_of is not None:
            resp["duplicate_of"] = self._entry_dict(duplicate_of)

        evicted = await self._evict_over_limit(tenant_id, user_id)
        if evicted:
            resp["evicted"] = evicted

        return resp

    async def update_entry(
        self,
        tenant_id: str,
        user_id: str,
        entry_id: str,
        key: str | None = None,
        value: str | None = None,
        confidence: int | None = None,
        source: str | None = None,
    ) -> Any | None:
        """按 id 局部更新（仅更新显式传入的字段）。"""
        store = self._require_store()
        source_val = None
        if source is not None:
            try:
                source_val = SourceType(source).value
            except ValueError:
                raise ValueError(f"invalid source: {source}") from None
        if confidence is not None:
            confidence = max(0, min(100, int(confidence)))

        # 内容变了必须重算向量，否则检索会拿旧向量去匹配新文本。
        embedding_set = False
        embedding = None
        if value is not None:
            target = await store.get_by_id(tenant_id, user_id, entry_id)
            if target is None:
                return None
            embedding = await self._embed(f"{key or target.item_key}: {value}")
            embedding_set = True

        return await store.update(
            tenant_id,
            user_id,
            entry_id,
            item_key=key,
            item_value=value,
            confidence=confidence,
            source=source_val,
            embedding=embedding,
            embedding_set=embedding_set,
        )

    async def delete_entry(self, tenant_id: str, user_id: str, entry_id: str) -> bool:
        """按 id 删除单条记忆。"""
        removed: bool = await self._require_store().delete(
            tenant_id, user_id, entry_id
        )
        return removed

    async def forget_by_key(
        self,
        tenant_id: str,
        user_id: str,
        item_key: str,
        slot: str | None = None,
    ) -> int:
        """按 key 删除（可限定槽位）；返回删除条数。"""
        removed: int = await self._require_store().delete_by_key(
            tenant_id, user_id, item_key, slot
        )
        return removed

    async def clear_all(self, tenant_id: str, user_id: str) -> int:
        """清空当前用户全部记忆（隐私出口）。"""
        removed: int = await self._require_store().delete_all(tenant_id, user_id)
        return removed

    async def search(
        self,
        tenant_id: str,
        user_id: str,
        query: str,
        top_k: int = 10,
        slot: str | None = None,
    ) -> dict[str, Any]:
        """检索记忆：L2 条目 + L3 摘要两个独立结果区。

        ``results`` 是 L2 记忆条目（优先语义匹配，嵌入不可用时降级关键词）；
        ``summaries`` 是同一 query 在对话摘要里的命中。``mode`` 让前端如实标注
        本次是语义还是关键词匹配 —— 两者结果质量差别很大，不该让用户以为一直是
        语义检索。``count`` 只计 L2 命中（``summary_count`` 单列 L3）。
        """
        q = (query or "").strip()
        if not q:
            raise ValueError("query is required")
        store = self._require_store()
        items = await store.list(tenant_id, user_id, slot=slot)

        qvec = await self._embed(q)
        hits: list[tuple[Any, float, float]] = []
        mode = "keyword"

        if qvec:
            mode = "semantic"
            for item in items:
                if not item.embedding:
                    continue
                sim = cosine_similarity(qvec, item.embedding)
                if sim < SIMILARITY_THRESHOLD:
                    continue
                score = rerank_score(
                    sim,
                    item.confidence,
                    item.last_accessed_at,
                    item.status != "active",
                )
                hits.append((item, sim, score))
            hits.sort(key=lambda t: -t[2])
        else:
            needle = q.lower()
            for item in items:
                hay = f"{item.item_key} {item.item_value}".lower()
                if needle in hay:
                    hits.append((item, 0.0, float(item.confidence)))

        hits = hits[: max(1, int(top_k))]

        # 命中即引用：被检索到的条目记账，供整理判断「哪些记忆真的有用」。
        hit_ids = [h[0].id for h in hits]
        if hit_ids:
            try:
                await store.touch(hit_ids)
            except Exception as e:  # 记账失败不影响本次结果
                logger.warning("memory touch failed: %s", e)

        results = []
        for item, sim, score in hits:
            payload = self._entry_dict(item)
            payload["similarity"] = round(float(sim), 4)
            payload["score"] = round(float(score), 4)
            results.append(payload)

        # L3 区：「我记下的条目」与「历史对话片段」是两种东西，混在一个列表里
        # 用户无法判断某条到底来自哪里，也就无从决定该编辑还是该忽略。
        summaries: list[dict[str, Any]] = []
        if self._summary_store is not None:
            try:
                data = await self.recall_summaries(tenant_id, user_id, q, top_k=top_k)
                summaries = data["summaries"]
            except Exception as e:  # L3 是增强项，失败不该让整个检索失败
                logger.warning("L3 search failed (fail-soft): %s", e)

        return {
            "query": q,
            "mode": mode,
            "count": len(results),
            "results": results,
            "summaries": summaries,
            "summary_count": len(summaries),
        }

    # ── L2 个性化设置档案卡（user_memory_profile）───────────────────

    async def update_profile(
        self,
        tenant_id: str,
        user_id: str,
        slot: SlotType,
        item_key: str,
        item_value: Any,
        confidence: int = 50,
        source: SourceType = SourceType.DERIVED,
    ) -> ProfileUpdateResult:
        """更新用户个性化设置（档案卡）。"""
        card = self._require_profile_card()
        result: ProfileUpdateResult = await card.upsert_item(
            tenant_id=tenant_id,
            user_id=user_id,
            slot=slot,
            item_key=item_key,
            item_value=item_value,
            confidence=confidence,
            source=source,
        )
        return result

    async def forget(
        self,
        tenant_id: str,
        user_id: str,
        slot: SlotType,
        item_key: str,
    ) -> bool:
        """删除个性化设置条目。"""
        card = self._require_profile_card()
        removed: bool = await card.delete_item(
            tenant_id=tenant_id,
            user_id=user_id,
            slot=slot,
            item_key=item_key,
        )
        return removed

    # ── L3 摘要 ─────────────────────────────────────────────────────

    async def save_summary(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
        turn_start: int = 0,
        turn_end: int = 0,
    ) -> dict[str, Any]:
        """把一批消息巩固为一条 L3 摘要。

        没有 consolidator 时 **fail loud**：静默丢弃摘要会让「记忆为什么没生效」
        变成一个查不出来的问题（此前装配串位时正是如此）。
        """
        if self._consolidator is None:
            raise RuntimeError("consolidator not bound; cannot save summary")
        result = await self._consolidator.consolidate(
            tenant_id, user_id, session_id, messages, turn_start, turn_end
        )
        return {
            "error": result.error or None,
            "summary": result.summary.to_dict() if result.summary else None,
            "deduplicated": result.deduplicated,
        }

    async def list_summaries(
        self,
        tenant_id: str,
        user_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        """列出 L3 摘要（记忆页「摘要」区）。"""
        if self._summary_store is None:
            return {"summaries": [], "count": 0}
        items = await self._summary_store.list_active(tenant_id, user_id, limit)
        return {
            "summaries": [s.to_dict() for s in items],
            "count": len(items),
        }

    async def recall_summaries(
        self,
        tenant_id: str,
        user_id: str,
        query: str,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """在 L3 摘要中做语义检索。

        优先委托摘要库自己的 ``recall``（真实实现带查询缓存与向量检索）；实现没有
        这个能力时（例如内存替身）退化为「取最近 + 本层算相似度」。两条路径返回同一
        形状，调用方不必区分自己拿到的是哪一种。
        """
        store = self._summary_store
        if store is None:
            return {"summaries": [], "count": 0, "mode": "empty"}

        q = (query or "").strip()

        delegated = getattr(store, "recall", None)
        if delegated is not None:
            scope = Scope(tenant_id=tenant_id, user_id=user_id, session_id="")
            raw = await delegated(scope=scope, query=q, top_k=top_k)
            if not raw:
                return {"summaries": [], "count": 0, "mode": "empty"}

            def _as_dict(item: Any) -> dict[str, Any]:
                """RecalledItem → 与 ``SummaryEntry.to_dict`` 同形的字典。"""
                return {
                    "id": getattr(item, "id", ""),
                    "session_id": getattr(item, "session_id", ""),
                    "content": getattr(item, "content", ""),
                    "topics": list(getattr(item, "topics", None) or []),
                    "entities": dict(getattr(item, "entities", None) or {}),
                    "turn_range": list(getattr(item, "turn_range", (0, 0))),
                    "access_count": getattr(item, "access_count", 0),
                    "last_accessed_at": getattr(item, "last_accessed_at", None),
                    "created_at": getattr(item, "created_at", 0.0),
                    "has_embedding": getattr(item, "embedding", None) is not None,
                    "status": getattr(item, "status", "active"),
                }

            return {
                "summaries": [_as_dict(i) for i in raw],
                "count": len(raw),
                "mode": "semantic",
            }

        items = await store.list_active(tenant_id, user_id, 200)
        if not items:
            return {"summaries": [], "count": 0, "mode": "empty"}

        ordered: list[Any] = []
        if not q:
            ordered = items[:top_k]
            mode = "recent"
        else:
            qvec = await self._embed(q)
            if qvec:
                mode = "semantic"
                scored = []
                for item in items:
                    if not item.embedding:
                        continue
                    sim = cosine_similarity(qvec, item.embedding)
                    if sim < SIMILARITY_THRESHOLD:
                        continue
                    scored.append((item, sim))
                scored.sort(key=lambda t: -t[1])
                ordered = [s[0] for s in scored[:top_k]]
            else:
                mode = "keyword"
                needle = q.lower()
                ordered = [
                    i for i in items
                    if needle in f"{i.content} {' '.join(i.topics or [])}".lower()
                ][:top_k]

        summaries = [item.to_dict() for item in ordered]
        if ordered:
            try:
                for item in ordered:
                    await self._summary_store.touch(item.id)
            except Exception as e:
                logger.warning("summary touch failed: %s", e)
        return {"summaries": summaries, "count": len(summaries), "mode": mode}

    async def recall(
        self,
        tenant_id: str,
        user_id: str,
        query: str = "",
        top_k: int = 5,
        exclude_turn_range: tuple[int, int] | None = None,
        slots: list[str] | None = None,
    ) -> RecallResult:
        """召回记忆（L2 条目 + L3 摘要），供提示词注入。

        L2 与 L3 都 fail-soft：记忆是增强项，不该因为它挂掉就阻断整轮对话。
        """
        # ── L2 ──
        profile_items: list[Any] = []
        try:
            if self._store is not None:
                profile_items = await self._store.list(tenant_id, user_id)
                if slots:
                    # 按分类收窄：用户在对话里显式表达了「只带上偏好」这类意图
                    wanted = {str(s) for s in slots}
                    profile_items = [
                        i for i in profile_items if _slot_value(i.slot) in wanted
                    ]
        except Exception as e:
            logger.warning("L2 recall failed: %s", e)

        # 个性化设置（user_memory_profile）与长期记忆（user_memory_entries）都要参与
        # 注入：前者是引擎提炼的稳定偏好，后者是用户显式管理的记忆，对一轮对话的
        # 价值是同等的。ProfileItem 与 MemoryEntry 的字段名一致（slot / item_key /
        # item_value / confidence / source），因此可以共用同一个序列化器。
        if self._profile_card is not None:
            try:
                profile_items += await self._profile_card.get_profile(
                    tenant_id, user_id
                )
            except Exception as e:
                logger.warning("L2 profile-card recall failed: %s", e)

        profile_block = self._serialize_entries(profile_items)

        # ── L3 ──
        summary_items: list[RecalledItem] = []
        if self._summary_store is not None and query:
            try:
                data = await self.recall_summaries(
                    tenant_id, user_id, query, top_k=top_k
                )
                for raw in data["summaries"]:
                    turn_range = tuple(raw.get("turn_range") or (0, 0))
                    if exclude_turn_range and self._is_turn_range_overlap(
                        turn_range, exclude_turn_range
                    ):
                        continue
                    summary_items.append(
                        RecalledItem(
                            id=raw.get("id", ""),
                            content=raw.get("content", ""),
                            topics=list(raw.get("topics") or []),
                            entities=dict(raw.get("entities") or {}),
                            turn_range=turn_range,
                            session_id=raw.get("session_id", ""),
                            access_count=raw.get("access_count", 0),
                            last_accessed_at=raw.get("last_accessed_at") or 0.0,
                            created_at=raw.get("created_at") or 0.0,
                            score=0.0,
                        )
                    )
            except Exception as e:
                logger.warning("L3 recall failed (fail-soft, empty L3): %s", e)
                summary_items = []

        return RecallResult(
            profile_block=profile_block,
            summary_items=summary_items,
        )

    @staticmethod
    def _is_turn_range_overlap(
        range_a: tuple[int, int],
        range_b: tuple[int, int],
    ) -> bool:
        """判断两个 turn_range 是否重叠。"""
        start_a, end_a = range_a
        start_b, end_b = range_b
        return start_a <= end_b and start_b <= end_a

    # ── 冲突裁决 ─────────────────────────────────────────────────────

    async def _register_conflict(
        self,
        tenant_id: str,
        user_id: str,
        slot: str,
        key: str,
        old_value: Any,
        new_value: Any,
        source: str,
        conflict_id: str | None = None,
    ) -> dict[str, Any]:
        """登记待裁决冲突：先落 Redis（多副本共享），失败则只留进程内。

        进程内那份不是缓存，而是**降级路径** —— Redis 不可用或未配置时，
        当前实例至少仍能看见并裁决自己产生的冲突，而不是整块能力消失。
        """
        cid = conflict_id or f"cfl_{int(time.time() * 1000)}_{len(self._conflicts)}"
        record = {
            "conflict_id": cid,
            "slot": slot,
            "key": key,
            "old_value": old_value,
            "new_value": new_value,
            "source": source,
            "status": "pending",
            "tenant_id": tenant_id,
            "user_id": user_id,
            "created_at": time.time(),
        }
        self._conflicts[cid] = record
        # persisted=False 表示「只存在于本副本」（Redis 未配置或写失败）。
        # 它是 list_conflicts_shared 判断「本地这份要不要补进结果」的依据 ——
        # 已落盘的以 Redis 为准，否则别的副本裁决后本副本仍会当作待办展示。
        record["persisted"] = False
        if self._conflict_manager is not None:
            try:
                persisted = await self._conflict_manager.register_conflict(
                    MemoryConflict(
                        conflict_id=cid,
                        slot=SlotType(slot),
                        item_key=key,
                        old_value=old_value,
                        new_value=new_value,
                        source=SourceType(source),
                        tenant_id=tenant_id,
                        user_id=user_id,
                        created_at=record["created_at"],
                    )
                )
                record["persisted"] = bool(persisted)
            except Exception as e:
                logger.warning(
                    "Failed to persist conflict %s to Redis (in-process only): %s",
                    cid,
                    e,
                )
        return record

    async def list_conflicts_shared(
        self,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """待裁决冲突的**跨副本**视图：以 Redis 为准，并入本地未落盘的部分。

        ``list_conflicts`` 保持同步（纯读进程内视图）；需要跨副本一致性的调用方
        （API 层，本身就是 async）用这个方法。
        """
        if self._conflict_manager is None:
            return self.list_conflicts(tenant_id, user_id)
        try:
            remote = await self._conflict_manager.get_pending_conflicts(
                tenant_id, user_id
            )
        except Exception as e:
            logger.warning(
                "Redis conflict list failed, falling back to local view: %s", e
            )
            return self.list_conflicts(tenant_id, user_id)

        merged: dict[str, dict[str, Any]] = {
            c.conflict_id: {
                "conflict_id": c.conflict_id,
                "slot": c.slot.value,
                "key": c.item_key,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "source": c.source.value,
                "status": "pending",
                "tenant_id": c.tenant_id,
                "user_id": c.user_id,
                "created_at": c.created_at,
            }
            for c in remote
        }
        # 只补「没能落到共享存储」的那些（persisted=False）。已落盘的一律以 Redis
        # 为准 —— 否则别的副本裁决/忽略之后，本副本仍会把它当作待办展示，
        # 而用户在该副本上无论怎么点都不会让它消失。
        for local in self.list_conflicts(tenant_id, user_id):
            if not local.get("persisted"):
                merged.setdefault(local["conflict_id"], local)
        return list(merged.values())

    async def _load_conflict(self, conflict_id: str) -> dict[str, Any] | None:
        """从 Redis 取回冲突（可能是其他副本登记的）。"""
        if self._conflict_manager is None:
            return None
        try:
            c = await self._conflict_manager.get_conflict(conflict_id)
        except Exception as e:
            logger.warning("Failed to load conflict %s from Redis: %s", conflict_id, e)
            return None
        if c is None:
            return None
        return {
            "conflict_id": c.conflict_id,
            "slot": c.slot.value,
            "key": c.item_key,
            "old_value": c.old_value,
            "new_value": c.new_value,
            "source": c.source.value,
            "status": "pending",
            "tenant_id": c.tenant_id,
            "user_id": c.user_id,
            "created_at": c.created_at,
        }

    def list_conflicts(
        self,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """列出待裁决的记忆冲突（按租户/用户隔离）。

        同步方法：冲突存在进程内的映射里，没有任何 IO 可等待。写成 async 只会让
        调用方多写一个 await —— 漏掉时得到的是「coroutine 没有 len()」这种与语义
        毫不相干的报错。
        """
        return [
            c for c in self._conflicts.values()
            if c["status"] == "pending"
            and c["tenant_id"] == tenant_id
            and c["user_id"] == user_id
        ]

    async def resolve_conflict(
        self,
        conflict_id: str,
        resolution: str,
        manual_value: Any = None,
    ) -> dict[str, Any]:
        """裁决冲突：``keep_old`` / ``adopt_new``（``use_new`` 同义）/ ``manual``。

        裁决后把结果写回 L2，并把该值提升为「用户确认」—— 人已经看过并拍板，
        再让后续派生值悄悄覆盖它就没有意义了。
        """
        # 兼容两种命名：早期 API 用 adopt_new，前端曾用 use_new。
        if resolution == "use_new":
            resolution = "adopt_new"
        if resolution not in ("keep_old", "adopt_new", "manual"):
            raise ValueError("invalid resolution: must be keep_old, adopt_new or manual")

        record = self._conflicts.get(conflict_id)
        if record is None and self._conflict_manager is not None:
            # 可能是别的副本登记的 —— 取回来，否则用户在这个副本上裁决不了
            record = await self._load_conflict(conflict_id)
        if record is None:
            raise ValueError(f"conflict not found: {conflict_id}")

        store = self._require_store()
        if resolution == "keep_old":
            resolved_value = record["old_value"]
        elif resolution == "adopt_new":
            resolved_value = record["new_value"]
        else:
            resolved_value = manual_value

        target = await store.get_by_key(
            record["tenant_id"], record["user_id"], record["slot"], record["key"]
        )
        if target is not None:
            embedding = await self._embed(f"{record['key']}: {resolved_value}")
            await store.update(
                record["tenant_id"],
                record["user_id"],
                target.id,
                item_value=resolved_value,
                confidence=100,
                source="user_confirmed",
                embedding=embedding,
                embedding_set=True,
            )

        record["status"] = "resolved"
        record["resolution"] = resolution
        record["resolved_value"] = resolved_value
        self._conflicts.pop(conflict_id, None)

        # 从共享存储摘除，否则其他副本仍会把它当作待裁决项返回。
        # ConflictManager 只认 keep_old / use_new / manual（它没有 adopt_new）。
        if self._conflict_manager is not None:
            try:
                await self._conflict_manager.resolve_conflict(
                    conflict_id,
                    "use_new" if resolution == "adopt_new" else resolution,
                    manual_value,
                )
            except Exception as e:
                logger.warning(
                    "Failed to drop conflict %s from Redis: %s", conflict_id, e
                )

        logger.info("Conflict %s resolved: %s", conflict_id, resolution)
        return {
            "conflict_id": conflict_id,
            "status": "resolved",
            "resolution": resolution,
            "resolved_value": resolved_value,
            "slot": record["slot"],
            "key": record["key"],
        }

    async def delete_conflict(self, conflict_id: str) -> bool:
        """用户否认该冲突（不改变记忆内容）。"""
        local = self._conflicts.pop(conflict_id, None) is not None
        if self._conflict_manager is not None:
            try:
                remote = await self._conflict_manager.delete_conflict(conflict_id)
                return bool(remote or local)
            except Exception as e:
                logger.warning(
                    "Failed to drop conflict %s from Redis: %s", conflict_id, e
                )
        return local

    # ── 智能整理 ─────────────────────────────────────────────────────

    async def organize_now(self, tenant_id: str, user_id: str) -> OrganizeResult:
        """同步执行一次整理：补向量 → 合并近重复 → 归档陈旧 → 淘汰超限。

        顺序有意如此：先补向量，近重复才可能被识别出来（无向量的条目无法比较）。
        """
        store = self._require_store()
        result = OrganizeResult()
        items = await store.list(tenant_id, user_id, include_archived=False)

        # ① 补向量
        for item in items:
            if item.embedding:
                continue
            vec = await self._embed(f"{item.item_key}: {item.item_value}")
            if vec is None:
                continue
            try:
                await store.update(
                    tenant_id, user_id, item.id,
                    embedding=vec, embedding_set=True,
                )
                item.embedding = vec
                result.backfilled += 1
            except Exception as e:
                result.errors.append(f"backfill {item.id}: {e}")

        # ② 合并近重复：保留置信度高的那条
        survivors: list[Any] = []
        for item in items:
            if not item.embedding:
                survivors.append(item)
                continue
            duplicate_of = None
            for kept in survivors:
                if not kept.embedding:
                    continue
                if cosine_similarity(item.embedding, kept.embedding) <= NEAR_DUPLICATE_THRESHOLD:
                    continue
                duplicate_of = kept
                break
            if duplicate_of is None:
                survivors.append(item)
                continue
            # 置信度高的留下；两者相同时保留先入库的那条（更早的引用更多）
            keeper, victim = (
                (item, duplicate_of)
                if item.confidence > duplicate_of.confidence
                else (duplicate_of, item)
            )
            try:
                await store.delete(tenant_id, user_id, victim.id)
                result.merged += 1
            except Exception as e:
                result.errors.append(f"merge {victim.id}: {e}")
                continue
            if keeper is item:
                survivors = [s for s in survivors if s is not duplicate_of]
                survivors.append(item)

        # ③ 归档陈旧低置信条目
        now = datetime.now(UTC)
        for item in survivors:
            if item.confidence >= 60:
                continue
            ref = item.last_accessed_at or item.updated_at or item.created_at
            if not isinstance(ref, datetime):
                continue
            if ref.tzinfo is None:
                ref = ref.replace(tzinfo=UTC)
            if (now - ref).days <= settings.memory_archive_days:
                continue
            try:
                await store.archive(item.id)
                result.archived += 1
            except Exception as e:
                result.errors.append(f"archive {item.id}: {e}")

        # ④ 淘汰超限
        result.evicted = await self._evict_over_limit(tenant_id, user_id)
        return result

    async def start_organize(
        self,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """异步触发整理（单飞行：同一用户并发触发时返回 already_running）。"""
        key = (tenant_id, user_id)
        task = self._organize_tasks.get(key)
        if task is not None and not task.done():
            return {
                "started": False,
                "reason": "already_running",
                "status": self.organize_status(tenant_id, user_id),
            }

        self._organize_state[key] = {
            "running": True,
            "started_at": time.time(),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        self._organize_tasks[key] = asyncio.create_task(
            self._run_organize(tenant_id, user_id)
        )
        return {
            "started": True,
            "status": self.organize_status(tenant_id, user_id),
        }

    async def _run_organize(self, tenant_id: str, user_id: str) -> None:
        key = (tenant_id, user_id)
        state = self._organize_state.setdefault(key, {})
        try:
            result = await self.organize_now(tenant_id, user_id)
            state.update(
                {
                    "running": False,
                    "finished_at": time.time(),
                    "result": {
                        "backfilled": result.backfilled,
                        "merged": result.merged,
                        "archived": result.archived,
                        "evicted": result.evicted,
                        "errors": result.errors,
                    },
                }
            )
        except Exception as e:
            logger.error("organize failed: %s", e)
            state.update(
                {"running": False, "finished_at": time.time(), "error": str(e)}
            )

    def organize_status(self, tenant_id: str, user_id: str) -> dict[str, Any]:
        """整理任务状态（从未运行过时返回空态）。"""
        state = self._organize_state.get((tenant_id, user_id), {})
        return {
            "running": bool(state.get("running", False)),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "result": state.get("result"),
            "error": state.get("error"),
        }

    # ── 辅助 ─────────────────────────────────────────────────────────

    async def _find_near_duplicate(
        self,
        tenant_id: str,
        user_id: str,
        embedding: list[float],
        exclude_key: str | None = None,
        slot: str | None = None,
    ) -> Any | None:
        """在既有条目中查找近重复（cosine > NEAR_DUPLICATE_THRESHOLD）。"""
        try:
            items = await self._require_store().list(
                tenant_id, user_id, include_archived=False, slot=slot
            )
        except Exception:
            return None
        for item in items:
            if exclude_key is not None and item.item_key == exclude_key:
                continue
            if not item.embedding:
                continue
            if cosine_similarity(embedding, item.embedding) > NEAR_DUPLICATE_THRESHOLD:
                return item
        return None

    async def _evict_over_limit(self, tenant_id: str, user_id: str) -> int:
        """超过条目上限时淘汰：优先淘汰「派生 + 低置信 + 久未引用」的条目。

        用户确认过的条目（source=user_confirmed）最后才动 —— 它们是人明确要求记住的。
        """
        store = self._require_store()
        limit = int(getattr(settings, "memory_profile_max_items", 0) or 0)
        if limit <= 0:
            return 0
        items = await store.list(tenant_id, user_id, include_archived=False)
        overflow = len(items) - limit
        if overflow <= 0:
            return 0

        def eviction_rank(item: Any) -> tuple[Any, ...]:
            confirmed = 1 if _source_value(item.source) == "user_confirmed" else 0
            return (
                confirmed,                              # 确认过的排最后
                item.confidence,                        # 低置信的先走
                recency_decay(item.last_accessed_at),   # 久未引用的先走
            )

        victims = sorted(items, key=eviction_rank)[:overflow]
        evicted = 0
        for victim in victims:
            try:
                await store.delete(tenant_id, user_id, victim.id)
                evicted += 1
            except Exception as e:
                logger.warning("evict failed for %s: %s", victim.id, e)
        return evicted

    @staticmethod
    def _serialize_entries(items: list[Any]) -> str:
        """把 L2 记忆条目序列化为紧凑文本（≤1.5KB），供提示词注入。"""
        if not items:
            return "暂无用户档案信息"
        parts = []
        for item in items:
            slot = _slot_value(item.slot)
            source = _source_value(item.source)
            tag = "✓" if source == "user_confirmed" else "◇"
            parts.append(
                f"- {tag} [{slot}] {item.item_key}: {item.item_value} "
                f"(置信度:{item.confidence}%)"
            )
        text = "\n".join(parts)
        if len(text.encode("utf-8")) > 1500:
            logger.warning("Profile block exceeds 1.5KB limit, truncating")
            text = text[:1400] + "\n... (已截断)"
        return text

    @staticmethod
    def _serialize_profile(items: list[ProfileItem]) -> str:
        """把个性化设置档案卡序列化为紧凑文本（保留旧调用方）。"""
        if not items:
            return "暂无用户档案信息"
        parts = []
        for item in items:
            source_tag = "✓" if item.source == SourceType.USER_CONFIRMED else "◇"
            confirmed_tag = " [已确认]" if item.confirmed_at else ""
            parts.append(
                f"- {source_tag} [{item.slot.value}] {item.item_key}: {item.item_value} "
                f"(置信度:{item.confidence}%){confirmed_tag}"
            )
        text = "\n".join(parts)
        if len(text.encode("utf-8")) > 1500:
            text = text[:1400] + "\n... (已截断)"
        return text

    # ── 异步队列集成 ──────────────────────────────────────────────────

    async def _enqueue_consolidate(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
        turn_count: int,
    ) -> None:
        """入队巩固任务（每回合完成后调用）。"""
        if not self._producer:
            return
        try:
            await self._producer.enqueue(
                task_type="memory_consolidate",
                tenant_id=tenant_id,
                payload={
                    "session_id": session_id,
                    "user_id": user_id,
                    "turn_count": turn_count,
                    "trigger": "turn_complete",
                },
                priority=0,
            )
        except Exception as e:
            logger.warning("Failed to enqueue consolidate task: %s", e)

    async def _enqueue_rollup(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> None:
        """入队 rollup 任务（会话结束时调用）。"""
        if not self._producer:
            return
        try:
            await self._producer.enqueue(
                task_type="memory_rollup",
                tenant_id=tenant_id,
                payload={
                    "session_id": session_id,
                    "user_id": user_id,
                    "trigger": "session_end",
                },
                priority=1,
            )
        except Exception as e:
            logger.warning("Failed to enqueue rollup task: %s", e)


def _new_memory_entry(
    tenant_id: str,
    user_id: str,
    slot: str,
    key: str,
    value: str,
    confidence: int,
    source: str,
    embedding: list[float] | None,
) -> Any:
    """构造 L2 条目（延迟导入，避免模块级循环依赖）。"""
    from app.memory.layers import MemoryEntry

    return MemoryEntry(
        id=new_entry_id(),
        tenant_id=tenant_id,
        user_id=user_id,
        slot=slot,
        item_key=key,
        item_value=value,
        confidence=confidence,
        source=source,
        embedding=embedding,
        access_count=0,
        last_accessed_at=None,
        status="active",
        created_at=None,
        updated_at=None,
    )


# ── 全局单例工厂 ──────────────────────────────────────────────────────────

_memory_service_instance: MemoryService | None = None


def set_memory_service(svc: MemoryService) -> None:
    """设置全局 MemoryService 实例（应用启动时调用）。"""
    global _memory_service_instance
    _memory_service_instance = svc
    logger.info("MemoryService instance set globally")


def get_memory_service() -> MemoryService | None:
    """获取全局 MemoryService 实例。"""
    return _memory_service_instance


def get_service() -> MemoryService | None:
    """获取全局 MemoryService 实例（API 层便捷别名）。"""
    return _memory_service_instance


def create_memory_service(redis: Any = None, pool: Any = None) -> MemoryService:
    """创建 MemoryService 实例（便捷工厂函数）。"""
    store = None
    profile_card = None
    if pool is not None:
        from app.memory.profile import ProfileStore

        store = ProfileStore(pool)
    if redis is not None:
        profile_card = ProfileCard(redis)
    return MemoryService(
        store=store,
        profile_card=profile_card,
        session_meta_store=SessionMetaStore(),
    )
