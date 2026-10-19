"""知识库改用 PageIndex：重建 knowledge_* 表 + 用户级 provider 设置

Revision ID: 0023_knowledge_pageindex
Revises: 0022_run_lineage_ledger
Create Date: 2026-10-19

为什么是**重建**而不是改列：旧栈的三张表承载的是"向量 + 切块 + 全文检索"那套语义 ——
``knowledge_chunks`` 只因向量检索而存在，``knowledge_documents.content`` 是 BYTEA 原文，
``knowledge_bases.type`` 区分 wiki/rag 两种检索形态。改用 PageIndex 后这三样**一个都不剩**：
没有切块、没有向量、没有 FTS，检索形态由 provider（自托管 / Cloud）决定。留着旧列只会让
"哪一列还有用"永远说不清，所以这里直接换掉（迁移时线上只有测试数据，见变更单）。

新结构的三处取舍：

* **原文不进 PG**（``content`` → ``storage_key``）。对象存储才是原文的正确位置：大文件放
  PG 会放大 WAL、备份与迁移成本，且无法独立扩容。``storage_key`` 是对象存储里的 key，
  **不含** bucket —— bucket 属于部署配置，不进数据。
* **``type`` → ``provider``**，并且**只在这里留一个字符串**：provider 的取值集合会随接入
  的检索引擎增长（当前 ``selfhosted`` / ``cloud``），写成 PG ENUM 会让"新增一种 provider"
  变成一次 ``ALTER TYPE``。取值校验留在应用层（``app.knowledge.providers``）。
* **``status`` 承载"未启用/异常"**：``selfhosted`` 未配置或探测失败时，服务照常启动，库与
  用户设置各自落到 ``disabled`` / ``error`` —— 这正是"SaaS 平台不能因为一个可选依赖缺失
  就起不来"的落点。

用户级 Cloud key 的密文列（``user_knowledge_settings.cloud_api_key_enc``）与 Go 侧
``internal/settings`` 的 AEAD **同构**（见 ``app/knowledge/crypto.py`` 的算法说明），
所以网关侧也能解同一份密文。这里**不复用** ``system_settings``：那张表没有 ``user_id``
列（用户维度塞在 ``updated_by`` 里），也没有任何唯一约束（见 ``internal/settings`` 的
SaveConfig 注释），并发写入会各自成功而留下多份互相覆盖的配置 —— 用户级密钥是每个
请求都要读的东西，不能建在这种形状上。

``IF NOT EXISTS`` / ``IF EXISTS``：与既有迁移风格一致；本迁移依赖 0001 的基线表。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0023_knowledge_pageindex"
down_revision: Union[str, Sequence[str], None] = "0022_run_lineage_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── 新结构 ──

_CREATE_KNOWLEDGE_BASES = """
CREATE TABLE IF NOT EXISTS public.knowledge_bases (
    id                character varying(36)  NOT NULL,
    tenant_id         character varying(36)  NOT NULL,
    user_id           character varying(36)  NOT NULL,
    name              character varying(255) NOT NULL,
    description       text,
    visibility        character varying(16)  NOT NULL DEFAULT 'private',
    provider          character varying(16)  NOT NULL,
    status            character varying(16)  NOT NULL DEFAULT 'disabled',
    index_ref         character varying(255),
    config            jsonb                  NOT NULL DEFAULT '{}'::jsonb,
    document_count    integer                NOT NULL DEFAULT 0,
    total_size_bytes  bigint                 NOT NULL DEFAULT 0,
    credits_consumed  integer                NOT NULL DEFAULT 0,
    error_message     text,
    created_at        timestamp without time zone NOT NULL DEFAULT now(),
    updated_at        timestamp without time zone NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_bases_pkey PRIMARY KEY (id),
    CONSTRAINT knowledge_bases_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id),
    CONSTRAINT knowledge_bases_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
)
"""

_CREATE_KNOWLEDGE_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS public.knowledge_documents (
    id                 character varying(36)  NOT NULL,
    knowledge_base_id  character varying(36)  NOT NULL,
    tenant_id          character varying(36)  NOT NULL,
    user_id            character varying(36)  NOT NULL,
    name               character varying(255) NOT NULL,
    file_type          character varying(32),
    file_size_bytes    bigint                 NOT NULL DEFAULT 0,
    storage_key        character varying(512),
    status             character varying(16)  NOT NULL DEFAULT 'pending',
    pageindex_doc_id   character varying(64),
    page_count         integer                NOT NULL DEFAULT 0,
    error_message      text,
    metadata           jsonb                  NOT NULL DEFAULT '{}'::jsonb,
    created_at         timestamp without time zone NOT NULL DEFAULT now(),
    updated_at         timestamp without time zone NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_documents_pkey PRIMARY KEY (id),
    CONSTRAINT knowledge_documents_knowledge_base_id_fkey
        FOREIGN KEY (knowledge_base_id) REFERENCES public.knowledge_bases(id) ON DELETE CASCADE,
    CONSTRAINT knowledge_documents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id),
    CONSTRAINT knowledge_documents_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
)
"""

_CREATE_USER_KNOWLEDGE_SETTINGS = """
CREATE TABLE IF NOT EXISTS public.user_knowledge_settings (
    user_id           character varying(36) NOT NULL,
    tenant_id         character varying(36) NOT NULL,
    provider          character varying(16) NOT NULL,
    cloud_api_key_enc text,
    cloud_key_hash    character varying(64),
    status            character varying(16) NOT NULL DEFAULT 'disabled',
    last_checked_at   timestamp without time zone,
    error_message     text,
    created_at        timestamp without time zone NOT NULL DEFAULT now(),
    updated_at        timestamp without time zone NOT NULL DEFAULT now(),
    CONSTRAINT user_knowledge_settings_pkey PRIMARY KEY (user_id),
    CONSTRAINT user_knowledge_settings_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE,
    CONSTRAINT user_knowledge_settings_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)
)
"""

# 索引一起进迁移：它们与查询形状一一对应（列表页、管理统计、按库查文档、幂等回写）。
_INDEXES = (
    # 列表页：租户内看自己的库
    "CREATE INDEX IF NOT EXISTS ix_knowledge_bases_owner ON public.knowledge_bases (tenant_id, user_id)",
    # 管理统计 / 按状态巡检
    "CREATE INDEX IF NOT EXISTS ix_knowledge_bases_status ON public.knowledge_bases (tenant_id, status)",
    # index_ref 是 provider 侧的物理位置（自托管=索引目录，cloud=folder），重复即互相覆盖
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_knowledge_bases_index_ref "
    "ON public.knowledge_bases (provider, index_ref) WHERE index_ref IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_kb "
    "ON public.knowledge_documents (knowledge_base_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_tenant ON public.knowledge_documents (tenant_id)",
    # 同一 provider 文档在同一个库内只能登记一次（重建幂等的依据）
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_knowledge_documents_pageindex "
    "ON public.knowledge_documents (knowledge_base_id, pageindex_doc_id) "
    "WHERE pageindex_doc_id IS NOT NULL",
)


def upgrade() -> None:
    # 旧栈三张表整体退场。顺序：先切块（它引用另两张），再文档，最后知识库。
    op.execute("DROP TABLE IF EXISTS public.knowledge_chunks")
    op.execute("DROP TABLE IF EXISTS public.knowledge_documents")
    op.execute("DROP TABLE IF EXISTS public.knowledge_bases")

    op.execute(_CREATE_KNOWLEDGE_BASES)
    op.execute(_CREATE_KNOWLEDGE_DOCUMENTS)
    op.execute(_CREATE_USER_KNOWLEDGE_SETTINGS)
    for stmt in _INDEXES:
        op.execute(stmt)


def downgrade() -> None:
    """回到 0022 的表形状（结构可回，数据不可 —— 旧列已无处取回）。"""
    op.execute("DROP TABLE IF EXISTS public.user_knowledge_settings")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.knowledge_bases (
            id               character varying(36) NOT NULL,
            tenant_id        character varying(36),
            user_id          character varying(36),
            name             character varying(255),
            description      text,
            type             character varying(32),
            visibility       character varying(32),
            status           character varying(32),
            document_count   integer,
            total_size_bytes bigint,
            credits_consumed integer,
            config           json,
            created_at       timestamp without time zone,
            updated_at       timestamp without time zone,
            doc_count        integer,
            CONSTRAINT knowledge_bases_pkey PRIMARY KEY (id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.knowledge_documents (
            id                 character varying(36) NOT NULL,
            knowledge_base_id  character varying(36),
            tenant_id          character varying(36),
            user_id            character varying(36),
            name               character varying(255),
            file_url           character varying(1024),
            file_type          character varying(32),
            file_size_bytes    bigint,
            chunk_count        integer,
            status             character varying(32),
            error_message      text,
            metadata           json,
            created_at         timestamp without time zone,
            updated_at         timestamp without time zone,
            content            bytea,
            CONSTRAINT knowledge_documents_pkey PRIMARY KEY (id),
            CONSTRAINT knowledge_documents_knowledge_base_id_fkey
                FOREIGN KEY (knowledge_base_id) REFERENCES public.knowledge_bases(id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.knowledge_chunks (
            id                character varying(36) NOT NULL,
            document_id       character varying(36),
            knowledge_base_id character varying(36),
            tenant_id         character varying(36),
            chunk_index       integer,
            content           text NOT NULL,
            metadata          json,
            search_vector     character varying(255),
            created_at        timestamp without time zone,
            CONSTRAINT knowledge_chunks_pkey PRIMARY KEY (id),
            CONSTRAINT knowledge_chunks_document_id_fkey
                FOREIGN KEY (document_id) REFERENCES public.knowledge_documents(id),
            CONSTRAINT knowledge_chunks_knowledge_base_id_fkey
                FOREIGN KEY (knowledge_base_id) REFERENCES public.knowledge_bases(id)
        )
        """
    )
    # 0010 的全文索引随表重建后补回，downgrade 后形状与 0022 一致
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_content_fts
            ON public.knowledge_chunks
            USING gin (to_tsvector('simple', content))
        """
    )
