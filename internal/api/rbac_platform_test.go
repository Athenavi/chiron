package api

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// ── 平台级端点门禁（P0 越权回归）──
//
// 背景：`admin:write` 是**租户内置 admin 角色**就持有的权限
// （auth.RolePermissions["admin"]）。于是像
//
//	POST /v1/admin/database/query   SELECT * FROM users   → 拿到全平台 password_hash
//	DELETE /v1/admin/tenants/1                             → 删掉别的租户
//
// 这类本质上是平台级的操作，只要租户 2 的 admin 一登录就能执行。下面这几条用例钉住
// "租户 admin 不得进入平台级端点"，以及"平台 owner 仍然进得去"（不要修成谁都进不去）。
func TestRequirePlatformOwner_RejectsTenantAdmin(t *testing.T) {
	reached := false
	h := RequirePlatformOwner(auth.PermAdminWrite)(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		reached = true
	}))

	claims := &auth.Claims{UserID: "u-2", Role: "admin", TenantID: 2}
	req := httptest.NewRequest(http.MethodPost, "/v1/admin/database/query", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), claims))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if reached {
		t.Fatal("租户 admin 进入了平台级端点：任意租户 admin 可跨租户读库/删租户")
	}
	if rec.Code != http.StatusForbidden {
		t.Errorf("应返回 403，得到 %d", rec.Code)
	}
}

func TestRequirePlatformOwner_AllowsPlatformOwner(t *testing.T) {
	reached := false
	h := RequirePlatformOwner(auth.PermAdminWrite)(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		reached = true
	}))

	claims := &auth.Claims{UserID: "u-1", Role: "owner", TenantID: 1}
	req := httptest.NewRequest(http.MethodPost, "/v1/admin/database/query", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), claims))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if !reached {
		t.Fatalf("平台 owner 被拦了（%d）：修复不能把平台运维一起挡在外面", rec.Code)
	}
}

// 权限点仍然要生效：owner 角色但 claims 显式收窄到空权限（API key 路径就是这样做的），
// 不应因为角色名是 owner 就放开。
func TestRequirePlatformOwner_StillHonoursPermissionSet(t *testing.T) {
	reached := false
	h := RequirePlatformOwner(auth.PermAdminWrite)(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		reached = true
	}))

	claims := &auth.Claims{UserID: "u-1", Role: "owner", TenantID: 1, Perms: []string{}}
	req := httptest.NewRequest(http.MethodPost, "/v1/admin/database/query", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), claims))
	h.ServeHTTP(httptest.NewRecorder(), req)

	if reached {
		t.Fatal("显式空权限的 claims 绕过了平台门禁")
	}
}

// ── 租户上下文注入 ──
//
// `GetTenantID` 只读 CtxKeyTenantID，而这个键**只能**由鉴权链路写入。
// 历史缺陷：TenantMiddleware（唯二的写入点之一）全仓没有调用点，authMW 也只注入
// claims 不注入租户键 ⇒ 所有租户级 handler（成员管理 / 用户管理 / 域名 / 邀请码）
// 拿到的 tenantID 恒为 0，全部返回 401。
func TestWithTenantCtx_InjectsTenantAndUser(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/v1/admin/tenant/members", nil)
	claims := &auth.Claims{UserID: "u-7", Role: "admin", TenantID: 7}
	req = req.WithContext(withTenantCtx(auth.WithClaims(req.Context(), claims), claims))

	if got := GetTenantID(req); got != 7 {
		t.Errorf("GetTenantID = %d, want 7（租户级 handler 会恒 401）", got)
	}
	if got := GetUserID(req); got != "u-7" {
		t.Errorf("GetUserID = %q, want u-7", got)
	}
}

// 未绑定租户的 claims 不应被伪造成默认租户：调用方用 `GetTenantID(r) == 0` 表达 401，
// 静默回填 DefaultTenantID 会让一个租户未绑定的身份读到默认租户的数据。
func TestWithTenantCtx_DoesNotInventATenant(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/v1/admin/tenant/members", nil)
	claims := &auth.Claims{UserID: "u-9", Role: "admin"}
	req = req.WithContext(withTenantCtx(auth.WithClaims(req.Context(), claims), claims))

	if got := GetTenantID(req); got != 0 {
		t.Errorf("GetTenantID = %d, want 0（不得回填 DefaultTenantID）", got)
	}
}
