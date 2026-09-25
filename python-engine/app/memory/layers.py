"""四层记忆架构的核心数据模型定义。

本模块定义了记忆系统四层架构的核心数据结构和类型：
- L1 SessionMeta: 会话元数据（进程内存）
- L2 ProfileCard 相关类型: 用户档案卡（PostgreSQL）
- L3 SummaryStore 相关类型: 对话摘要（Milvus + PG）
- Scope: 记忆查询范围
- 事件类型: MemoryConflict 等
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any, Literal

# ── L1: 会话元数据 ─────────────────────────────────────────────────────

#: 会话入口渠道。L1 ``SessionMeta`` 与 L2 记忆写入共用同一取值集合。
EntryChannel = Literal["web", "api", "quick_execute", "workflow"]


@dataclass
class SessionMeta:
    """L1 会话元数据（进程内存，用完即丢）。

    存储会话级别的簿记数据：session_id、入口渠道、模式、回合数、token 用量等。
    与对话内容无关，纯粹为路由、计费、审计、降级标记服务。
    """

    session_id: str
    tenant_id: str
    user_id: str
    entry_channel: EntryChannel
    mode: str  # agent 模式
    started_at: float
    last_active_at: float
    turn_count: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    degraded: bool = False  # LLM 摘要不可用等降级标记
    flags: dict[str, Any] = field(default_factory=dict)

    def mark_turn_complete(self, tokens_in: int, tokens_out: int) -> None:
        """标记回合完成，更新计数器和时间戳。"""
        self.turn_count += 1
        self.total_tokens_in += tokens_in
        self.total_tokens_out += tokens_out
        self.last_active_at = time.time()

    def mark_degraded(self, reason: str = "") -> None:
        """标记会话降级。"""
        self.degraded = True
        if reason:
            self.flags["degraded_reason"] = reason


@dataclass
class SessionContext:
    """会话上下文（on_session_start 返回）。"""

    meta: SessionMeta | None
    profile_cached: bool  # L2 档案卡是否已缓存
    summaries_prefetched: int  # 预取的 L3 摘要数量


# ── 通用类型 ─────────────────────────────────────────────────────────────


@dataclass
class Scope:
    """记忆查询范围（tenant + user + session）。"""

    tenant_id: str
    user_id: str
    session_id: str


class MemoryType(StrEnum):
    """记忆类型引用（Milvus memory_type 取值）。"""

    PROFILE = "profile"
    SUMMARY = "summary"
    TOPIC = "topic"
    LONG_TERM = "long_term"
    SHORT_TERM = "short_term"


# ── L2: 用户档案卡相关类型 ──────────────────────────────────────────────


class SlotType(StrEnum):
    """L2 档案卡槽位类型。"""

    IDENTITY = "identity"  # 身份属性
    PREFERENCE = "preference"  # 偏好
    DECISION = "decision"  # 关键决策
    FACT = "fact"  # 长期事实


class SourceType(StrEnum):
    """记忆来源类型。"""

    USER_CONFIRMED = "user_confirmed"  # 用户显式确认
    DERIVED = "derived"  # Agent 提炼
    TOOL_WRITTEN = "tool_written"  # 工具写入


SLOTS: list[str] = [s.value for s in SlotType]

# 展示名必须与前端 frontend-vue/src/api/memory.ts 的 MEMORY_SLOTS 一致：
# 两处若分叉，同一个 slot 会在「记忆页 Tab」与「条目标签」上显示成两个名字。
SLOT_LABELS: dict[str, str] = {
    "identity": "身份",
    "preference": "偏好",
    "decision": "关键决策",
    "fact": "长期事实",
}


@dataclass
class ProfileItem:
    """L2 档案卡单个条目。"""

    slot: SlotType
    item_key: str
    item_value: Any
    confidence: int  # 0-100
    source: SourceType
    version: int
    confirmed_at: float | None  # 用户最后确认时间（NULL=未确认）
    last_referenced_at: float | None  # 最近被召回引用时间
    created_at: float
    updated_at: float
    id: str | None = None  # 唯一标识符（用于 API 路由）

    def to_dict(self) -> dict[str, Any]:
        slot_val = self.slot.value if isinstance(self.slot, SlotType) else self.slot
        source_val = (
            self.source.value if isinstance(self.source, SourceType) else self.source
        )
        return {
            "id": self.id or f"{slot_val}:{self.item_key}",
            "slot": slot_val,
            "item_key": self.item_key,
            "item_value": self.item_value,
            "confidence": self.confidence,
            "source": source_val,
            "version": self.version,
            "confirmed_at": self.confirmed_at,
            "last_referenced_at": self.last_referenced_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class MemoryEntry:
    """L2 用户记忆条目（= user_memory_entries 行模型，profile.py 持久层使用）。"""

    id: str
    tenant_id: str
    user_id: str
    slot: Any
    item_key: str
    item_value: Any
    confidence: int  # 0-100
    source: Any
    embedding: list[float] | None = None
    access_count: int = 0
    last_accessed_at: Any = None
    status: str = "active"
    created_at: Any = None
    updated_at: Any = None

    def to_dict(self) -> dict[str, Any]:
        """基础字段的字典形态（存储行模型的自述）。

        展示名（``slot_label`` / ``source_label``）不在这里 —— 那是服务层的展示口径，
        不该固化进存储行模型；由 ``MemoryService._entry_dict`` 叠加。
        """
        return {
            "id": self.id,
            "slot": enum_value(self.slot),
            "key": self.item_key,
            "value": self.item_value,
            "confidence": self.confidence,
            "source": enum_value(self.source),
            "has_embedding": self.embedding is not None,
            "access_count": self.access_count,
            "last_accessed_at": to_iso(self.last_accessed_at),
            "status": self.status,
            "created_at": to_iso(self.created_at),
            "updated_at": to_iso(self.updated_at),
        }


def enum_value(value: Any) -> str:
    """把枚举成员或裸字符串统一成字符串值。

    ``slot`` / ``source`` 在存储层是字符串，在领域层是枚举 —— 出口统一走这里，
    免得每个调用点各写一遍 ``x.value if isinstance(x, X) else x``。
    """
    return value.value if isinstance(value, Enum) else str(value)


def to_iso(value: Any) -> str | None:
    """把 datetime / 时间戳统一成 ISO 字符串。

    前端对时间字段直接做 ``.slice(0, 10)`` 取日期，传 float 时间戳会在浏览器里抛
    TypeError —— 出口统一走这里，避免"接口能返回、页面渲染即崩"。
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


@dataclass
class ProfileUpdateResult:
    """L2 档案卡更新结果。"""

    success: bool
    item: ProfileItem | None
    conflict: ConflictRef | None = None


@dataclass
class ConflictRef:
    """冲突引用（当 L2 更新时与 user_confirmed 冲突）。"""

    conflict_id: str
    slot: SlotType
    item_key: str
    old_value: Any
    new_value: Any
    old_source: SourceType
    old_confirmed_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "slot": self.slot.value,
            "item_key": self.item_key,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "old_source": self.old_source.value,
            "old_confirmed_at": self.old_confirmed_at,
            "status": "pending",
        }


# ── L3: 对话摘要相关类型 ────────────────────────────────────────────────


@dataclass
class SummaryEntry:
    """L3 对话摘要条目（存储用）。

    除前 5 个字段外全部有默认值：摘要由引擎在回合结束时自动生成，
    调用方（Consolidator / 测试）不该被迫补齐 turn 区间、hash、向量等派生字段。
    """

    id: str
    tenant_id: str
    user_id: str
    session_id: str
    content: str
    topics: list[str] = field(default_factory=list)
    entities: dict[str, list[str]] = field(default_factory=dict)
    turn_start: int = 0
    turn_end: int = 0
    content_hash: str = ""
    access_count: int = 0
    last_accessed_at: float | None = None
    status: str = "active"
    created_at: float = 0.0
    embedding: list[float] | None = None

    @property
    def embed_text(self) -> str:
        """生成 embedding 用的文本。

        topics 置于正文之前：主题词是用户检索时最常命中的字面，若只嵌正文，
        「asyncio」这类主题词会检索不到。
        """
        if not self.topics:
            return self.content
        return f"{' '.join(self.topics)}\n{self.content}"

    def to_dict(self) -> dict[str, Any]:
        """API / 缓存友好的字典形态（``has_embedding`` 供前端标注向量状态）。"""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "content": self.content,
            "topics": list(self.topics),
            "entities": dict(self.entities),
            "turn_range": [self.turn_start, self.turn_end],
            "access_count": self.access_count,
            "last_accessed_at": self.last_accessed_at,
            "status": self.status,
            "created_at": self.created_at,
            "has_embedding": self.embedding is not None,
        }


@dataclass
class RecalledItem:
    """L3 召回的单个摘要项。"""

    id: str
    content: str
    topics: list[str]
    entities: dict[str, list[str]]  # {"person": [], "tech": [], "url": []}
    turn_range: tuple[int, int]
    session_id: str
    access_count: int
    last_accessed_at: float
    created_at: float
    score: float  # final_score


@dataclass
class RecallResult:
    """recall 方法返回结果。"""

    profile_block: str = ""  # L2 档案卡紧凑序列化（≤1.5KB）
    summary_items: list[RecalledItem] = field(default_factory=list)  # L3 召回摘要（≤6KB）

    @property
    def has_content(self) -> bool:
        """是否有有效内容（L2 档案卡或 L3 摘要）。"""
        # 注意：profile_block 中的 "暂无用户档案信息" 是无档案时的占位文本，
        # 不应视为有效内容。
        has_profile = (
            bool(self.profile_block.strip())
            and "暂无用户档案信息" not in self.profile_block
        )
        return has_profile or len(self.summary_items) > 0


# ── 事件类型 ─────────────────────────────────────────────────────────────


@dataclass
class MemoryConflict:
    """记忆冲突事件（SSE 推送）。"""

    conflict_id: str
    slot: SlotType
    item_key: str
    old_value: Any
    new_value: Any
    source: SourceType
    tenant_id: str
    user_id: str
    created_at: float


# ── 其他类型 ─────────────────────────────────────────────────────────────


@dataclass
class MemoryRef:
    """记忆引用（用于 forget 操作）。"""

    memory_type: MemoryType
    # 如果 memory_type == "profile"
    slot: SlotType | None = None
    item_key: str | None = None
    # 如果 memory_type == "summary"
    memory_id: str | None = None


@dataclass
class TokenUsage:
    """Token 使用统计。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @property
    def tokens_in(self) -> int:
        return self.prompt_tokens

    @property
    def tokens_out(self) -> int:
        return self.completion_tokens


# ── 工具函数 ─────────────────────────────────────────────────────────────


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度。"""
    import math

    if len(a) != len(b):
        return 0.0

    dot_product = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot_product / (norm_a * norm_b)


def recency_decay(when: datetime | None, half_life_days: float = 60.0) -> float:
    """新鲜度衰减因子：越久未被引用越接近 0。

    用 ``exp(-age_days / half_life_days)``（e 折损时间，不是严格的半衰期）：
    刚引用过的条目为 1.0，``half_life_days`` 天前约为 0.368。

    ``when is None`` 时返回 0.5 —— 从未被引用的条目既不该拿满分（否则会压过
    真正被高频使用的旧条目），也不该按最陈旧一档处理（否则新建条目在检索里立刻沉底）。
    """
    if when is None:
        return 0.5
    if half_life_days <= 0:
        return 0.0
    now = datetime.now(UTC)
    if when.tzinfo is None:  # asyncpg 的 timestamptz 在某些 codec 下回传 naive
        when = when.replace(tzinfo=UTC)
    age_days = max(0.0, (now - when).total_seconds() / 86400.0)
    return math.exp(-age_days / half_life_days)


def rerank_score(
    similarity: float,
    confidence: int,
    last_accessed_at: datetime | None,
    archived: bool = False,
) -> float:
    """把相似度 / 置信度 / 新鲜度合成最终排序分。

    三项加权（相似度为主、置信度次之、新鲜度微调），与 L3 的
    ``SummaryStore._compute_final_scores`` 保持同一思路 —— 两层记忆若各用一套
    排序口径，同一条内容在「检索结果」与「提示词注入」里的顺序会不一致。

    ``archived`` 条目整体降权而非直接排除：用户打开归档视图时仍要能看到它们。
    """
    base = (
        0.7 * max(0.0, min(1.0, similarity))
        + 0.2 * (max(0, min(100, int(confidence))) / 100.0)
        + 0.1 * recency_decay(last_accessed_at)
    )
    return base * 0.5 if archived else base
