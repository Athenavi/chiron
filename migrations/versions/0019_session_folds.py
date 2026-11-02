"""session_folds：压缩改为「投影」后，把被折叠区间记成一行摘要

背景（已核实）
--------------
压缩（`compaction.py`）目前只改**请求里**的消息列表，不落库。于是
"这段上下文被折叠过"这件事在库里没有痕迹：原文仍在 `messages`，
但没有任何记录说明它是被哪次摘要、覆盖了哪个区间而离开上下文的。

`read_archive` 要真正可用，模型必须先**知道**某段原文的 `messages.id`。
而 `compaction.py:320-350` 的裁剪通知目前只给 `sn_xxx`（Redis
`result_store`，2 小时 TTL），拿不到持久表里的 id。本表是补这条链的地基：
折叠时记下 `[covered_from, covered_to)`，通知里就能带上区间与摘要。

为什么区间用 `messages.seq` 而不是 id
-------------------------------------
`seq` 是 `GENERATED ALWAYS AS IDENTITY`（迁移 0016），单调且无并发歧义；
用 id 表达区间无法回答"这条消息在折叠边界之内吗"。半开区间
`[covered_from, covered_to)` 让"相邻两次折叠首尾相接"成为可判定的性质。

为什么不加 tenant_id
--------------------
`session_id → sessions.tenant_id` 是 NOT NULL 的可靠链（`init.sql:876-881`），
冗余一列只会制造两处不一致。

`body_suffix` 的用途
--------------------
记录折叠发生时**尾部未被覆盖**的消息条数。投影 = [冻结体][活动摘要][原样尾部]，
没有它就无从判断"摘要之后还该拼多少条原消息"，跨副本重建上下文会错位。

Revision ID: 0019_session_folds
Revises: 0018_message_archives
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019_session_folds"
down_revision: str | Sequence[str] | None = "0018_message_archives"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.session_folds "
        "( "
        "    id            varchar(36) NOT NULL PRIMARY KEY, "
        "    session_id    varchar(36) NOT NULL "
        "        REFERENCES public.sessions ON DELETE CASCADE, "
        "    covered_from  bigint NOT NULL, "
        "    covered_to    bigint NOT NULL, "
        "    digest        text NOT NULL, "
        "    digest_model  varchar(128) NOT NULL, "
        "    digest_prompt varchar(32) NOT NULL, "
        "    body_suffix   bigint NOT NULL, "
        "    created_at    timestamptz NOT NULL DEFAULT now(), "
        "    CONSTRAINT session_folds_range_ordered CHECK (covered_to > covered_from) "
        ")"
    )
    # 查询形态只有"取本会话最近一次折叠"：session 收窄 + covered_to DESC 保序，
    # 复合索引可让 PG 直接取第一条而不用排序。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_folds_session "
        "ON public.session_folds (session_id, covered_to DESC)"
    )
    # 同一区间只能有一条折叠记录。
    #
    # 为什么这条约束是必需的：`messages` 表在压缩时一个字节都不动，所以
    # **同一段 seq 会反复被选中**。缓存 TTL 过期后从 PG 重读全量再裁同一段、
    # 或另一副本处理同一会话，都会再 INSERT 一次。没有约束时，重复行会让
    # 投影里的摘要被拼接多遍 —— 越老的会话，重复越多。
    # 配合 `persist_folds` 的 `ON CONFLICT DO NOTHING`，写入幂等。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_session_folds_range "
        "ON public.session_folds (session_id, covered_from, covered_to)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_session_folds_session")
    op.execute("DROP TABLE IF EXISTS public.session_folds")
