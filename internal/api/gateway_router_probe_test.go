package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// 记录哪些路径经过了被包裹的中间件。
type middlewareProbe struct {
	seen []string
}

// passthrough 返回一个"透传"中间件，记录路径后继续调用 next。
func (p *middlewareProbe) passthrough(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.seen = append(p.seen, r.URL.Path)
		next.ServeHTTP(w, r)
	})
}

// reject 返回一个"拒绝"中间件（模拟未认证/限流不可用），记录路径后直接 401/503。
func (p *middlewareProbe) reject(status int) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			p.seen = append(p.seen, r.URL.Path)
			w.WriteHeader(status)
			// 刻意不调用 next：模拟认证失败/限流 fail-close 时请求不会到达业务 handler
		})
	}
}

func (p *middlewareProbe) hit(path string) bool {
	for _, s := range p.seen {
		if s == path {
			return true
		}
	}
	return false
}

// TestLogoutRequiresAuthMiddleware 是 SEC-10 的回归测试。
//
// 背景：POST /v1/auth/logout 曾经只挂 rlMW，没有 authMW。而 Logout 的实现是
//
//	if claims := auth.GetClaims(r.Context()); claims != nil { ...写吊销黑名单... }
//
// 没有 authMW 注入 claims，claims 恒为 nil，函数体从不执行 —— "退出登录"是空操作，
// token 一路有效到自然过期（默认 24h）。这是凭证泄露后唯一的本地止损手段。
//
// 这条测试盯的是"中间件链路"，而不是 Logout 的内部实现，因为缺陷就在链路上。
func TestLogoutRequiresAuthMiddleware(t *testing.T) {
	mux := http.NewServeMux()
	auth := &middlewareProbe{}
	rl := &middlewareProbe{}
	// authMW 拒绝（模拟未认证），rlMW 透传。若 logout 挂了 authMW，
	// 请求会被 authMW 拦下（记录到 auth.seen）；否则 rlMW 会放行到 handler。
	registerAuthRoutes(mux, &AuthHandler{}, auth.reject(http.StatusUnauthorized), rl.passthrough)

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/auth/logout", nil))

	if !auth.hit("/v1/auth/logout") {
		t.Fatal("POST /v1/auth/logout 未经过 authMW：claims 恒为 nil，登出不会吊销 token")
	}
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("未认证的登出应被 authMW 拒绝，得到 %d", rec.Code)
	}
}

// TestProbesBypassRateLimitMiddleware 是 SCL-1 的回归测试。
//
// 背景：/health 与 /ready 曾经挂在 rlMW 上，而限流在 Redis 不可用时是 fail-close。
// 于是 Redis 一抖，kubelet 的 livenessProbe 跟着失败 → 全副本重启风暴。
// 重启救不了 Redis，却会丢掉全部会话热缓存，把依赖抖动放大成全站抖动。
func TestProbesBypassRateLimitMiddleware(t *testing.T) {
	mux := http.NewServeMux()
	auth := &middlewareProbe{}
	rl := &middlewareProbe{}
	registerPublicEndpoints(mux, auth.passthrough, rl.reject(http.StatusServiceUnavailable), nil, nil, nil, nil, nil, nil)

	for _, path := range []string{"/health", "/ready"} {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))

		if rl.hit(path) {
			t.Errorf("%s 经过了限流中间件：Redis 抖动会连带打挂存活探针 → 全副本重启", path)
		}
	}

	// /health 不查任何依赖，必须稳定 200 —— 否则 liveness 会随依赖抖动一起失败。
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/health", nil))
	if rec.Code != http.StatusOK {
		t.Errorf("/health 应稳定返回 200（它不查依赖），得到 %d", rec.Code)
	}

	// /ready 会自行检查 Redis/PG，依赖不可用时返回 503 是**正确语义**，
	// 因此这里不断言它的状态码 —— 只断言它没有被限流中间件污染。
}
