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

from app.config import settings
from app.memory.layers import CosineScorer, SummaryEntry
from app.memory.summariser import NothingWorthRememberingError
from app.memory.vector_backend import (
    active_embedding_model,
    report_space_mismatch,
    same_vector_space,
)

logger = logging.getLogger(__name__)

Embedder = Callable[[str], Awaitable[list[float] | None]]
#: 摘要正文的生成口。第二个参数是**这次巩固所属的租户**（可为空）：网关的预算预检与
#: 扣减只在 `tenant_id` 非空时发生，不传等于让后台记忆任务绕过租户记账。
Summariser = Callable[..., Awaitable[str]]


@dataclass
class ConsolidateResult:
    """一次巩固的产出。"""

    summary: SummaryEntry | None = None
    deduplicated: bool = False  # 精确 hash 命中既有条目
    near_duplicate_of: str | None = None  # 近重复既有 summary id
    skipped: bool = False  # 判定"这一窗没什么值得记"，未写入（不是失败）
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": asdict(self.summary) if self.summary else None,
            "deduplicated": self.deduplicated,
            "near_duplicate_of": self.near_duplicate_of,
            "skipped": self.skipped,
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
        tenant_id: int,
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
            content = (
                await self._summariser(messages, tenant_id)
                if self._summariser
                else _extract_summary(messages)
            )
        except NothingWorthRememberingError:
            # 模型的判定就是产出：这一窗没什么值得记。写入一段截断原文比不写更糟，
            # 因为 L3 会被后续每回合召回。这不是失败，所以不进 error（失败会重试）。
            result.skipped = True
            return result
        except Exception as e:
            logger.warning("Consolidator summarise failed: %s", e)
            content = _extract_summary(messages)

        # 空串 = 生成式这条路这次**没做成**（网关没绑、额度耗尽、模型空回）。退回抽取式
        # 而不是判失败：降级正文仍然可检索，而抛错会让整条记忆写入进重试直至 DLQ。
        content = (content or "").strip()
        if not content:
            content = _extract_summary(messages).strip()
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

        # ④ 条目先成形，再按**它自己的** `embed_text` 嵌一次。
        # 口径从此只有一处定义（`SummaryEntry.embed_text`：topics 置于正文之前）。过去这里
        # 写的是 `", ".join(topics) + ": " + content`，而那个属性是 `" ".join(topics) + "\n"
        # + content` —— 两处都自称"topics + 正文"，落库的却是两个互不可比的向量空间。
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
        embedding: list[float] | None = None
        if self._embedder:
            try:
                embedding = await self._embedder(entry.embed_text)
            except Exception as e:
                logger.warning("Consolidator embed failed: %s", e)
                embedding = None
        if embedding:
            model_tag = active_embedding_model()
            # 条目自己带上向量：写入侧返回的是重新查回来的行，但 `has_embedding`
            # 是给用户看的（记忆面板），不该取决于哪一条投影把行读回来。
            entry.embedding = embedding
            # 口径标签与向量同生同灭：没有向量就没有"属于哪个空间"可声称。
            entry.embedding_model = model_tag
            entry.embedding_dim = len(embedding)
        else:
            model_tag = ""

        # 近重复检测（cosine > `memory_dedup_threshold`）。这个配置此前无人引用，
        # 阈值硬编在代码里 —— 与 memory_consolidate_batch 同一类"写了但没接"的死配置。
        if embedding:
            dup_id = await self._find_near_duplicate(
                tenant_id, user_id, embedding, model=model_tag, dim=len(embedding)
            )
            if dup_id:
                result.near_duplicate_of = dup_id
                # 近重复仍写入（保留 lineage），但标记
                # 设计文档：合并保留新，旧的 archived

        # ⑤ 写 L3
        try:
            created = await self._store.insert(entry, embedding)
            result.summary = created
        except Exception as e:
            logger.error("Consolidator write failed: %s", e)
            result.error = str(e)

        return result

    async def _find_near_duplicate(
        self,
        tenant_id: int,
        user_id: str,
        embedding: list[float],
        *,
        model: str,
        dim: int,
    ) -> str | None:
        """在既有摘要中查找近重复（cosine > `memory_dedup_threshold`）。"""
        try:
            existing = await self._store.list_active(
                tenant_id, user_id, limit=50, with_embedding=True
            )
        except Exception:
            return None
        scorer = CosineScorer(embedding)
        threshold = float(settings.memory_dedup_threshold)
        skipped: list[Any] = []
        dup_id: str | None = None
        for e in existing:
            if not e.embedding:
                continue
            recorded = (getattr(e, "embedding_model", None), getattr(e, "embedding_dim", None))
            if not same_vector_space(*recorded, model=model, dim=dim):
                skipped.append(recorded)
                continue
            if scorer.score(e.embedding) > threshold:
                dup_id = str(e.id)
                break
        # 跳出循环也要报：口径过滤掉了几条历史行，是"判重为什么没命中"的唯一线索。
        report_space_mismatch(skipped, model=model, dim=dim)
        return dup_id


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


def compute_hash(tenant_id: int, user_id: str, content: str) -> str:
    """计算内容哈希。"""
    raw = f"{tenant_id}:{user_id}:{content}"
    return f"sha256:{hashlib.sha256(raw.encode()).hexdigest()[:40]}"
