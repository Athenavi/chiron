"""messages 增加 attachments：把"这一轮用户发了哪些附件"落到消息行上。

**为什么要有这一列**：附件改为引用化之后，历史里只剩 `<attachment id="…" />` 引用块
（见 `python-engine/app/agent/runtime.py` 的 `fold_attachment_parts_for_persistence`），
内容不再驻留。前端的附件 chip、以及"刷新后还能按 asset_id 重新签名取图"，都需要
**结构化**的附件清单 —— 从正文里反解 `<attachment>` 标签是把展示契约压在文本格式上，
一旦引用块的措辞调整（改文案、加字段）回放就会静默失效。

**为什么是 jsonb 且带默认值**：

- 默认 `'[]'::jsonb NOT NULL` 让存量行无需回填即可满足非空约束，写入方也不必在
  没有附件时显式写空数组 —— 向后兼容是"扩展-迁移-收缩"里的第一步；
- 只增列、不改既有列，旧代码（不认识该列的查询）完全不受影响；读侧一律用
  `COALESCE(to_jsonb(messages)->>'attachments', '[]')`，即使迁移尚未执行也只会拿到空数组，
  不会让整条历史查询失败。

**不建索引**：该列只按消息行随读随用，没有按附件内容检索的需求；建索引只会给写入加成本。
真要"按 asset 找消息"，应当查媒体库侧而不是翻消息表。

Revision ID: 0002_messages_attachments
Revises: 0001_authoritative_baseline
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002_messages_attachments"
down_revision: Union[str, Sequence[str], None] = "0001_authoritative_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "messages"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE public.{_TABLE}
            ADD COLUMN IF NOT EXISTS attachments jsonb DEFAULT '[]'::jsonb NOT NULL
        """
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE public.{_TABLE} DROP COLUMN IF EXISTS attachments")
