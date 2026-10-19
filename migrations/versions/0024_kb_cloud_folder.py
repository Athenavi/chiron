"""user_knowledge_settings 增 cloud_folder_id（PageIndex Cloud 的用户级 folder）

Revision ID: 0024_kb_cloud_folder
Revises: 0023_knowledge_pageindex
Create Date: 2026-10-19

**为什么需要一列 folder_id。** PageIndex Cloud 的账号是一个平的空间：知识库文档、用户
自己在 PageIndex 控制台上传的文档，全都在同一个账号里。Chiron 侧能做的隔离有两层：

* 检索时显式传本库的 ``doc_id`` 列表（``service.answer_question`` 按"已就绪文档"收敛）；
* 上传时把文档放进 **以 Chiron 用户 id 命名的 folder**，其下按知识库再分一层子 folder ——
  这样"哪些文档是 Chiron 放进去的"在账号里可见、可审计、可批量定位。

**这不是安全边界，别当成安全边界用。** PageIndex 的 ``folder_id`` 官方语义是
"steer discovery toward one folder"（引导发现方向），``doc_id`` 同理 —— 它们都**不限制**
工具访问范围（官方文档原话：context "does not restrict tool access"）。所以真正的加固在
引擎侧：见 ``app/knowledge/providers/cloud.py`` 的**引用归属校验**（越界的引用直接判定为
异常、拒绝返回），以及"绝不把账号级浏览工具暴露给 agent"（``agent_tools`` 保持未接线）。

folder 是**用户级**的（一个 Chiron 用户一个顶层 folder），所以它落在用户设置表上，而不是
``knowledge_bases.index_ref``；后者记的是**库级**物理位置（自托管的索引目录 / Cloud 的库子
folder），在那里才有"每个库一个位置"的唯一性约束。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0024_kb_cloud_folder"
down_revision: Union[str, Sequence[str], None] = "0023_knowledge_pageindex"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.user_knowledge_settings
            ADD COLUMN IF NOT EXISTS cloud_folder_id character varying(64)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.user_knowledge_settings
            DROP COLUMN IF EXISTS cloud_folder_id
        """
    )
