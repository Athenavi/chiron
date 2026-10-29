"""租户成员表 tenant_members —— 个人账号平滑升级为团队的地基。

背景（已核实）
--------------
`users.tenant_id` 是 `smallint`（`init.sql:728` / 基线同名列），单值。产品已定
「一期一人一租户」，所以不引入租户切换器，`users.tenant_id` 继续作为当前租户。
但单值列无法表达成员关系：谁是管理员、谁属于哪个租户、如何移除成员，都需要
一张关联表。`ent_user_roles(user_id, role_id)` 早已存在，却**没有 tenant_id 列**
（基线同一处），单靠它回答不了"属于哪个租户"，这正是本表的理由。

设计取舍
--------
1. **角色复用 `ent_roles`，不另设枚举**（产品 2026-10-29 决策）。因此本表存
   `role_id` 外键而非 `role varchar(16)`。代价是「谁是管理员」可能有两个真相，
   消解规则：`tenant_members.role_id` 是**租户内唯一**的角色真相，`users.role`
   （`owner`/`user`）只保留系统级语义，不再参与租户内判定。
2. **`UNIQUE (user_id)` 落实"一人一租户"**。靠数据库约束而不是代码自觉 ——
   否则并发邀请或手工 SQL 都能造出跨租户成员，`users.tenant_id` 立刻失真。
3. **`ent_roles` 没有 `(tenant_id, name)` 唯一约束**（基线只有 `ent_roles_pkey`），
   所以播种**不能用 `ON CONFLICT (tenant_id, name)`** —— 那会直接报
   "no unique or exclusion constraint matching the ON CONFLICT specification"。
   改用 `WHERE NOT EXISTS` 保证幂等。
4. **内置角色必须播种完整权限集**。`enterprise.LoadEffectivePerms` 的返回语义里
   「有角色记录但权限为空」等于**明确无权限**（`rbac.go:27-31`），与"没有角色记录
   时回退旧权限体系"是两回事。若播种出空 permissions，回填完成后所有用户会在一夜
   之间失去权限。权限集与 `auth.RolePermissions` 保持一致：
   owner/admin 全量（admin 少 `users:manage`），member 对齐系统级 `user`。

灰度顺序（roadmap §10.5）：本迁移只做「加表 + 播种 + 回填」，**不改读路径**。
权限判定仍读 `users.tenant_id` 与旧角色体系，出问题随时可 `downgrade` 回退。

Revision ID: 0008_tenant_members
Revises: 0007_invoice_requests
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_tenant_members"
down_revision: str | Sequence[str] | None = "0007_invoice_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 与 internal/auth/jwt.go 的 RolePermissions 保持一致（owner / admin / user）。
# 这里三个角色名用 owner / admin / member：前两者与系统级同名，member 对应系统级
# 的 "user" —— 租户内叫 member 比 user 更准确（他还是全站的 user）。
_OWNER_PERMS = (
    "'chat:write','chat:read','admin:read','admin:write','tools:execute',"
    "'users:manage','audit:read','ent:manage','policy:manage','market:manage','sso:manage'"
)
_ADMIN_PERMS = (
    "'chat:write','chat:read','admin:read','admin:write','tools:execute',"
    "'audit:read','ent:manage','policy:manage','market:manage','sso:manage'"
)
_MEMBER_PERMS = "'chat:write','chat:read','tools:execute'"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.tenant_members
        (
            tenant_id smallint     NOT NULL REFERENCES public.tenants (id),
            user_id   varchar(36)  NOT NULL REFERENCES public.users (id),
            -- 指向 ent_roles：租户内唯一的角色真相（见文件头取舍 1）。
            role_id   varchar(36)  NOT NULL REFERENCES public.ent_roles (id),
            joined_at timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, user_id)
        )
        """
    )

    # "一人一租户"的数据库级保证。没有它，同一 user_id 可以挂到多个 tenant，
    # 而 users.tenant_id 是单值列，两者必然冲突且无法判断哪个才是真的。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_tenant_members_user "
        "ON public.tenant_members USING btree (user_id)"
    )

    # 按 tenant 播种内置角色。ent_roles 无 (tenant_id, name) 唯一约束，
    # 因此用 NOT EXISTS 做幂等，不能写 ON CONFLICT。
    for name, display_name, perms in (
        ("owner", "Owner", _OWNER_PERMS),
        ("admin", "Admin", _ADMIN_PERMS),
        ("member", "Member", _MEMBER_PERMS),
    ):
        op.execute(
            f"""
            INSERT INTO public.ent_roles
                (id, tenant_id, name, display_name, is_builtin, permissions,
                 created_at, updated_at)
            SELECT gen_random_uuid()::text, t.id, '{name}', '{display_name}',
                   TRUE, ARRAY[{perms}], now(), now()
            FROM public.tenants t
            WHERE NOT EXISTS (
                SELECT 1 FROM public.ent_roles r
                WHERE r.tenant_id = t.id AND r.name = '{name}'
            )
            """
        )

    # 存量回填：users.role 三档映射到租户内角色，与 api.tenantRoleNameFor 口径一致。
    #
    # 必须是三档：admin.go:242 明确允许 users.role = 'admin'，若把 admin 也压成
    # member，C1-4 切读路径后这些存量管理员会**静默降权**（丢掉 admin:* /
    # audit:read），且没有任何提示。
    # 不带 ON CONFLICT 目标 —— PK (tenant_id, user_id) 与 ux_tenant_members_user
    # 都可能冲突，无目标形式两个都覆盖。
    op.execute(
        """
        INSERT INTO public.tenant_members (tenant_id, user_id, role_id)
        SELECT u.tenant_id, u.id, r.id
        FROM public.users u
        JOIN public.ent_roles r
          ON r.tenant_id = u.tenant_id
         AND r.name = CASE
                 WHEN u.role = 'owner' THEN 'owner'
                 WHEN u.role = 'admin' THEN 'admin'
                 ELSE 'member'
             END
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    # 先删表：它对本表播种的角色有外键依赖，顺序反了会因 RESTRICT 失败。
    op.execute("DROP TABLE IF EXISTS public.tenant_members")
    # 只清理本迁移播种的 is_builtin 角色，管理端自建的角色（is_builtin = FALSE）不动。
    op.execute(
        """
        DELETE FROM public.ent_roles
        WHERE is_builtin = TRUE AND name IN ('owner', 'admin', 'member')
        """
    )
