package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// 这些用例盯的是**链路**（路由是否暴露、中间件是否挂齐）而不是 handler 实现，
// 与 gateway_router_probe_test.go 的判据同型：请求只有匹配到已注册的模式才会
// 进入中间件链，所以"探针记到了这个路径"等价于"该路径已注册"。

// TestDesktopInferRouteIsRegistered 防的是"handler 写好了但没注册"—— Go 的
// ServeMux 不会落到子 mux 去试，未注册就是 404（桌面端会以为云端不支持片段推理）。
func TestDesktopInferRouteIsRegistered(t *testing.T) {
	mux := http.NewServeMux()
	authProbe := &middlewareProbe{}
	rlProbe := &middlewareProbe{}
	registerDesktopRoutes(mux, authProbe.reject(http.StatusUnauthorized), rlProbe.passthrough, nil)

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/desktop/infer", strings.NewReader("{}")))

	if !authProbe.hit("/v1/desktop/infer") {
		t.Fatal("POST /v1/desktop/infer 未经过 authMW：匿名请求会带着 tenant_id=0 打到引擎")
	}
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("未认证的片段推理应被 authMW 拒绝，得到 %d", rec.Code)
	}
}

// TestDesktopInferRouteIsRateLimited：这个端点会真的花上游 token，只挂 authMW
// 等于给每个登录用户开一条不限速的打上游通道。
func TestDesktopInferRouteIsRateLimited(t *testing.T) {
	mux := http.NewServeMux()
	authProbe := &middlewareProbe{}
	rlProbe := &middlewareProbe{}
	registerDesktopRoutes(mux, authProbe.passthrough, rlProbe.reject(http.StatusTooManyRequests), nil)

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/desktop/infer", strings.NewReader("{}")))

	if !rlProbe.hit("/v1/desktop/infer") {
		t.Fatal("POST /v1/desktop/infer 未经过 rlMW：片段推理成了绕过限流的第二通道")
	}
	if rec.Code != http.StatusTooManyRequests {
		t.Errorf("被限流的请求应返回 429，得到 %d", rec.Code)
	}
}

// TestDesktopInferRejectsAnonymousRequestWithoutGateway：即使 pythonClient 为 nil
// （引擎未接线/启动中），也必须是"明确的错误"，不能 200 空响应。
func TestDesktopInferHandlerWithoutEngine(t *testing.T) {
	// pythonClient 为 nil：handler 必须先自报不可用，而不是把请求转给空客户端 panic。
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/desktop/infer", strings.NewReader("{}"))
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))
	DesktopInferHandler(nil).ServeHTTP(rec, req)

	if rec.Code != http.StatusInternalServerError {
		t.Errorf("引擎不可用应返回 500，得到 %d", rec.Code)
	}
}

// TestDesktopInferHandlerRequiresTenant：防御"路由被挪到没有 authMW 的地方"。
// tenant_id=0 在引擎侧不做租户隔离，放进去等于跨租户。
func TestDesktopInferHandlerRequiresTenant(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/desktop/infer", strings.NewReader("{}"))
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1"}))
	DesktopInferHandler(nil).ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("缺租户上下文应返回 401，得到 %d", rec.Code)
	}
}
