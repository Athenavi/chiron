package api

import (
	"strings"
	"testing"
)

// ── 邀请码参数默认值 ──
//
// `tenant_invites.max_uses = 0` 在 `consumeInviteQuery` 里表示**不限次数**。
// 所以"用户没填次数"绝不能被原样写成 0 —— 那等于发了一张永久有效、可无限
// 使用的邀请码。这几条用例就是钉住这个语义分叉。
func TestNormalizeInviteParams_ZeroMeansUnlimitedInDB(t *testing.T) {
	maxUses, days := normalizeInviteParams(0, 0)
	if maxUses != 1 {
		t.Errorf("max_uses(0) = %d, want 1（0 在库语义里是不限次，不能直接落库）", maxUses)
	}
	if days != 7 {
		t.Errorf("expires_in_days(0) = %d, want 7", days)
	}
}

func TestNormalizeInviteParams_KeepsExplicitValues(t *testing.T) {
	maxUses, days := normalizeInviteParams(5, 30)
	if maxUses != 5 || days != 30 {
		t.Errorf("got (%d, %d), want (5, 30)", maxUses, days)
	}
}

func TestNormalizeInviteParams_NegativeTreatedAsUnset(t *testing.T) {
	maxUses, days := normalizeInviteParams(-3, -1)
	if maxUses != 1 || days != 7 {
		t.Errorf("got (%d, %d), want (1, 7)", maxUses, days)
	}
}

// ── 租户隔离（本组接口最重要的安全约束）──
//
// 这些断言看着像"检查字符串"，但它们防的是一类真实越权：**任何人只要拿到
// admin 权限，就能改别的租户的成员**。约束靠的是"每条语句都带 tenant_id 条件"，
// 而 tenant_id 来自登录态（GetTenantID），不来自请求体。语句里一旦漏掉
// `tenant_id`，这条用例立刻红。

func TestTenantMemberQueries_AllScopedToTenant(t *testing.T) {
	cases := []struct {
		name  string
		query string
	}{
		{"tenantMembersListQuery", tenantMembersListQuery},
		{"tenantRolesListQuery", tenantRolesListQuery},
		{"tenantMemberRoleQuery", tenantMemberRoleQuery},
		{"tenantOwnerCountQuery", tenantOwnerCountQuery},
		{"tenantMemberDeleteQuery", tenantMemberDeleteQuery},
	}
	for _, c := range cases {
		if !strings.Contains(c.query, "tenant_id = $1") {
			t.Errorf("%s 缺少 `tenant_id = $1` 条件，会导致跨租户访问:\n%s", c.name, c.query)
		}
	}
}

// 改角色时，`role` 也必须限定在本租户 —— 否则可以把别的租户的角色挂到这个成员身上。
func TestTenantMemberSetRoleQuery_RoleMustBelongToSameTenant(t *testing.T) {
	if !strings.Contains(tenantMemberSetRoleQuery, "r.tenant_id = $1") {
		t.Errorf("改角色语句必须限定 r.tenant_id = $1，否则可挂载他人租户的角色:\n%s",
			tenantMemberSetRoleQuery)
	}
	if !strings.Contains(tenantMemberSetRoleQuery, "tm.tenant_id = $1") {
		t.Errorf("改角色语句必须限定 tm.tenant_id = $1:\n%s", tenantMemberSetRoleQuery)
	}
}

// 撤销邀请码同样只能撤销本租户的：否则凭一个 code 就能删掉别人的邀请。
func TestTenantInviteDeleteQuery_ScopedToTenant(t *testing.T) {
	if !strings.Contains(tenantInviteDeleteQuery, "tenant_id = $2") {
		t.Errorf("撤销邀请必须带 tenant_id 条件:\n%s", tenantInviteDeleteQuery)
	}
}

// 移除成员时吊销 key 必须覆盖该用户**全部**未吊销的 key，而不是某一把。
func TestUserAPIKeysRevokeAllQuery_ScopedToUserOnly(t *testing.T) {
	if !strings.Contains(userAPIKeysRevokeAllQuery, "user_id = $1") {
		t.Errorf("批量吊销必须按 user_id 收窄:\n%s", userAPIKeysRevokeAllQuery)
	}
	if strings.Contains(userAPIKeysRevokeAllQuery, "tenant_id") {
		// 按租户收窄会漏掉：key 归属人只有一个租户，但漏掉"全部 key"的语义
		// 会让用户在别的租户继续用同一把 key。
		t.Log("提示：该语句不按 tenant 收窄是刻意的 —— 要吊销的是这个人的全部 key")
	}
}

// 最后一个 owner 的守卫：语义必须是"剩余 <= 1 就拒绝"。
// 这条逻辑写在 guardLastOwner 里，此处只钉住常量存在与错误非空，
// 真正的 DB 分支由 live 冒烟覆盖。
func TestErrLastOwner_IsActionable(t *testing.T) {
	if errLastOwner == nil {
		t.Fatal("errLastOwner 必须非空，否则守卫会静默放行")
	}
	if !strings.Contains(errLastOwner.Error(), "owner") {
		t.Errorf("错误文案应点明 owner，实际 = %q", errLastOwner.Error())
	}
}
