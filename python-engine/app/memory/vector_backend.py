"""记忆层向量后端的选路与"后端不可达"的可见性。

两件事放在一起，是因为它们属于同一个缺陷的两半（「L3 摘要向量没有数据来源」；那份核心机制
剖析文档**不在仓库里**，结论全部写在下面）：过去这两个 store 都**硬编 Milvus**，而 Milvus 在 compose 里属
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


# ── 向量口径（embedding space）───────────────────────────────────────────

#: 已经警告过的"与当前口径不符的历史行"，按 `model/dim` 去重。
_space_warned: set[str] = set()


def active_embedding_model() -> str:
    """当前 L3 写入应该打的模型标签。

    与 `app.llm.client.LLMClient.embed` 的选择顺序一致：**本地模型优先**，否则用
    `embedding_model` 走网关。已知边界：本地模型已配置但那次调用**失败回退到网关**时，
    这里给出的标签会是本地的名字 —— 两种模型维度通常不同，`embedding_dim` 记的是向量的
    真实长度，所以这种错标会被维度守卫接住；同维互换（例如 1536 维换 1536 维）才会漏。
    """
    from app.config import settings

    local = (settings.local_embedding_model or "").strip()
    return local or (settings.embedding_model or "").strip()


def same_vector_space(
    recorded_model: Any,
    recorded_dim: Any,
    *,
    model: str,
    dim: int,
) -> bool:
    """这一行的向量能不能和当前口径的向量放在同一个空间里比。

    * 维度对不上：``dot_product`` 对长度不一致返回 0.0（layers.py 的显式口径），所以它
      不会算错，而是**永不相似** —— 近重复与语义召回静默变空。既然结论已经注定，就把它
      显式过滤掉并说一声，而不是让调用方拿着一个 0.0 去猜为什么。
    * 模型对不上（维数却相同）：余弦照样落在 0~1，跨模型空间里这个数没有意义，一个
      >0.95 会把两条无关摘要标成近重复。这是必须排除的那一类。
    * 标签为 NULL：口径**未知**，按本改造之前的行为继续参与。严格排除会让历史行突然
      失忆，而未标行本来就是这个形状。
    """
    if recorded_model is not None and str(recorded_model) != model:
        return False
    if recorded_dim is not None and int(recorded_dim) != dim:
        return False
    return True


def _space_key(recorded: tuple[Any, Any]) -> str:
    """把一行的口径写成 `model/dim`。

    未打标签（迁移 0020 之前的行）要有能读的名字 —— 日志里出现 `None/4` 会让人以为
    是代码坏了，而不是"这一行没记口径"。
    """
    model, dim = recorded
    return f"{model or '口径未知'}/{dim if dim is not None else '?'}"


def report_space_mismatch(skipped: list[Any], *, model: str, dim: int) -> int:
    """把"因口径不符被跳过的行"说一次，返回跳过条数。

    `skipped` 收的是 `same_vector_space` 的那两个入参原值（模型标签、维度），格式化留
    在这里做，三处调用点就不必各自想一遍 None 该怎么写。

    每种口径只警告一次（同一种不符会在每次巩固/召回时重复出现，刷日志没有信息量），
    但第一次必须说 —— 否则"语义层怎么空了"又变成一个查不出来的问题。
    """
    if not skipped:
        return 0
    for key in sorted({_space_key(s) for s in skipped}):
        if key not in _space_warned:
            _space_warned.add(key)
            logger.warning(
                "L3 摘要向量的口径与当前 %s/%d 不符，这些历史行不参与语义判定（同类只警告一次）：%s",
                model,
                dim,
                key,
            )
    return len(skipped)
