"""memory_summaries 增向量口径标签列 embedding_model / embedding_dim

Revision ID: 0020_memory_summary_embedding_space
Revises: 0019_memory_summary_embedding
Create Date: 2026-10-18

背景（缺陷「L3 摘要的向量口径不可判定」；那份核心机制剖析文档**不在仓库里**，结论全部写在下面）：
0019 把向量落进了
PG，但**一个数是多少维、由哪个模型算出来的，行里没有任何记录**。列型是无长度约束的
``double precision[]``，而 `settings.embedding_model` / `embedding_dim` 只管 L2 与 RAG，
不作用于 L3 —— 于是换一次嵌入模型（或本地/远端后端切来切去）之后：

* **维度不同**：`dot_product` 对长度不一致返回 0.0（layers.py 的显式口径），不会算错，但
  新旧行**永不相似** —— 近重复判定与语义召回静默退化成空，日志里一个字都没有；
* **维度相同、模型不同**：这才是真危险。余弦照样落在 0~1，而且跨模型的向量空间并不对应
  同一语义，一个 >0.95 的分数会让两条其实无关的摘要被当成近重复标记在一起
  （`Consolidator` 的判重结论据此影响整理）。

所以本轮（M14 / 方案 C3）写侧在落向量的**同时**落"这个向量属于哪个模型、多少维"，读侧只在
同口径之间比相似度。这与业界的嵌入迁移实践同源：并排记录 model/dimension 标签，切换时按标签
隔离新旧向量，而不是假装一个数组列里的数彼此可比。

列约定：

* 两列都**可空**。NULL 的语义是"这一行的口径未知"（0019 之前写的、或由未带标签的入口写的行），
  读侧对未知行**按现状继续参与比较** —— 严格排除会让历史行为突然失忆，而未知行本来就是
  本改造之前的形状。从本轮起写入必带标签，所以"同维不同模型"这个危险面从此有界。
* **不加索引**：与 0019 同一条理由，这两列只用于"扫回来的候选先过滤再比"，不参与检索谓词。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0020_memory_summary_embedding_space"
down_revision: Union[str, Sequence[str], None] = "0019_memory_summary_embedding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory_summaries
            ADD COLUMN IF NOT EXISTS embedding_model text,
            ADD COLUMN IF NOT EXISTS embedding_dim integer
        """
    )


def downgrade() -> None:
    # 退回 0019 的形状：向量还在，只是不再可判定属于哪个口径。
    op.execute(
        """
        ALTER TABLE memory_summaries
            DROP COLUMN IF EXISTS embedding_model,
            DROP COLUMN IF EXISTS embedding_dim
        """
    )
