package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// K2：租户功能开关端点（SSO 灰度载体）。
//
// 两条关键语义：
//  1. **写仅平台 owner** —— 租户 admin 即便持有 `sso:manage` 也不能自行放开 SSO
//     （"该租户能否用 SSO"是平台侧的信任边界）；
//  2. **无记录 = 全关** —— 灰度默认，绝不因"没配"而自动开放。

const featTenantID = "1"

func newFeaturesRequest(method, body string, claims *auth.Claims) *http.Request {
	var r *http.Request
	if body == "" {
		r = httptest.NewRequest(method, "/v1/ent/tenants/"+featTenantID+"/features", nil)
	} else {
		r = httptest.NewRequest(
			method, "/v1/ent/tenants/"+featTenantID+"/features", strings.NewReader(body),
		)
	}
	r.SetPathValue("id", featTenantID)
	if claims != nil {
		r = r.WithContext(auth.WithClaims(r.Context(), claims))
	}
	return r
}

func TestPutTenantFeaturesRequiresPlatformOwner(t *testing.T) {
	h := &EntIdentityHandler{db: &recordingEntStore{}}

	cases := []struct {
		name   string
		claims *auth.Claims
	}{
		{"无 claims", nil},
		{"租户 admin（有 sso:manage 也不行）", &auth.Claims{UserID: "u1", Role: "admin", TenantID: 1}},
		{"普通用户", &auth.Claims{UserID: "u2", Role: "user", TenantID: 1}},
	}
	for _, c := range cases {
		rec := httptest.NewRecorder()
		h.PutTenantFeatures(rec, newFeaturesRequest(http.MethodPut, `{"sso_enabled":true}`, c.claims))
		if rec.Code != http.StatusForbidden {
			t.Errorf("%s：应 403，得到 %d", c.name, rec.Code)
		}
	}
}

func TestGetTenantFeaturesDefaultsToAllOff(t *testing.T) {
	h := &EntIdentityHandler{db: &recordingEntStore{}} // 无记录 ⇒ 全关
	rec := httptest.NewRecorder()
	h.GetTenantFeatures(rec, newFeaturesRequest(http.MethodGet, "", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("应 200，得到 %d", rec.Code)
	}
	if body := rec.Body.String(); !strings.Contains(body, "sso_enabled") {
		t.Errorf("响应应含 sso_enabled 字段，得到 %s", body)
	}
}

// 路由暴露：`/v1/ent/tenants/{id}/features` 必须真的挂在 mux 上
// （这个仓库栽过多次"handler 写好了但没接线 ⇒ 前端恒 404"）。
func TestEntTenantFeaturesRoutesAreExposed(t *testing.T) {
	mux := http.NewServeMux()
	probe := &middlewareProbe{}
	NewEntIdentityHandler().RegisterRoutes(mux, probe.reject(http.StatusUnauthorized))

	for _, c := range []struct{ method, path string }{
		{http.MethodGet, "/v1/ent/tenants/" + featTenantID + "/features"},
		{http.MethodPut, "/v1/ent/tenants/" + featTenantID + "/features"},
	} {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(c.method, c.path, nil))
		if !probe.hit(c.path) {
			t.Errorf("%s %s 没有匹配到任何注册模式 ⇒ 前端会拿到 404", c.method, c.path)
		}
	}
}
