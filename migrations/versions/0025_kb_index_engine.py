"""knowledge_bases 增 index_engine（建立该库索引时的检索引擎版本）

Revision ID: 0025_kb_index_engine
Revises: 0024_kb_cloud_folder
Create Date: 2026-10-19

**为什么要记录这个。** 索引是**引擎侧的产物**：自托管（PageIndex local）把树写在
``storage_path`` 下的一堆文件里，格式由 SDK 决定 —— 而 SDK 迭代以小时计（0.4.3 → 0.4.4
同日发布）。一旦升级 SDK，旧索引**可能读不出来**，表现是"文档明明在、查询却是空的"。

有了这一列，升级流程就从"祈祷兼容"变成可核对的事实：

* 索引成功时由 worker 写入当时的 ``index_engine``（如 ``pageindex==0.4.4``）；
* 升级后只要 ``index_engine`` 与当前镜像里的版本不一致，该库就应**重建索引**
  （运维入口/前端提示据此判断，而不是靠人记住"上次是哪个版本建的"）。

**不自动重建**：重建要重跑 LLM（时间与费用），必须由人决定何时做。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0025_kb_index_engine"
down_revision: Union[str, Sequence[str], None] = "0024_kb_cloud_folder"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.knowledge_bases
            ADD COLUMN IF NOT EXISTS index_engine character varying(64)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.knowledge_bases
            DROP COLUMN IF EXISTS index_engine
        """
    )
