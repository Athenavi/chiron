package api

import (
	"context"
	"slices"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 内置角色播种 + 成员关系写入的**真实 PostgreSQL** 用例。
//
// ## 为什么必须是真库
//
// 现场是桌面端本地模式首启 500（POST /v1/auth/session）：
//
//	ensure builtin role "owner" for tenant 1:
//	ERROR: inconsistent types deduced for parameter $2 (SQLSTATE 42P08)
//
// 这是 PostgreSQL 在 **Parse 阶段**报的错：同一条语句里 $2 既出现在
// `INSERT … SELECT` 的目标列（推成 text），又出现在 `name = $2`（推成
// varchar(64)），两种推断冲突 ⇒ 语句根本没执行就被拒。它跟表里有没有数据、
// 租户是否已有角色**全都无关**，因此：
//
//   - `strings.Contains(sql, …)` 这一类字符串断言看不见（SQL 文本当时是对的）；
//   - 假 Tx / 假 Pool 看不见（它们只记录语句，不解析）；
//   - 只有真实 PostgreSQL 才会红。
//
// 同一个原因，注册（auth.go:476）、邮件/短信建号、SSO 首登、管理端补播种
// （admin_ops.go:227）在真实库上都会 500 —— 此前没有任何 live 用例覆盖这段，
// 所以它一路发到桌面安装包上才暴露。
//
// 取值口径与其他 `*_live_test.go` 一致：`CHIRON_TEST_POSTGRES_DSN` 未设置则 skip，
// CI 的 real-stack job 用 `-run 'Live|Diag'` 真正执行。

// liveBuiltinTenant 建一个只属于本次用例的租户，并在结束时把它名下的
// members → roles → users → tenant 一次清干净。
//
// 清理集中在这一处，是因为 t.Cleanup 按 LIFO 跑：用户清理若挂在
// liveBuiltinUser 上，会先于 tenant_members 的删除执行，外键（
// tenant_members_user_id_fkey）直接把删除顶回去，测试留下残渣。
// ent_roles.tenant_id 与 tenant_members.role_id 也各有外键，顺序不能反。
func liveBuiltinTenant(t *testing.T, pool *pgxpool.Pool) int16 {
	t.Helper()
	ctx := context.Background()

	var tenantID int16
	if err := pool.QueryRow(ctx,
		`INSERT INTO tenants (name, status) VALUES ($1, 'active') RETURNING id`,
		"builtin-roles-live").Scan(&tenantID); err != nil {
		t.Fatalf("建测试租户失败: %v", err)
	}

	t.Cleanup(func() {
		cleanup := context.Background()
		for _, stmt := range []string{
			`DELETE FROM tenant_members WHERE tenant_id = $1`,
			`DELETE FROM ent_roles WHERE tenant_id = $1`,
			// 本用例的用户只可能挂在本用例的租户下，按租户删即可。
			`DELETE FROM users WHERE tenant_id = $1`,
			`DELETE FROM tenants WHERE id = $1`,
		} {
			if _, err := pool.Exec(cleanup, stmt, tenantID); err != nil {
				t.Logf("清理租户 %d 失败 (%s): %v", tenantID, stmt, err)
			}
		}
	})
	return tenantID
}

// liveBuiltinUser 在建好的租户里插一个用户，返回 id（删除由 liveBuiltinTenant 的清理负责）。
func liveBuiltinUser(t *testing.T, pool *pgxpool.Pool, tenantID int16, role string) string {
	t.Helper()
	userID := uuid.NewString()
	// id 必须显式给：users.id 是 varchar(36) NOT NULL 且无默认值。
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO users (id, tenant_id, email, name, password_hash, role, password_set)
		 VALUES ($1, $2, $3, $4, 'not-a-real-hash', $5, FALSE)`,
		userID, tenantID, "live-"+userID+"@chiron.test", "live", role); err != nil {
		t.Fatalf("建测试用户失败: %v", err)
	}
	return userID
}

// builtinRoleCount 数某租户下的 ent_roles 行数（内置与非内置一起数）。
func builtinRoleCount(t *testing.T, pool *pgxpool.Pool, tenantID int16) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM ent_roles WHERE tenant_id = $1`, tenantID).Scan(&n); err != nil {
		t.Fatalf("统计角色失败: %v", err)
	}
	return n
}

// 空库、零角色：播种必须真的能跑起来，并且把权限**写全**。
//
// 权限非空不是锦上添花：enterprise.LoadEffectivePerms 的语义是
// 「有角色记录但权限为空 = 明确无权限」（rbac.go:27-31）。播种出一行权限为空的
// owner，界面会照常显示"我是所有者"，实际每次调用都 403 —— 比报错更难查。
func TestEnsureTenantBuiltinRolesLive_SeedsFreshTenant(t *testing.T) {
	pool := liveInvitePool(t)
	tenantID := liveBuiltinTenant(t, pool)

	if err := ensureTenantBuiltinRoles(context.Background(), pool, tenantID); err != nil {
		t.Fatalf("新租户播种失败（42P08 复发）: %v", err)
	}

	if got := builtinRoleCount(t, pool, tenantID); got != len(tenantBuiltinRoles) {
		t.Errorf("角色数 = %d, want %d", got, len(tenantBuiltinRoles))
	}

	var names []string
	rows, err := pool.Query(context.Background(),
		`SELECT name FROM ent_roles WHERE tenant_id = $1 ORDER BY name`, tenantID)
	if err != nil {
		t.Fatalf("读角色名失败: %v", err)
	}
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			rows.Close()
			t.Fatalf("扫描角色名失败: %v", err)
		}
		names = append(names, n)
	}
	rows.Close()
	wantNames := make([]string, 0, len(tenantBuiltinRoles))
	for _, r := range tenantBuiltinRoles {
		wantNames = append(wantNames, r.name)
	}
	slices.Sort(wantNames)
	if !slices.Equal(names, wantNames) {
		t.Errorf("角色名 = %v, want %v", names, wantNames)
	}

	for _, r := range tenantBuiltinRoles {
		var (
			perms     []string
			builtin   bool
			dispName  string
			roleIDLen int
		)
		err := pool.QueryRow(context.Background(),
			`SELECT permissions, is_builtin, display_name, length(id)
			 FROM ent_roles WHERE tenant_id = $1 AND name = $2`,
			tenantID, r.name).Scan(&perms, &builtin, &dispName, &roleIDLen)
		if err != nil {
			t.Fatalf("读角色 %s 失败: %v", r.name, err)
		}
		if !builtin {
			t.Errorf("角色 %s 的 is_builtin = false", r.name)
		}
		if dispName != r.displayName {
			t.Errorf("角色 %s 的 display_name = %q, want %q", r.name, dispName, r.displayName)
		}
		// id 是 varchar(36)：gen_random_uuid()::text 正好 36，超长会被截断报错。
		if roleIDLen > 36 {
			t.Errorf("角色 %s 的 id 长度 = %d, 超过 varchar(36)", r.name, roleIDLen)
		}
		want := auth.RolePermissions[r.sysRole]
		if len(want) == 0 {
			t.Fatalf("auth.RolePermissions[%q] 为空：播种口径本身出了问题", r.sysRole)
		}
		if len(perms) != len(want) {
			t.Errorf("角色 %s 权限数 = %d, want %d（%v）", r.name, len(perms), len(want), want)
			continue
		}
		for _, p := range want {
			if !slices.Contains(perms, p) {
				t.Errorf("角色 %s 缺权限 %q（实得 %v）", r.name, p, perms)
			}
		}
	}
}

// 播种是幂等的，而且**只补不写**：重复调用既不累积重复行，也不覆盖管理员改过的权限。
//
// ent_roles 上没有 (tenant_id, name) 唯一约束，重复播种只能靠 NOT EXISTS；
// 一旦这里改成"存在就更新"，管理员在后台调过的角色权限会被每次注册悄悄打回默认值。
func TestEnsureTenantBuiltinRolesLive_IdempotentAndNonDestructive(t *testing.T) {
	pool := liveInvitePool(t)
	ctx := context.Background()
	tenantID := liveBuiltinTenant(t, pool)

	if err := ensureTenantBuiltinRoles(ctx, pool, tenantID); err != nil {
		t.Fatalf("第一次播种失败: %v", err)
	}
	// 模拟管理员收紧 owner 权限。
	if _, err := pool.Exec(ctx,
		`UPDATE ent_roles SET permissions = $1 WHERE tenant_id = $2 AND name = 'owner'`,
		[]string{"audit:read"}, tenantID); err != nil {
		t.Fatalf("改写 owner 权限失败: %v", err)
	}

	for i := 2; i <= 3; i++ {
		if err := ensureTenantBuiltinRoles(ctx, pool, tenantID); err != nil {
			t.Fatalf("第 %d 次播种失败: %v", i, err)
		}
	}
	if got := builtinRoleCount(t, pool, tenantID); got != len(tenantBuiltinRoles) {
		t.Errorf("重复播种后角色数 = %d, want %d（NOT EXISTS 失效）", got, len(tenantBuiltinRoles))
	}

	var perms []string
	if err := pool.QueryRow(ctx,
		`SELECT permissions FROM ent_roles WHERE tenant_id = $1 AND name = 'owner'`,
		tenantID).Scan(&perms); err != nil {
		t.Fatalf("读 owner 权限失败: %v", err)
	}
	if !slices.Equal(perms, []string{"audit:read"}) {
		t.Errorf("owner 权限被播种覆盖成 %v, 应保持 [audit:read]", perms)
	}
}

// 事务内建号 → 成员关系的完整形状（ensureLocalOwner / Register 都走这条）。
//
// 单列一条 SQL 能跑，不代表放进事务能跑：本地引导是
// `Begin → set_config → advisory_xact_lock → INSERT users → addTenantMember → Commit`，
// 而 addTenantMember 里的三条语句共用同一个 tx。这里按同样的形状跑一遍，
// 并验证 users.role → ent_roles.name 的三档映射在真库上落到正确的 role_id。
func TestAddTenantMemberLive_ResolvesBuiltinRoleInTransaction(t *testing.T) {
	pool := liveInvitePool(t)
	ctx := context.Background()
	tenantID := liveBuiltinTenant(t, pool)

	cases := map[string]string{ // users.role → ent_roles.name
		"owner": "owner",
		"admin": "admin",
		"user":  "member",
	}
	for sysRole, wantName := range cases {
		userID := liveBuiltinUser(t, pool, tenantID, sysRole)

		tx, err := pool.Begin(ctx)
		if err != nil {
			t.Fatalf("Begin 失败: %v", err)
		}
		if _, err := tx.Exec(ctx,
			"SELECT set_config('app.current_tenant_id', $1, true)", tenantStr(tenantID)); err != nil {
			tx.Rollback(ctx)
			t.Fatalf("设置租户上下文失败: %v", err)
		}
		if err := addTenantMember(ctx, tx, tenantID, userID, sysRole); err != nil {
			tx.Rollback(ctx)
			t.Fatalf("addTenantMember(role=%s) 失败: %v", sysRole, err)
		}
		if err := tx.Commit(ctx); err != nil {
			t.Fatalf("提交失败: %v", err)
		}

		var gotName string
		err = pool.QueryRow(ctx,
			`SELECT er.name FROM tenant_members tm
			 JOIN ent_roles er ON er.id = tm.role_id AND er.tenant_id = tm.tenant_id
			 WHERE tm.user_id = $1`, userID).Scan(&gotName)
		if err != nil {
			t.Fatalf("读回 %s 的租户角色失败: %v", sysRole, err)
		}
		if gotName != wantName {
			t.Errorf("users.role=%q 映射到租户角色 %q, want %q（静默降权/提权）",
				sysRole, gotName, wantName)
		}
	}

	// 同一用户再走一次（重启/重复注册）：ON CONFLICT DO NOTHING 必须扛住，
	// 且角色不能被改写 —— ux_tenant_members_user 让一人只能有一个租户成员身份。
	first := liveBuiltinUser(t, pool, tenantID, "user")
	if err := addTenantMember(ctx, pool, tenantID, first, "user"); err != nil {
		t.Fatalf("首次写入成员失败: %v", err)
	}
	if err := addTenantMember(ctx, pool, tenantID, first, "owner"); err != nil {
		t.Fatalf("重复写入成员失败: %v", err)
	}
	var stillMember string
	if err := pool.QueryRow(ctx,
		`SELECT er.name FROM tenant_members tm
		 JOIN ent_roles er ON er.id = tm.role_id
		 WHERE tm.user_id = $1`, first).Scan(&stillMember); err != nil {
		t.Fatalf("读回成员角色失败: %v", err)
	}
	if stillMember != "member" {
		t.Errorf("重复写入后角色变成 %q，应保持不变（member）", stillMember)
	}
}
