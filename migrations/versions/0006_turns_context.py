"""turns 增加 context：持久化每一轮的上下文快照。

**为什么需要**：状态栏的「上下文构成」（系统提示 / 工具定义 / 你的输入 / 模型回复 /
工具输出）由引擎在 done 事件里随 `context` 下发。网关此前只在 SSE 里转发它 ——
刷新页面或切回历史会话时，那条事件早已过去，前端只能显示"上下文占用：暂无数据"。

把最近一份快照落到回合行上，`/v1/sessions/{id}/metrics` 就能回放它，
历史会话也能看到构成；`compact_ratio` / `largest` 一并保留。

Revision ID: 0006_turns_context
Revises: 0005_model_price_verified_sources
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0006_turns_context"
down_revision: Union[str, Sequence[str], None] = "0005_model_price_verified_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.turns ADD COLUMN IF NOT EXISTS context jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE public.turns DROP COLUMN IF EXISTS context")
