"""memory_summaries 增嵌入向量列 embedding

Revision ID: 0019_memory_summary_embedding
Revises: 0018_pending_interactions
Create Date: 2026-10-18

背景（缺陷「L3 摘要向量没有数据来源」；那份核心机制剖析文档**不在仓库里**，结论全部写在下面）：
L3 的三项语义能力 —— `Consolidator._find_near_duplicate` 的近重复判定、`SummaryStore.recall` 的语义召回、
配置项 `memory_summary_min_cosine` —— **过去全部悬空**。向量只往 Milvus 写，而 Milvus 在
compose 里属于 `selfhosted-s3` profile、注释自陈"默认不启动"；`memory_summaries` 表里连一列
向量都没有，于是读侧 `list_active` 选不出 embedding，判重循环每条都走
`if not e.embedding: continue` ⇒ **恒不触发**，召回退化分支恒 0 命中却仍标 `mode="semantic"`。
这不是"索引不够快"，是**数据不存在**。

本列把摘要向量变成 PG（本项目唯一恒在的持久存储）里的一等数据：写入侧存进来、读侧选得回去，
语义判重与语义召回才有真实输入。

列约定：

* 类型选 ``double precision[]`` 而**不是** pgvector 的 ``vector(1536)``，三条实测理由：
  ① ``vector`` 列要求目标库装有该扩展，而 ``CREATE EXTENSION`` 需要超级用户 —— 一个纯加列
  改造会因此变成部署前置条件；本仓库本地库是 ``postgres:16-alpine``（不含扩展），
  只有 CI 用 ``pgvector/pgvector:pg16``；
  ② asyncpg 没有 vector codec，读回来是文本 ``[1,2,3]`` 得自己解析（或新增 `pgvector` 依赖），
  而 ``float8[]`` 由 asyncpg 原生解码为 ``list[float]`` 并走二进制协议；
  ③ 本列**不参与 ANN 检索**：单租户/单用户的候选集被 limit 限死（判重扫 50 条、召回扫 200 条），
  M12 已把这条路径换成归一化点积 + 把 query 模长提到循环外，N≤200 的 1-vs-N 精确扫描实测约
  16ms。pgvector 能给的只有 ``<=>`` 与 ANN 索引，这里两者都不需要 —— 与 M12 判定"给 L2 加
  HNSW 属过度设计"是同一条结论。（真要上 ANN 时，``ALTER TABLE … TYPE vector(n) USING …``
  是一条可写的后续迁移，届时再付扩展这个前置条件。）
* **可空**：历史行没有向量，本轮不做批量回填（回填要给全表逐条调嵌入 API，是成本与幂等的
  产品决策，不是加列能顺手解决的）。读侧把 NULL 当作"这条只能按时间参与召回"。
* **不加索引**：见 ③；btree/GIN 对数组在这个查询形态下没有检索意义，ANN 又需要 vector 类型。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0019_memory_summary_embedding"
down_revision: Union[str, Sequence[str], None] = "0018_pending_interactions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory_summaries
            ADD COLUMN IF NOT EXISTS embedding double precision[]
        """
    )


def downgrade() -> None:
    # 向量只被语义路径消费，删列不丢正文：语义召回退回"取最近"，判重退回不触发。
    op.execute("ALTER TABLE memory_summaries DROP COLUMN IF EXISTS embedding")
