package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

func TestWithQuery(t *testing.T) {
	cases := []struct{ path, query, want string }{
		{"/v1/skills", "", "/v1/skills"},
		{"/v1/skills", "a=1", "/v1/skills?a=1"},
		{"/v1/skills?b=2", "a=1", "/v1/skills?b=2&a=1"},
	}
	for _, c := range cases {
		if got := withQuery(c.path, c.query); got != c.want {
			t.Errorf("withQuery(%q, %q) = %q, want %q", c.path, c.query, got, c.want)
		}
	}
}

func TestIdentityParamsCarriesClaimsIdentity(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/v1/skills", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{
		UserID:   "u1",
		TenantID: "t1",
	}))

	q := identityParams(req)

	if got := q.Get("user_id"); got != "u1" {
		t.Errorf("user_id = %q, want %q", got, "u1")
	}
	if got := q.Get("tenant_id"); got != "t1" {
		t.Errorf("tenant_id = %q, want %q", got, "t1")
	}
}

// 回归：GET / DELETE 无 body，身份必须经 query 下发。
//
// 历史缺陷是**根本没有这段注入** —— 引擎收到空 user_id/tenant_id 后一律回退全局共享层，
// 用户看不到自己的私有技能与租户共享技能。
func TestIdentityParamsWithoutClaimsOmitsUserID(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/v1/skills", nil)

	q := identityParams(req)

	if got := q.Get("user_id"); got != "" {
		t.Errorf("expected empty user_id without claims, got %q", got)
	}
}

// 身份段里出现穿越片段时，编码结果不应含裸的 `../`（引擎会 400，网关不该原样发出）。
func TestIdentityParamsEscapesSegments(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/v1/skills", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{
		UserID:   "../etc",
		TenantID: "t1",
	}))

	encoded := identityParams(req).Encode()

	if encoded == "" {
		t.Fatal("expected non-empty query")
	}
	if strings.Contains(encoded, "../") {
		t.Errorf("encoded query leaks raw path traversal: %q", encoded)
	}
}
