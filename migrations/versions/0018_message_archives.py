"""message_archives：超大原文的对象存储归档 + messages.blob_ref

背景（已核实）
--------------
`messages.content` 是 `text`，PG 单列上限 1GB，但**撑大表会拖慢所有查询**：
`messages` 的每一页查询都要扫到这张表，单行上 MB 的正文会把 TOAST 与索引一起拖重。

设计文档（`docs/context-and-delegation-design.md` §4.2）定了两道阈值：

| 大小 | 存哪里 | 上下文里放什么 |
| --- | --- | --- |
| ≤ 64KB | `messages.content` 原样 | 原样全文 |
| 64KB ~ 4MB | `messages.content` 原样（TOAST 自动压缩） | 结构摘要 + 全文引用 |
| > 4MB | `messages.content` 存截断体；全文进对象存储 | 结构摘要 + 两段引用 |

本迁移只提供"存哪里"的落点，不实现搬运逻辑（那在引擎侧）。

为什么不复用 media_assets
------------------------
媒体资产是**用户可下载的产物**，归档是**内部可召回的原文**。混用会让
"清理媒体"误删归档，而两者的保留策略相反（媒体可删，归档必须活到会话结束）。
`app/media/store.py` 的 `BaseStore` 抽象仍复用，但走独立前缀 `archives/`。

为什么不加 tenant_id
--------------------
`session_id → sessions.tenant_id` 是 `NOT NULL` 的可靠链（`init.sql:876-881`）。
冗余一列只会制造两处不一致的可能，而跨租户校验本来就该走 sessions。

为什么 storage_key 不进任何对外响应
-----------------------------------
`storage_key` 是物理路径。暴露它等于给出"构造任意路径"的起点，也让
"归档路径穿越"这类攻击有了明确的目标。对外只给 `ref`，服务端自己查表拿 key。
**服务端生成、不接受客户端传入**，拼接后必须 realpath 校验前缀。

blob_ref 与 ref 的区别
-----------------------
- `messages.blob_ref`：**指向本表 `ref`** 的指针，挂在具体消息行上。
  为空表示该消息正文完整地就在 `content` 里。
- `message_archives.ref`：归档自身的稳定标识（`arc_<12hex>`），
  按它才能定位到对象存储里的那段字节。

两者分开而不是合并成一个列：一条消息可以被多次归档（重试、编辑后重存），
而归档本身的生命周期独立于任何一条消息行。

Revision ID: 0018_message_archives
Revises: 0017_messages_tool_call_id
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018_message_archives"
down_revision: str | Sequence[str] | None = "0017_messages_tool_call_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS：可重入，重复执行不报错（与 0013–0017 同一取向）。
    op.execute(
        "CREATE TABLE IF NOT EXISTS public.message_archives "
        "( "
        "    ref          varchar(64) NOT NULL PRIMARY KEY, "
        "    session_id   varchar(36) NOT NULL "
        "        REFERENCES public.sessions ON DELETE CASCADE, "
        "    byte_size    bigint NOT NULL, "
        "    storage_key  text NOT NULL, "
        "    sha256       char(64) NOT NULL, "
        "    created_at   timestamptz NOT NULL DEFAULT now() "
        ")"
    )
    # 按会话查归档：清理会话时要能一次找出全部归档再删对象，
    # 单列索引足够（查询形态只有 `WHERE session_id = $1`）。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_message_archives_session "
        "ON public.message_archives (session_id)"
    )
    # 可空：绝大多数消息的正文完整地就在 content 里，只有 > 4MB 的才带指针。
    # 不加外键：归档先于消息写入（先落对象存储再写 messages），
    # 若加 FK 会让写入顺序受制于约束，且删除归档时不该阻塞消息行。
    op.execute(
        "ALTER TABLE public.messages ADD COLUMN IF NOT EXISTS blob_ref varchar(64)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.messages DROP COLUMN IF EXISTS blob_ref")
    op.execute("DROP INDEX IF EXISTS public.ix_message_archives_session")
    op.execute("DROP TABLE IF EXISTS public.message_archives")
