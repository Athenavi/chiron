"""L3 摘要巩固 pipeline — 把对话消息序列压缩为"主题 + 实体 + 摘要"写入 L3。

复用现有组件：
- ContextManager._summarise（LLM 摘要，gateway 调用）
- TaskRouter._extract_entities（规则式 NER，零依赖）
- llm_client.embed（嵌入网关）

pipeline 步骤：
1. LLM 摘要（或降级提取式摘要）
2. NER 实体抽取 + 主题解析
3. 去重检查（content_hash 精确 + 近重复 cosine>0.95）
4. 写 L3（PG + Milvus 双写）
5. 稳定事实探测（可选，PR-4 冲突流用）
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from app.memory.layers import SummaryEntry, cosine_similarity

logger = logging.getLogger(__name__)

Embedder = Callable[[str], Awaitable[list[float] | None]]
Summariser = Callable[[list[dict[str, Any]]], Awaitable[str]]


@dataclass
class ConsolidateResult:
    """一次巩固的产出。"""

    summary: SummaryEntry | None = None
    deduplicated: bool = False  # 精确 hash 命中既有条目
    near_duplicate_of: str | None = None  # 近重复既有 summary id
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": asdict(self.summary) if self.summary else None,
            "deduplicated": self.deduplicated,
            "near_duplicate_of": self.near_duplicate_of,
            "error": self.error or None,
        }


class Consolidator:
    """L4 滑出消息 → L3 摘要巩固 pipeline。"""

    def __init__(
        self,
        # 只需 get_by_hash / insert / list_active 三个方法，而**两个** SummaryStore
        # （app.memory.summaries 的写入侧与 app.memory.summary_store 的读取侧）都提供它们
        # —— 调用方（app/main.py）按职责分别传其一，故这里不收窄到具体类。
        store: Any,
        embedder: Embedder | None = None,
        summariser: Summariser | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._summariser = summariser

    async def consolidate(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
        turn_start: int = 0,
        turn_end: int = 0,
    ) -> ConsolidateResult:
        """把一批消息巩固为一条 L3 摘要。"""
        if not messages:
            return ConsolidateResult(error="no messages to consolidate")

        result = ConsolidateResult()

        # ① 摘要
        try:
            if self._summariser:
                content = await self._summariser(messages)
            else:
                content = _extract_summary(messages)
        except Exception as e:
            logger.warning("Consolidator summarise failed: %s", e)
            content = _extract_summary(messages)

        content = content.strip()
        if not content:
            return ConsolidateResult(error="summary is empty")

        # ② 实体 + 主题
        entities = _extract_entities(content)
        topics = _extract_topics(content)

        # ③ 去重：精确 hash
        ch = compute_hash(tenant_id, user_id, content)
        try:
            existing = await self._store.get_by_hash(tenant_id, user_id, ch)
        except Exception:
            existing = None
        if existing is not None:
            result.summary = existing
            result.deduplicated = True
            return result

        # ④ 嵌入
        embed_text = ", ".join(topics) + ": " + content if topics else content
        embedding = None
        if self._embedder:
            try:
                embedding = await self._embedder(embed_text)
            except Exception as e:
                logger.warning("Consolidator embed failed: %s", e)
                embedding = None

        # 近重复检测（cosine > 0.95）
        if embedding:
            dup_id = await self._find_near_duplicate(tenant_id, user_id, embedding)
            if dup_id:
                result.near_duplicate_of = dup_id
                # 近重复仍写入（保留 lineage），但标记
                # 设计文档：合并保留新，旧的 archived

        # ⑤ 写 L3
        entry = SummaryEntry(
            id=new_summary_id(),
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            content=content,
            topics=topics,
            entities=entities,
            turn_start=turn_start,
            turn_end=turn_end,
            content_hash=ch,
            access_count=0,
            last_accessed_at=None,
        )
        try:
            created = await self._store.insert(entry, embedding)
            result.summary = created
        except Exception as e:
            logger.error("Consolidator write failed: %s", e)
            result.error = str(e)

        return result

    async def _find_near_duplicate(
        self, tenant_id: str, user_id: str, embedding: list[float]
    ) -> str | None:
        """在既有摘要中查找近重复（cosine > 0.95）。"""
        try:
            existing = await self._store.list_active(tenant_id, user_id, limit=50)
        except Exception:
            return None
        for e in existing:
            if not e.embedding:
                continue
            if cosine_similarity(embedding, e.embedding) > 0.95:
                return str(e.id)
        return None


# ── 纯函数（无外部依赖）──


def _extract_summary(messages: list[dict[str, Any]]) -> str:
    """降级摘要：每条消息取前 200 字符拼接。"""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            snippet = content[:200].replace("\n", " ")
            parts.append(f"[{role}]: {snippet}")
    return "\n".join(parts) if parts else "(no content)"


def _extract_entities(text: str) -> dict[str, list[str]]:
    """规则式 NER（与 TaskRouter._extract_entities 同实现，零依赖）。"""
    entities: dict[str, list[str]] = {}

    urls = re.findall(r'https?://[^\s<>"\')\]]+', text)
    if urls:
        entities["urls"] = urls

    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if emails:
        entities["emails"] = emails

    phones = re.findall(r"(?<!\d)1[3-9]\d{9}(?!\d)", text)
    if phones:
        entities["phones"] = phones

    paths = re.findall(
        r"(?:[/\\][\w./\\-]+\.\w+|[\w./\\-]+\.(?:py|js|ts|go|rs|java|cpp|c|md|yaml|yml|json|toml))",
        text,
    )
    if paths:
        entities["file_paths"] = paths

    dates = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text)
    if dates:
        entities["dates"] = dates

    amounts = re.findall(r"[¥$￥]\s*[\d,]+(?:\.\d+)?(?:万|亿)?", text)
    if amounts:
        entities["amounts"] = amounts

    ips = re.findall(
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
        text,
    )
    if ips:
        entities["ip_addresses"] = ips

    code_refs = re.findall(r"\b([A-Z][a-zA-Z0-9_]*)\.([a-z][a-zA-Z0-9_]*)\(", text)
    if code_refs:
        entities["code_refs"] = [f"{c[0]}.{c[1]}()" for c in code_refs]

    return entities


def _extract_topics(text: str) -> list[str]:
    """从摘要文本中提取主题。

    策略：
    1. 如果摘要包含 "topics:" 行，解析
    2. 否则取高频名词短语（简单分词）
    """
    # 策略 1：显式 topics 行
    m = re.search(r"topics?\s*[:：]\s*(.+)", text, re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
        parts = [
            p.strip().strip("\"'[]") for p in re.split(r"[,，;；]", raw) if p.strip()
        ]
        if parts:
            return parts[:5]

    # 策略 2：简单关键词提取
    words = re.findall(r"[\u4e00-\u9fff]{2,4}|[a-zA-Z]{3,}", text)
    freq = Counter(words)
    return [word for word, count in freq.most_common() if count >= 2][:5]


def new_summary_id() -> str:
    """生成新的摘要 ID。"""
    return f"sms_{uuid.uuid4().hex[:20]}"


def compute_hash(tenant_id: str, user_id: str, content: str) -> str:
    """计算内容哈希。"""
    raw = f"{tenant_id}:{user_id}:{content}"
    return f"sha256:{hashlib.sha256(raw.encode()).hexdigest()[:40]}"
