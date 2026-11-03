package api

import "testing"

// ── users.role → 租户内角色名的映射（三档，不是两档）──
//
// 这组断言防的是一次**静默降权**：addTenantMember 若只写
// 「owner → owner，其余一律 member」，全站 admin（admin.go:242 明确允许这个
// 取值）会被降成普通成员。而 C1-4 之后权限判定优先读 tenant_members，
// 所以降权是真实生效的 —— 存量管理员丢掉 admin:* / audit:read，无任何提示。

func TestTenantRoleNameFor_OwnerAndAdminPreserved(t *testing.T) {
	cases := map[string]string{
		"owner": "owner",
		"admin": "admin", // ← 少了这一档就是静默降权
		"user":  "member",
	}
	for sysRole, want := range cases {
		if got := tenantRoleNameFor(sysRole); got != want {
			t.Errorf("tenantRoleNameFor(%q) = %q, want %q", sysRole, got, want)
		}
	}
}

// 未知/空角色落到 member 而不是 owner：宁可少给权限，也不能凭空提权。
func TestTenantRoleNameFor_UnknownRoleNeverElevates(t *testing.T) {
	for _, sysRole := range []string{"", "no-such-role", "ADMIN", "Owner"} {
		got := tenantRoleNameFor(sysRole)
		if got != "member" {
			t.Errorf("tenantRoleNameFor(%q) = %q, want member（未知角色不得提权）", sysRole, got)
		}
	}
}
