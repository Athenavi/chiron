package api

// interactions_test.go —— M7c 介入端点的网关半边。
//
// 锁三组事情，全部是"网关 uniquely 负责"的那三件：
//   1. 身份**只来自已验证 claims**：客户端在 query / body 里自报的 user_id、tenant_id
//      必须被丢弃（否则"知道别人的 interaction_id"就够答别人的票）；
//   2. 引擎信封（200 + ok/error_code）→ 面向用户的状态码映射；
//   3. 路径参数转义：interaction_id 是外部输入，不能让它把代理路径撑出另一段。

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

// newInteractionTestHandlers 起一个假引擎：把收到的 method/path/query/body 原样记进 seen，
// 并按 canned 回应（canned 为空则回一个通用成功信封）。
func newInteractionTestHandlers(t *testing.T, seen map[string]any, canned string) (*InteractionHandler, func()) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		seen["path"] = r.URL.Path
		seen["method"] = r.Method
		seen["query"] = r.URL.RawQuery
		var body map[string]any
		if len(raw) > 0 {
			_ = json.Unmarshal(raw, &body)
		}
		seen["body"] = body
		w.Header().Set("Content-Type", "application/json")
		if canned == "" {
			canned = `{"ok":true,"count":0,"items":[]}`
		}
		_, _ = w.Write([]byte(canned))
	}))
	return NewInteractionHandler(engine.NewPythonClient(srv.URL), nil), srv.Close
}

func decodeAPI(t *testing.T, rec *httptest.ResponseRecorder) APIResponse {
	t.Helper()
	var out APIResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应不是 APIResponse: %v (%s)", err, rec.Body.String())
	}
	return out
}

func TestInteractionListForwardsClaimIdentity(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInteractionTestHandlers(t, seen, `{"ok":true,"count":1,"items":[{"id":"i1","kind":"approval"}]}`)
	defer closeSrv()

	// 客户端伪造一个别人的 user_id/tenant_id：只允许 claims 的那份被转发。
	req := httptest.NewRequest(http.MethodGet,
		"/v1/agent/sessions/s1/interactions?user_id=attacker&tenant_id=t-victim", nil)
	req.SetPathValue("session_id", "s1")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u-owner", TenantID: "t-own"}))
	rec := httptest.NewRecorder()

	h.List(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	if seen["path"] != "/v1/agent/sessions/s1/interactions" {
		t.Fatalf("转发路径不对: %v", seen["path"])
	}
	q, _ := seen["query"].(string)
	if !strings.Contains(q, "user_id=u-owner") || !strings.Contains(q, "tenant_id=t-own") {
		t.Fatalf("未按 claims 注入身份: %s", q)
	}
	if strings.Contains(q, "attacker") || strings.Contains(q, "t-victim") {
		t.Fatalf("客户端自报身份被转发出去了: %s", q)
	}
	out := decodeAPI(t, rec)
	if !out.Success {
		t.Fatalf("应成功: %+v", out)
	}
	data, _ := out.Data.(map[string]any)
	if data["count"] != float64(1) || data["session_id"] != "s1" {
		t.Fatalf("data 形状不对: %+v", data)
	}
	items, _ := data["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("items 未透传: %+v", data)
	}
}

func TestInteractionListRequiresAuthenticatedClaims(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInteractionTestHandlers(t, seen, "")
	defer closeSrv()

	req := httptest.NewRequest(http.MethodGet, "/v1/agent/sessions/s1/interactions", nil)
	req.SetPathValue("session_id", "s1")
	rec := httptest.NewRecorder()
	h.List(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("无 claims 应 401，得到 %d", rec.Code)
	}
	if _, ok := seen["path"]; ok {
		t.Fatal("未认证就不该打引擎")
	}
}

// 缺租户同样拒绝：引擎每条谓词都带 tenant，缺它只能靠 user 单条件匹配（口径与 pythonProxy 一致）。
func TestInteractionListRequiresTenantClaim(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInteractionTestHandlers(t, seen, "")
	defer closeSrv()

	req := httptest.NewRequest(http.MethodGet, "/v1/agent/sessions/s1/interactions", nil)
	req.SetPathValue("session_id", "s1")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1"}))
	rec := httptest.NewRecorder()
	h.List(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("缺租户应 401，得到 %d", rec.Code)
	}
}

func TestInteractionListClampsLimit(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"", "limit=20"},
		{"?limit=abc", "limit=20"},
		{"?limit=0", "limit=20"},
		{"?limit=99999", "limit=50"}, // 有界：与引擎 MAX_LIST_LIMIT 同值
		{"?limit=5", "limit=5"},
	}
	for _, tc := range cases {
		seen := map[string]any{}
		h, closeSrv := newInteractionTestHandlers(t, seen, "")
		req := httptest.NewRequest(http.MethodGet, "/v1/agent/sessions/s1/interactions"+tc.in, nil)
		req.SetPathValue("session_id", "s1")
		req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: "t1"}))
		rec := httptest.NewRecorder()
		h.List(rec, req)
		closeSrv()

		q, _ := seen["query"].(string)
		if !strings.Contains(q, tc.want) {
			t.Errorf("limit=%q 期望转发 %q，实际 %q", tc.in, tc.want, q)
		}
	}
}

func TestInteractionListMapsEngineFailure(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	h := NewInteractionHandler(engine.NewPythonClient(srv.URL), nil)

	req := httptest.NewRequest(http.MethodGet, "/v1/agent/sessions/s1/interactions", nil)
	req.SetPathValue("session_id", "s1")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: "t1"}))
	rec := httptest.NewRecorder()
	h.List(rec, req)

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("引擎不可达应 500，得到 %d body=%s", rec.Code, rec.Body.String())
	}
}

func TestInteractionResolveUsesClaimsIdentityNotBody(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInteractionTestHandlers(t, seen,
		`{"ok":true,"data":{"id":"i1","kind":"approval","session_id":"s1","status":"answered"}}`)
	defer closeSrv()

	body := `{"session_id":"s1","user_id":"attacker","tenant_id":"t-victim",` +
		`"response":{"decision":"edit","arguments":{"command":"ls -1"}}}`
	req := httptest.NewRequest(http.MethodPost, "/v1/agent/interactions/i1/resolve", strings.NewReader(body))
	req.SetPathValue("interaction_id", "i1")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u-owner", TenantID: "t-own"}))
	rec := httptest.NewRecorder()

	h.Resolve(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	fwd, _ := seen["body"].(map[string]any)
	if fwd["user_id"] != "u-owner" || fwd["tenant_id"] != "t-own" {
		t.Fatalf("身份未被 claims 覆盖: %+v", fwd)
	}
	if strings.Contains(seen["query"].(string), "user_id=") {
		t.Fatalf("resolve 不该走 query 身份（body 形态已足够）: %v", seen["query"])
	}
	// 答复载荷必须**原样**透传：编辑后的参数是"批准的就是执行的"的落点
	resp, _ := fwd["response"].(map[string]any)
	if resp["decision"] != "edit" {
		t.Fatalf("decision 丢失: %+v", resp)
	}
	args, _ := resp["arguments"].(map[string]any)
	if args["command"] != "ls -1" {
		t.Fatalf("编辑后的参数丢失: %+v", resp)
	}
	if fwd["session_id"] != "s1" {
		t.Fatalf("session_id 未透传: %+v", fwd)
	}
	out := decodeAPI(t, rec)
	data, _ := out.Data.(map[string]any)
	if data["status"] != "answered" {
		t.Fatalf("应把引擎 data 透出: %+v", out.Data)
	}
}

func TestInteractionResolveFieldValidation(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"缺 session_id", `{"response":{"decision":"approve"}}`},
		{"缺 response", `{"session_id":"s1"}`},
		{"response 空对象", `{"session_id":"s1","response":{}}`},
		{"非法 JSON", `not-json`},
	}
	for _, tc := range cases {
		seen := map[string]any{}
		h, closeSrv := newInteractionTestHandlers(t, seen, "")
		req := httptest.NewRequest(http.MethodPost, "/v1/agent/interactions/i1/resolve", strings.NewReader(tc.body))
		req.SetPathValue("interaction_id", "i1")
		req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: "t1"}))
		rec := httptest.NewRecorder()
		h.Resolve(rec, req)
		closeSrv()

		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s: 应 400，得到 %d body=%s", tc.name, rec.Code, rec.Body.String())
		}
		if _, ok := seen["path"]; ok {
			t.Errorf("%s: 参数不合法时不该打引擎", tc.name)
		}
	}
}

// 形状判定不在网关：网关不知道那一行的 kind（只有库知道），所以它只做非空校验，
// 由引擎回 invalid_decision。这里锁住映射，避免"引擎 200"被当成成功透出去。
func TestInteractionResolveMapsEngineErrorCodes(t *testing.T) {
	cases := []struct {
		canned     string
		wantStatus int
		wantCode   string
	}{
		{`{"ok":false,"error_code":"not_found","error":"interaction not found"}`, http.StatusNotFound, CodeNotFound},
		{`{"ok":false,"error_code":"stale","error":"already answered"}`, http.StatusConflict, CodeRequestFailed},
		{`{"ok":false,"error_code":"invalid_decision","error":"unknown decision"}`, http.StatusBadRequest, CodeInvalidRequest},
		{`{"ok":false,"error_code":"storage_error","error":"unknown interaction kind"}`, http.StatusInternalServerError, CodeInternal},
		{`{"ok":false,"error":"无 code"}`, http.StatusInternalServerError, CodeRequestFailed},
	}
	for _, tc := range cases {
		seen := map[string]any{}
		h, closeSrv := newInteractionTestHandlers(t, seen, tc.canned)
		req := httptest.NewRequest(http.MethodPost, "/v1/agent/interactions/i1/resolve",
			strings.NewReader(`{"session_id":"s1","response":{"decision":"approve"}}`))
		req.SetPathValue("interaction_id", "i1")
		req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: "t1"}))
		rec := httptest.NewRecorder()
		h.Resolve(rec, req)
		closeSrv()

		if rec.Code != tc.wantStatus {
			t.Errorf("%s: 状态码=%d 期望 %d", tc.canned, rec.Code, tc.wantStatus)
			continue
		}
		out := decodeAPI(t, rec)
		if out.Success {
			t.Errorf("%s: 业务失败不该 success=true", tc.canned)
		}
		if out.Code != tc.wantCode {
			t.Errorf("%s: code=%q 期望 %q", tc.canned, out.Code, tc.wantCode)
		}
	}
}

// interaction_id 是外部输入：带 "/" 的 id 不能把代理路径撑出另一段（否则会打到引擎的别的端点）。
func TestInteractionResolveEscapesPathParams(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInteractionTestHandlers(t, seen, `{"ok":true,"data":{}}`)
	defer closeSrv()

	nasty := "../../v1/agent/approval"
	req := httptest.NewRequest(http.MethodPost, "/v1/agent/interactions/x/resolve",
		strings.NewReader(`{"session_id":"s1","response":{"decision":"approve"}}`))
	req.SetPathValue("interaction_id", nasty)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: "t1"}))
	rec := httptest.NewRecorder()
	h.Resolve(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	if seen["path"] != "/v1/agent/interactions/"+nasty+"/resolve" {
		t.Fatalf("id 不该改变请求的路径结构，实际转发到 %v", seen["path"])
	}
	if strings.Contains(seen["query"].(string), "approval") {
		t.Fatalf("路径穿越进了 query: %v", seen["query"])
	}
}

// 判据与 gateway_router_probe_test.go 相同：authMW 用 reject(401) —— 只有**注册过**的模式
// 才会进入中间件链，所以"探针记到这条路径"等价于"路由已暴露"。
//
// 这条不是形式主义：介入端点是"能把别人的票改掉"的写入口，漏挂 authMW 就等于 claims 恒为
// nil → 网关无从注入身份 → 未登录者只要知道 interaction_id 就能自行答复。
func TestInteractionRoutesAreRegisteredBehindAuthMiddleware(t *testing.T) {
	mux := http.NewServeMux()
	authProbe := &middlewareProbe{}
	rlProbe := &middlewareProbe{}
	registerAgentRoutes(
		mux, authProbe.reject(http.StatusUnauthorized), rlProbe.passthrough,
		authProbe.passthrough, authProbe.passthrough,
		nil, nil, nil, nil, nil, nil, nil, nil,
		"pytest-internal-token", time.Second,
		engine.NewPythonClient("http://127.0.0.1:1"),
	)

	cases := []struct{ method, path string }{
		{http.MethodGet, "/v1/agent/sessions/s1/interactions"},
		{http.MethodPost, "/v1/agent/interactions/i1/resolve"},
	}
	for _, tc := range cases {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(tc.method, tc.path, strings.NewReader(`{}`)))
		if !authProbe.hit(tc.path) {
			t.Errorf("%s %s 未经过 authMW（未注册，或未挂认证）", tc.method, tc.path)
			continue
		}
		if rec.Code != http.StatusUnauthorized {
			t.Errorf("%s %s 应被未认证拦下，得到 %d", tc.method, tc.path, rec.Code)
		}
	}
}
