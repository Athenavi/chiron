package api

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// readMigrationFile 读取 migrations/versions 下的迁移源码。
// go test 的工作目录是包目录（internal/api），所以是两级上跳。
func readMigrationFile(name string) (string, error) {
	b, err := os.ReadFile(filepath.Join("..", "..", "migrations", "versions", name))
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// ── users.role → 租户内角色名的映射（三档，不是两档）──
//
// 这组断言防的是一次**静默降权**：迁移 0008 回填与 addTenantMember 若只写
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

// 迁移回填的三档映射必须与 tenantRoleNameFor 完全一致，否则「迁移来的老用户」
// 和「新注册的用户」会拿到不同角色 —— 这种不一致只在有存量数据时才暴露。
func TestMigration0008_BackfillMatchesRuntimeMapping(t *testing.T) {
	src, err := readMigrationFile("0008_tenant_members.py")
	if err != nil {
		t.Fatalf("读迁移文件失败: %v", err)
	}
	// 三个档位必须在回填语句里出现。
	for _, want := range []string{
		"WHEN u.role = 'owner' THEN 'owner'",
		"WHEN u.role = 'admin' THEN 'admin'",
		"ELSE 'member'",
	} {
		if !strings.Contains(src, want) {
			t.Errorf("迁移 0008 回填缺少三档映射片段 %q（与 tenantRoleNameFor 不一致）", want)
		}
	}
	// 旧的两档写法必须已经消失。
	if strings.Contains(src, "CASE WHEN u.role = 'owner' THEN 'owner' ELSE 'member' END") {
		t.Error("迁移 0008 仍是两档映射，admin 会被静默降成 member")
	}
}
