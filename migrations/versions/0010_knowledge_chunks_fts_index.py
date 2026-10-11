"""knowledge_chunks 的全文检索 GIN 索引

Revision ID: 0010_knowledge_chunks_fts_index
Revises: 0009_turns_created_at_index
Create Date: 2026-10-10

`python-engine/app/rag/hybrid_search.py` 的 PG 全文检索分支每次查询都做：

    WHERE … AND to_tsvector('simple', kc.content) @@ plainto_tsquery('simple', $1)

`knowledge_chunks` 上此前**没有任何可用于该谓词的索引**（baseline 只给了主键与两条
外键），于是每次检索都要全表扫 knowledge_chunks 并**逐行**做 to_tsvector ——
知识库一变大，混合检索的 FTS 一路就退化成顺序扫描。

这里补一条 GIN 索引。表达式用**显式 regconfig**（`to_tsvector('simple', content)`），
该重载是 IMMUTABLE，可以直接建索引（不指定 regconfig 的重载依赖
`default_text_search_config`，是 STABLE，无法建索引）。

> 注：向量侧的索引不需要在这里补 —— `app/rag/stores/pgvector_store.py` 的
> `_ensure_table()` 已在运行时 `CREATE INDEX IF NOT EXISTS … USING ivfflat
> (embedding vector_cosine_ops) WITH (lists = 100)`。

``IF NOT EXISTS``：与既有迁移风格一致。新增迁移后请复核 README 的「数据库迁移」段
（启动时 `CheckSchemaVersion` 会比对 head）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0010_knowledge_chunks_fts_index"
down_revision: Union[str, Sequence[str], None] = "0009_turns_created_at_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_content_fts
            ON public.knowledge_chunks
            USING gin (to_tsvector('simple', content))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_knowledge_chunks_content_fts")
