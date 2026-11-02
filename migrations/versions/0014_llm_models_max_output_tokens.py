"""llm_models 增加 max_output_tokens：模型的最大输出 token 数

背景（已核实）
--------------
输出上限此前**没有任何存储**：所有调用点都写死 4096（`app/config.py:159`、
`gateway/provider.py`、`providers/openai.py`），前端则按对话模式硬编码
（常规 4096 / 极简 1024）。这两处都有同一个问题 —— 同一个模式换个模型，
输出上限本来就该跟着模型变。

`llm_models` 只有 `context_window`（输入+输出共享的窗口），**不能**用它推导
输出上限。反例（LiteLLM `model_prices_and_context_window.json` 同源数据）：

    模型                  context_window   max_output_tokens   比值
    deepseek/deepseek-chat      131072            8192          1/16
    deepseek/deepseek-reasoner  131072           65536          1/2

同一家、同一窗口，输出上限差 8 倍。DeepSeek 官方文档还把"默认 8K / 思考 64K /
上限 384K"三者分开表述，任何单一比例都表达不了。所以输出上限必须是
**按模型的独立元数据**（与 LiteLLM、models.dev 的做法一致），而不是换算系数。

为什么允许 NULL
---------------
存量行没有这个信息，无法凭空补。NULL 的语义是"未知"，读取方回落到
`DEFAULT_MAX_OUTPUT_TOKENS`（与 `settings.default_max_tokens` 同值，保持历史行为）。
这与 `context_window` 的处理一致：查不到就回落，不阻断对话 —— 它是调参输入，
不是安全边界。

为什么不加 CHECK 约束
--------------------
约束 `> 0` 看似合理，但会让运维"先插空值再补"的正常流程失败。语义上
`NULL`/`<= 0` 一律按未知处理（见 `model_window.max_output_tokens`），
在读取侧收敛比在写入侧拦截更能容忍脏数据。

Revision ID: 0014_llm_models_max_output_tokens
Revises: 0013_cron_jobs_tenant_index
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014_llm_models_max_output_tokens"
down_revision: str | Sequence[str] | None = "0013_cron_jobs_tenant_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS：可重入，重复执行不报错（与 0013 同一取向）。
    op.execute(
        "ALTER TABLE public.llm_models "
        "ADD COLUMN IF NOT EXISTS max_output_tokens integer"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.llm_models DROP COLUMN IF EXISTS max_output_tokens")
