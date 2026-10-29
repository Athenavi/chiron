package api

import (
	"context"
	"fmt"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// tenantExecer 抽象"能跑 SQL 的东西"，让同一段逻辑既能用在连接池（管理端、
// 补数脚本），也能用在事务（注册）。`*pgxpool.Pool` 与 `pgx.Tx` 都满足。
type tenantExecer interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// tenantBuiltinRole 描述一个租户内置角色。
//
// 关键点：权限集不在这里重复写一遍，而是从 auth.RolePermissions 取。否则
// 「播种出来的角色」与「系统级角色」会各说一套，而 enterprise.LoadEffectivePerms
// 的语义是「有角色记录但权限为空 = 明确无权限」（rbac.go:27-31）—— 一旦这里
// 的权限集落后于 jwt.go，用户会在无感知的情况下丢权限。
type tenantBuiltinRole struct {
	name        string // ent_roles.name
	displayName string
	sysRole     string // auth.RolePermissions 的键
}

var tenantBuiltinRoles = []tenantBuiltinRole{
	{name: "owner", displayName: "Owner", sysRole: "owner"},
	{name: "admin", displayName: "Admin", sysRole: "admin"},
	// 租户内叫 member 而非 user：他仍然是全站的 user，在团队里是成员。
	{name: "member", displayName: "Member", sysRole: "user"},
}

// ensureTenantBuiltinRoles 保证该租户的 owner/admin/member 三个内置角色存在。
//
// 为什么注册时也要调：`ent_roles` 没有任何 (tenant_id, name) 唯一约束，
// 迁移只能给「当时已存在的租户」播种；管理员事后用 POST /v1/admin/tenants
// 新建的租户没有角色。若注册时直接按 role_id 查不到就放弃写 tenant_members，
// 团队能力会在新租户上静默失效，所以这里补一次幂等播种。
//
// 幂等靠 NOT EXISTS：不能用 ON CONFLICT (tenant_id, name)，因为基线里
// ent_roles 只有主键约束，写上去会报 "no unique or exclusion constraint"。
func ensureTenantBuiltinRoles(ctx context.Context, ex tenantExecer, tenantID int16) error {
	for _, r := range tenantBuiltinRoles {
		perms := auth.RolePermissions[r.sysRole]
		if _, err := ex.Exec(ctx,
			`INSERT INTO ent_roles
			     (id, tenant_id, name, display_name, is_builtin, permissions,
			      created_at, updated_at)
			 SELECT gen_random_uuid()::text, $1, $2, $3, TRUE, $4::text[], now(), now()
			 WHERE NOT EXISTS (
			     SELECT 1 FROM ent_roles WHERE tenant_id = $1 AND name = $2
			 )`,
			tenantID, r.name, r.displayName, perms); err != nil {
			return fmt.Errorf("ensure builtin role %q for tenant %d: %w", r.name, tenantID, err)
		}
	}
	return nil
}

// tenantRoleIDFor 取租户内某个内置角色的 id。
//
// 加 ORDER BY 是必要的：ent_roles 没有 (tenant_id, name) 唯一约束，管理端可能
// 手工建出同名非内置角色。优先取内置的、再取最早创建的，保证同一租户下结果确定。
func tenantRoleIDFor(ctx context.Context, ex tenantExecer, tenantID int16, name string) (string, error) {
	var roleID string
	err := ex.QueryRow(ctx,
		`SELECT id FROM ent_roles
		 WHERE tenant_id = $1 AND name = $2
		 ORDER BY is_builtin DESC NULLS LAST, created_at ASC NULLS LAST, id ASC
		 LIMIT 1`,
		tenantID, name).Scan(&roleID)
	if err != nil {
		return "", fmt.Errorf("resolve tenant role %q for tenant %d: %w", name, tenantID, err)
	}
	return roleID, nil
}

// tenantRoleNameFor 把全站角色（users.role）映射成租户内角色名（ent_roles.name）。
//
// 必须是**三档**映射，不能只判 owner/其余：
//   owner → owner
//   admin → admin     ← admin.go:242 明确允许 users.role = 'admin'
//   user/其它 → member
//
// 早前这里写的是「owner → owner，其余一律 member」，会把全站 admin 静默降成
// 普通成员 —— 而 C1-4 之后权限判定优先读 tenant_members，降权会真实生效：
// 存量管理员丢掉 admin:* / audit:read，且没有任何提示。迁移 0008 的回填
// 必须与此处口径完全一致。
func tenantRoleNameFor(sysRole string) string {
	switch sysRole {
	case "owner":
		return "owner"
	case "admin":
		return "admin"
	default:
		return "member"
	}
}

// addTenantMember 在注册事务内建立 (tenantID, userID) 的成员关系。
//
// sysRole 是 users.role 的取值（owner / admin / user），经 tenantRoleNameFor
// 映射成租户内角色。这与迁移 0008 的回填口径一致，保证「新注册的用户」和
// 「迁移回填的老用户」拿到同样的角色。
//
// 与 users 的 INSERT 必须在同一事务：两者分开写会出现「用户已存在但没有成员
// 记录」的窗口，而读取路径一旦切到 tenant_members，这些用户就会凭空消失。
func addTenantMember(ctx context.Context, ex tenantExecer, tenantID int16, userID, sysRole string) error {
	if err := ensureTenantBuiltinRoles(ctx, ex, tenantID); err != nil {
		return err
	}
	roleID, err := tenantRoleIDFor(ctx, ex, tenantID, tenantRoleNameFor(sysRole))
	if err != nil {
		return err
	}
	// ON CONFLICT 不带目标：PK (tenant_id, user_id) 与 ux_tenant_members_user
	// 两处都可能冲突，无目标形式两个都覆盖。
	if _, err := ex.Exec(ctx,
		`INSERT INTO tenant_members (tenant_id, user_id, role_id)
		 VALUES ($1, $2, $3)
		 ON CONFLICT DO NOTHING`,
		tenantID, userID, roleID); err != nil {
		return fmt.Errorf("add tenant member: %w", err)
	}
	return nil
}
