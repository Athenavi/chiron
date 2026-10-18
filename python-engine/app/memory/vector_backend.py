"""记忆层向量后端的选路与"后端不可达"的可见性。

两件事放在一起，是因为它们属于同一个缺陷的两半（`docs/core-mechanisms-paper.md` §5.1
「L3 摘要向量没有数据来源」）：过去这两个 store 都**硬编 Milvus**，而 Milvus 在 compose 里属
`selfhosted-s3` profile、默认不启动；连不上时只 `logger.warning` 一句然后返回空 ——
症状是"L3 语义召回恒空却标着 mode=semantic"，而不是"后端没起来"。

所以这里定两条规矩：

1. **后端由配置选**，未知值退回 PG（唯一恒在的那个），而不是退回那个要额外服务才可用的；
2. **不可达要说得出**：连续失败只在第一次记 `error`，之后降到 `debug`（每请求刷一条 warning
   会把日志冲走），成功一次即复位，使下一次故障重新大声。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PG = "pg"
MILVUS = "milvus"

_MILVUS = frozenset({"milvus"})
_PG = frozenset({"pg", "pgvector", "postgres", "postgresql"})

# 同一个非法值只提醒一次：resolve() 每次召回都会被调用，而设置值不会在运行中变，
# 重复告警只是噪声；但"配错了却没人看见"仍然是真问题。
_warned: set[str] = set()


def resolve(raw: str | None = None) -> str:
    """``MEMORY_VECTOR_BACKEND`` → :data:`PG` / :data:`MILVUS`。

    Args:
        raw: 显式给出的配置值；``None`` 表示读 settings（测试里换值用的参数）。
    """
    value = (raw if raw is not None else _settings_value() or "").strip().lower()
    if value in _MILVUS:
        return MILVUS
    if value in _PG or not value:
        return PG
    if value not in _warned:
        _warned.add(value)
        logger.warning(
            "未知记忆向量后端 %r（只支持 pg / milvus），按 pg 走 —— PG 是唯一恒在的存储，"
            "退回别的后端会把语义召回变成静默为空",
            value,
        )
    return PG


def _settings_value() -> str:
    from app.config import settings

    return settings.memory_vector_backend


def parse_vector(value: Any) -> list[float] | None:
    """``memory_summaries.embedding``（``float8[]``）→ ``list[float] | None``。

    asyncpg 原生把数组解成 ``list``（二进制协议，不必解析文本），所以这里只做口径归一：
    空数组与 NULL 都是"这条没有向量"（读侧据此退回时间排序），替身给 str / tuple 也吃得下。
    列类型选 ``float8[]`` 而不是 pgvector 的 ``vector(n)`` 的理由见迁移 0019 的头注释。
    """
    if value is None:
        return None
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        if value is None:
            # 文本 "null" 解出来是 None，和"这一列没有向量"同义。
            return None
    items = list(value)
    return items or None


class FailureLog:
    """某个后端「连续失败只大声一次」的日志门。"""

    __slots__ = ("_name", "_armed")

    def __init__(self, name: str) -> None:
        self._name = name
        self._armed = True

    def failure(self, reason: str) -> None:
        if self._armed:
            self._armed = False
            logger.error(
                "%s 不可用，本次请求将退回 PG 侧读取（同类故障之后降到 debug，"
                "直到它恢复一次为止）：%s",
                self._name,
                reason,
            )
        else:
            logger.debug("%s 仍不可用: %s", self._name, reason)

    def success(self) -> None:
        self._armed = True
