package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// ── 注销闸门 ──
//
// 产品规则：「注销后不允许其他操作，只允许下载或导出，且可撤销」。
// 这条规则必须由服务端强制，前端隐藏按钮绕过去照样能调接口。
//
// 这里钉住两件事：
//  1. 白名单是**精确**的（只放行导出/状态/撤销 + 只读 /media），
//     任何新端点默认被拦 —— 白名单而非黑名单，漏管方向安全；
//  2. 无身份请求不在这里判定（交给下游鉴权），避免闸门成为鉴权的一部分。

func TestDeletionGateAllowedPathsAreMinimal(t *testing.T) {
	allowed := []struct{ method, path string }{
		{http.MethodGet, "/v1/me/export"},
		{http.MethodGet, "/v1/me/deletion"},
		{http.MethodPost, "/v1/me/deletion/cancel"},
		{http.MethodGet, "/media/s/abc123"},
		{http.MethodHead, "/media/s/abc123"},
	}
	for _, tc := range allowed {
		r := httptest.NewRequest(tc.method, tc.path, nil)
		if !isDeletionAllowedRequest(r) {
			t.Errorf("%s %s 应放行（导出/撤销/只读媒体是冻结期唯一允许的操作）", tc.method, tc.path)
		}
	}

	blocked := []struct{ method, path string }{
		{http.MethodPost, "/v1/chat"},                 // 核心交互：必须拦
		{http.MethodGet, "/v1/conversations"},         // 浏览历史：不是"导出"
		{http.MethodPost, "/v1/uploads"},              // 不许再产生新数据
		{http.MethodPost, "/media/s/abc123"},          // 写方法不放过
		{http.MethodPost, "/v1/me/deletion"},          // 已申请再申请没有意义
		{http.MethodPut, "/v1/auth/profile"},          // 改资料：不是导出
		{http.MethodGet, "/v1/me/deletion/cancel"},    // 撤销只接受 POST
		{http.MethodDelete, "/v1/me/export"},          // 方法不匹配
	}
	for _, tc := range blocked {
		r := httptest.NewRequest(tc.method, tc.path, nil)
		if isDeletionAllowedRequest(r) {
			t.Errorf("%s %s 不应放行（冻结期只允许导出与撤销）", tc.method, tc.path)
		}
	}
}

// 无身份请求：闸门不判定（交给下游鉴权）。否则未登录用户会收到
// "账号待删除"的 403，误导性极强。
func TestDeletionGatePassesThroughWithoutClaims(t *testing.T) {
	called := false
	gate := AccountDeletionGate(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	gate.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/v1/chat", nil))
	if !called {
		t.Fatal("无身份请求应透传给下游鉴权，而不是被闸门拦下")
	}
}

// 正常账号（active）不受影响 —— 闸门不能误伤绝大多数用户。
func TestDeletionGateAllowsActiveAccount(t *testing.T) {
	withP12Pool(t)
	_, userID := liveSmokeFixture(t, withP12Pool(t))
	ctx := context.Background()

	// 确保缓存里没有陈旧条目影响判定。
	invalidateAccountStatusCache(userID)

	called := false
	gate := AccountDeletionGate(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	req := httptest.NewRequest(http.MethodPost, "/v1/chat", nil)
	req = req.WithContext(auth.WithClaims(ctx, &auth.Claims{UserID: userID}))
	gate.ServeHTTP(httptest.NewRecorder(), req)

	if !called {
		t.Fatal("active 账号被闸门拦下了：会误伤全部正常用户")
	}
}

// 冻结中的账号：非白名单一律 403，且错误码明确（前端据此引导去导出/撤销）。
func TestDeletionGateBlocksPendingAccount(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	ctx := context.Background()

	if _, err := pool.Exec(ctx,
		`UPDATE users SET account_status = 'pending_deletion' WHERE id = $1`, userID); err != nil {
		t.Fatalf("设置状态失败: %v", err)
	}
	invalidateAccountStatusCache(userID)

	gate := AccountDeletionGate(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("冻结中的账号不该被放行到这里")
	}))
	req := httptest.NewRequest(http.MethodPost, "/v1/chat", nil)
	req = req.WithContext(auth.WithClaims(ctx, &auth.Claims{UserID: userID}))
	rec := httptest.NewRecorder()
	gate.ServeHTTP(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("应返回 403，得到 %d body=%s", rec.Code, rec.Body.String())
	}
	// 403 而非 401：401 会让前端踢回登录页，用户就再也看不到导出/撤销入口了。
	if body := rec.Body.String(); !strings.Contains(body, "account_pending_deletion") {
		t.Fatalf("响应缺少稳定错误码，前端无法据此引导用户：%s", body)
	}

	// 同一账号的导出端点必须仍然放行。
	exportGate := AccountDeletionGate(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	expReq := httptest.NewRequest(http.MethodGet, "/v1/me/export", nil)
	expReq = expReq.WithContext(auth.WithClaims(ctx, &auth.Claims{UserID: userID}))
	expRec := httptest.NewRecorder()
	exportGate.ServeHTTP(expRec, expReq)
	if expRec.Code != http.StatusOK {
		t.Fatalf("冻结期必须允许导出，得到 %d", expRec.Code)
	}
}
