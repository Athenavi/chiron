package api

import (
	"context"
	"errors"
	"sort"
	"testing"

	"github.com/jackc/pgx/v5"

	"github.com/athenavi/chiron/internal/auth"
)

// fakePermRow 是一个最小的 pgx.Row：只支持 Scan 到 *[]string。
type fakePermRow struct {
	perms []string
	err   error
}

func (r *fakePermRow) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	if len(dest) != 1 {
		return errors.New("fakePermRow: want exactly 1 dest")
	}
	p, ok := dest[0].(*[]string)
	if !ok {
		return errors.New("fakePermRow: dest must be *[]string")
	}
	*p = r.perms
	return nil
}

type fakePermQuerier struct {
	row     pgx.Row
	lastSQL string
	lastArg any
	calls   int
}

func (q *fakePermQuerier) QueryRow(_ context.Context, sql string, args ...interface{}) pgx.Row {
	q.calls++
	q.lastSQL = sql
	if len(args) > 0 {
		q.lastArg = args[0]
	}
	return q.row
}

func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	x := append([]string(nil), a...)
	y := append([]string(nil), b...)
	sort.Strings(x)
	sort.Strings(y)
	for i := range x {
		if x[i] != y[i] {
			return false
		}
	}
	return true
}

// tenant_members 有记录时必须以它为准 —— 否则团队角色就是摆设。
func TestResolveUserPerms_PrefersTenantMembers(t *testing.T) {
	want := []string{auth.PermChatRead, auth.PermAdminRead}
	q := &fakePermQuerier{row: &fakePermRow{perms: want}}

	got := resolveUserPerms(context.Background(), q, "u1", "user")

	if !sameSet(got, want) {
		t.Errorf("perms = %v, want %v", got, want)
	}
	if q.lastArg != "u1" {
		t.Errorf("query arg = %v, want u1", q.lastArg)
	}
}

// 查不到成员记录（ErrNoRows）→ 回退旧表，这是 C1-4 的可回退性所在。
func TestResolveUserPerms_FallsBackOnNoRows(t *testing.T) {
	q := &fakePermQuerier{row: &fakePermRow{err: pgx.ErrNoRows}}

	got := resolveUserPerms(context.Background(), q, "u1", "admin")

	if !sameSet(got, auth.RolePermissions["admin"]) {
		t.Errorf("perms = %v, want admin defaults %v", got, auth.RolePermissions["admin"])
	}
}

// 查询失败也回退，**不**拒绝登录：一次 DB 抖动不该把所有人锁在门外。
func TestResolveUserPerms_FallsBackOnDBError(t *testing.T) {
	q := &fakePermQuerier{row: &fakePermRow{err: errors.New("connection refused")}}

	got := resolveUserPerms(context.Background(), q, "u1", "owner")

	if !sameSet(got, auth.RolePermissions["owner"]) {
		t.Errorf("perms = %v, want owner defaults", got)
	}
}

// 空 userID 不查库，直接回退（例如调用点拿不到用户 ID 时）。
func TestResolveUserPerms_EmptyUserIDSkipsQuery(t *testing.T) {
	q := &fakePermQuerier{row: &fakePermRow{perms: []string{"should-not-be-used"}}}

	got := resolveUserPerms(context.Background(), q, "", "user")

	if q.calls != 0 {
		t.Errorf("empty userID must not query, got %d calls", q.calls)
	}
	if !sameSet(got, auth.RolePermissions["user"]) {
		t.Errorf("perms = %v, want user defaults", got)
	}
}

// 返回的权限**绝不能是 nil**：auth.HasPermission 用 `Perms != nil` 区分
// "显式授予（含空集）" 与 "未设置（回退 RolePermissions）"。返回 nil 会让
// 回退逻辑意外生效，把权限放大 —— 这是最危险的一种"默认值"。
func TestResolveUserPerms_NeverReturnsNil(t *testing.T) {
	cases := []struct {
		name string
		q    *fakePermQuerier
		role string
	}{
		{"tenant_members 返回 NULL 数组", &fakePermQuerier{row: &fakePermRow{perms: nil}}, "user"},
		{"未知角色回退", &fakePermQuerier{row: &fakePermRow{err: pgx.ErrNoRows}}, "no-such-role"},
		{"空角色无记录", &fakePermQuerier{row: &fakePermRow{err: pgx.ErrNoRows}}, ""},
	}
	for _, c := range cases {
		got := resolveUserPerms(context.Background(), c.q, "u1", c.role)
		if got == nil {
			t.Errorf("%s: perms 返回 nil，会让 HasPermission 回退并放大权限", c.name)
		}
	}
}

// 未知角色必须是"确定无权限"，而不是靠隐式回退。
func TestFallbackPerms_UnknownRoleIsEmptyNotNil(t *testing.T) {
	got := fallbackPerms("no-such-role")
	if got == nil {
		t.Fatal("fallbackPerms 对未知角色返回 nil，会导致权限放大")
	}
	if len(got) != 0 {
		t.Errorf("未知角色应为空权限，got %v", got)
	}
}

// 回退结果必须是副本：调用方改动不能污染 auth.RolePermissions 这个全局表。
func TestFallbackPerms_ReturnsCopy(t *testing.T) {
	got := fallbackPerms("user")
	if len(got) == 0 {
		t.Fatal("user 角色应有默认权限")
	}
	orig := auth.RolePermissions["user"][0]
	got[0] = "tampered"
	if auth.RolePermissions["user"][0] != orig {
		t.Errorf("fallbackPerms 泄漏了全局 map 的底层数组：%v", auth.RolePermissions["user"])
	}
}

// 权限查询语句必须按 user_id 收窄，且只读 tenant_members → ent_roles 这一条链。
func TestUserTenantPermsQuery_Shape(t *testing.T) {
	for _, want := range []string{"tenant_members", "ent_roles", "tm.user_id = $1", "permissions"} {
		if !contains(userTenantPermsQuery, want) {
			t.Errorf("查询缺少 %q:\n%s", want, userTenantPermsQuery)
		}
	}
	// 不能混入旧 RBAC 的两张表，否则"团队角色"这条链无法单独验证。
	for _, unwanted := range []string{"ent_user_roles", "ent_group_members"} {
		if contains(userTenantPermsQuery, unwanted) {
			t.Errorf("查询不应混入旧 RBAC 表 %q:\n%s", unwanted, userTenantPermsQuery)
		}
	}
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && indexOf(haystack, needle) >= 0
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}
