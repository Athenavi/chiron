package engine

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// ForwardRequest 的身份头契约：**一律以网关已验证的 JWT claims 为准**。
//
// 回归背景：skipHeaders 会剥掉客户端伪造的 X-User-Role（防止自报角色），
// 但注入块只补了 X-User-Id / X-Tenant-Id，**漏了 X-User-Role**。后果是静默失效：
// 调用方（routes_admin.go 的 GET /v1/admin/kb）先 Set("X-User-Role", claims.Role)
// 再转发，而转发层把它当伪造头删掉 ⇒ 引擎 `app/api/knowledge.py` 的
// `/admin/list` 与删除知识库的 is_admin 判定**永远为 false**。
//
// 这条漏项不会报错、不会 500，只表现为「管理员看不到知识库列表 / 删不掉」。
func TestForwardRequest_InjectsRoleFromClaims(t *testing.T) {
	var (
		gotRole     string
		gotUserID   string
		gotTenantID string
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotRole = r.Header.Get("X-User-Role")
		gotUserID = r.Header.Get("X-User-Id")
		gotTenantID = r.Header.Get("X-Tenant-Id")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	client := NewPythonClient(srv.URL)
	req := httptest.NewRequest(http.MethodGet, "/v1/admin/kb", nil)
	ctx := auth.WithClaims(req.Context(), &auth.Claims{
		UserID:   "u-1",
		Role:     "owner",
		TenantID: 7,
	})
	rec := httptest.NewRecorder()

	client.ForwardRequest(rec, req.WithContext(ctx), "/v1/admin/kb")

	if gotRole != "owner" {
		t.Fatalf("引擎收到的 X-User-Role = %q, want %q —— 漏注入会让引擎侧管理员判定永远为假",
			gotRole, "owner")
	}
	if gotUserID != "u-1" {
		t.Fatalf("X-User-Id = %q, want %q", gotUserID, "u-1")
	}
	if gotTenantID != "7" {
		t.Fatalf("X-Tenant-Id = %q, want %q", gotTenantID, "7")
	}
}

// 客户端伪造的身份头必须被覆盖，绝不能让调用方（或攻击者）自报角色。
func TestForwardRequest_ClientSuppliedRoleIsOverridden(t *testing.T) {
	var gotRole, gotUserID string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotRole = r.Header.Get("X-User-Role")
		gotUserID = r.Header.Get("X-User-Id")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	client := NewPythonClient(srv.URL)
	req := httptest.NewRequest(http.MethodGet, "/v1/kb", nil)
	// 伪造：自称 owner，实际身份是普通 user
	req.Header.Set("X-User-Role", "owner")
	req.Header.Set("X-User-Id", "victim")
	ctx := auth.WithClaims(req.Context(), &auth.Claims{UserID: "u-2", Role: "user"})

	client.ForwardRequest(httptest.NewRecorder(), req.WithContext(ctx), "/v1/kb")

	if gotRole != "user" {
		t.Fatalf("伪造的 X-User-Role=owner 未被覆盖，引擎收到 %q", gotRole)
	}
	if gotUserID != "u-2" {
		t.Fatalf("伪造的 X-User-Id 未被覆盖，引擎收到 %q", gotUserID)
	}
}

// 无 claims（未认证路径）时不得凭空造出身份头 —— 否则引擎会把匿名请求当管理员。
func TestForwardRequest_NoClaimsInjectsNoRole(t *testing.T) {
	var (
		present bool
		value   string
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		value = r.Header.Get("X-User-Role")
		_, present = r.Header["X-User-Role"]
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	client := NewPythonClient(srv.URL)
	req := httptest.NewRequest(http.MethodGet, "/v1/kb", nil)

	client.ForwardRequest(httptest.NewRecorder(), req, "/v1/kb")

	if present || value != "" {
		t.Fatalf("无 claims 时不应注入 X-User-Role，实际 present=%v value=%q", present, value)
	}
}

// 已认证但 Role 为空的账号，同样不该发出空角色头。
func TestForwardRequest_EmptyRoleIsNotSent(t *testing.T) {
	var present bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, present = r.Header["X-User-Role"]
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	client := NewPythonClient(srv.URL)
	req := httptest.NewRequest(http.MethodGet, "/v1/kb", nil)
	ctx := auth.WithClaims(req.Context(), &auth.Claims{UserID: "u-3", Role: ""})

	client.ForwardRequest(httptest.NewRecorder(), req.WithContext(ctx), "/v1/kb")

	if present {
		t.Fatal("claims.Role 为空时不应发出 X-User-Role 头")
	}
}
