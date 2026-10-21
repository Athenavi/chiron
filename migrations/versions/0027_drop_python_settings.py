"""清理 system_settings 里已废弃的 python 段配置（M25）

Revision ID: 0027_drop_python_settings
Revises: 0026_run_usage_settlements
Create Date: 2026-10-21

背景（M25）：后台「系统设置 → Python 引擎」那一段（`llm_provider` / `llm_model` /
`llm_api_key` / `llm_base_url` / `embedding_model` / `max_turns` /
`queue_worker_concurrency` / `cache_l1_capacity`）**从来没有读取点** ——
引擎侧的 provider / 模型 / 密钥走后台「模型服务」+ `llm_provider_keys` + KeyRing，
`embedding_model` / `cache_l1_capacity` / `queue_worker_concurrency` 走引擎 env
（`python-engine/app/config.py` 的 settings），而 `max_turns` 又与 `agent.max_turns`
是同一个量。设置页那一段已整段摘除（用户拍板），这里把**旧库里已经存下的键**一并清掉，
避免留下"看起来还在生效"的残留。

⚠ `downgrade` 是**空操作**：被删的是"从未生效的配置"，没有可恢复的语义。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0027_drop_python_settings"
down_revision: Union[str, Sequence[str], None] = "0026_run_usage_settlements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM system_settings WHERE category = 'python'")


def downgrade() -> None:
    # 有意留空：这些键从未被任何代码读取，恢复它们只会重新制造"看起来生效"的假象。
    pass
