"""审计表 append-only：禁止 UPDATE（触发器实现）。

**为什么需要**：合规审计（等保、SOC2、ISO 27001）普遍要求「审计记录不可篡改」。
此前 `audit_logs` 与普通业务表无异 —— 持有应用凭据的任何人都能
`UPDATE audit_logs SET action = 'login_success' WHERE ...` 抹掉一次失败登录，
而这条记录看起来仍然"完整"。承诺"我们不会去改"不是控制措施。

**为什么用触发器而不是 REVOKE UPDATE**：
  - REVOKE 要写死数据库角色名（`chiron_app` / 部署方的自定义名），
    而角色名由部署环境决定，迁移里硬编码会在别人的环境里静默失效；
  - 触发器对**所有**角色生效（除超级用户与表属主另有手段），
    不依赖"部署时记得给正确的角色 REVOKE"这个人工步骤。

**为什么只拦 UPDATE，不拦 DELETE**：
  - DELETE 是**保留期清理**（`internal/api/audit_retention.go` 的
    `SweepAuditRetention`）的必需能力 —— 审计表会无限增长，
    合规也要求"到期删除"。拦掉 DELETE 等于让表撑爆磁盘。
  - 篡改的实质是"改写已发生的历史"，那正是 UPDATE。删除一条记录留下的是
    **空洞**（行数变少、序列不连续），比改写成"看起来正常"的内容更容易被发现。
  - 若后续要做到"连删除都可追溯"，方向是另建审计归档表 + 定期导出到
    对象存储（WORM），而不是把 DELETE 也堵死。

**边界说明（不要高估这层保护）**：表属主与超级用户仍可
`ALTER TABLE ... DISABLE TRIGGER` 后修改。本触发器防的是**应用层误改与
持应用凭据者的篡改**，不是防 DBA。真要做到 DBA 也无法篡改，需要 WORM 存储
或外部日志系统，属部署侧工作。

**迁移注意**：本触发器会阻止 `UPDATE audit_logs`。后续迁移若确实需要回填
（历史上 0004 就回填过 created_at），必须先
`ALTER TABLE public.audit_logs DISABLE TRIGGER audit_logs_no_update`，
改完再 `ENABLE`。**不要**因此删掉触发器。

Revision ID: 0018_audit_append_only
Revises: 0017_user_mfa
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0018_audit_append_only"
down_revision: Union[str, Sequence[str], None] = "0017_user_mfa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 触发器函数：任何 UPDATE 都直接抛错并中止事务。
    #
    # 用 `RAISE EXCEPTION` 而不是静默忽略：静默忽略会让"改成功了"的错觉延续，
    # 调用方以为写入生效、实际没生效 —— 那比明确报错糟糕得多。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.audit_logs_block_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_logs is append-only: UPDATE is not permitted (row id=%)', OLD.id
                USING ERRCODE = 'insufficient_privilege',
                      HINT = 'Audit records are immutable. To backfill during a migration, '
                             'temporarily DISABLE TRIGGER audit_logs_no_update.';
        END;
        $$;
        """
    )

    # CREATE TRIGGER 没有 IF NOT EXISTS，先 DROP 保证可重复执行。
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update ON public.audit_logs;")
    op.execute(
        """
        CREATE TRIGGER audit_logs_no_update
        BEFORE UPDATE ON public.audit_logs
        FOR EACH ROW
        EXECUTE FUNCTION public.audit_logs_block_update();
        """
    )


def downgrade() -> None:
    # 先删触发器再删函数：函数被触发器依赖时无法 DROP。
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update ON public.audit_logs;")
    op.execute("DROP FUNCTION IF EXISTS public.audit_logs_block_update();")
