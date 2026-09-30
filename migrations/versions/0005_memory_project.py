"""user_memory_entries 增项目维 project（方案 02 §4 · C3）

Revision ID: 0005_memory_project
Revises: 0004_subagent_inherited_messages
Create Date: 2026-10-06

C3 的验收是「同用户两个项目**互不串记忆**」。这需要三件事一起变：

1. **加列**：`project character varying(120) NOT NULL DEFAULT ''` —— 空串 = 未分组
   （方案 02 §4：「`project` 为**可空字符串**、空 = 未分组」，不新造 `NULL` 语义：
   列非空 + 默认空串，既有行自动成为"未分组"，无需回填）。
2. **唯一约束加一维**：原约束是 `(tenant_id, user_id, slot, item_key)` —— 不加 `project`
   的话，同一个 `slot+item_key` 在两个项目里会**互相覆盖**（第二条 INSERT 被 ON CONFLICT
   吃掉），"隔离"就是假的。这里原地替换同名约束，不引入新名字。
3. **检索索引**：主查询形态变成 `tenant + user + project (+ status)`。
   既有的 `ix_user_memory_entries_lookup` 保留 —— 它仍服务"不带 project"的旧查询。

``IF NOT EXISTS`` / ``IF EXISTS`` 与既有迁移风格一致（某些环境可能已手工改过）。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005_memory_project"
down_revision: Union[str, Sequence[str], None] = "0004_subagent_inherited_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_memory_entries
            ADD COLUMN IF NOT EXISTS project character varying(120) NOT NULL DEFAULT ''
        """
    )
    # 唯一约束必须含 project，否则跨项目同 key 会互相覆盖
    op.execute(
        "ALTER TABLE user_memory_entries "
        "DROP CONSTRAINT IF EXISTS user_memory_entries_identity_uniq"
    )
    op.execute(
        "ALTER TABLE ONLY user_memory_entries "
        "ADD CONSTRAINT user_memory_entries_identity_uniq "
        "UNIQUE (tenant_id, user_id, project, slot, item_key)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_memory_entries_project "
        "ON public.user_memory_entries USING btree (tenant_id, user_id, project, status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_user_memory_entries_project")
    op.execute(
        "ALTER TABLE user_memory_entries "
        "DROP CONSTRAINT IF EXISTS user_memory_entries_identity_uniq"
    )
    # 回退前必须清掉跨项目重复：旧约束容不下"同用户同 key 出现在多个项目"
    op.execute(
        "DELETE FROM user_memory_entries a USING user_memory_entries b "
        "WHERE a.tenant_id = b.tenant_id AND a.user_id = b.user_id "
        "AND a.slot = b.slot AND a.item_key = b.item_key AND a.id <> b.id "
        "AND a.updated_at < b.updated_at"
    )
    op.execute(
        "ALTER TABLE ONLY user_memory_entries "
        "ADD CONSTRAINT user_memory_entries_identity_uniq "
        "UNIQUE (tenant_id, user_id, slot, item_key)"
    )
    op.execute("ALTER TABLE user_memory_entries DROP COLUMN IF EXISTS project")
