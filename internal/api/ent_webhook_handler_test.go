package api

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/athenavi/chiron/config"
)

// P3：`POST /v1/internal/webhook-event` 曾以 `internalTokenMW(nil, …)` 注册 ——
// 而 internalTokenMW 首行即读 `cfg.InternalToken`，于是**每个请求都 nil 解引用 panic**
// （未认证即可无限触发），且该端点还漏了 rlMW。本用例把两件事都钉住。
func TestInternalWebhookEventRequiresTokenWithoutPanic(t *testing.T) {
	mux := http.NewServeMux()
	auth := &middlewareProbe{}
	rl := &middlewareProbe{}
	// 用真实 cfg（InternalToken 为空）：修复前这里会 panic
	NewEntWebhookHandler().RegisterRoutes(mux, auth.passthrough, rl.passthrough, &config.Config{})

	const path = "/v1/internal/webhook-event"
	rec := httptest.NewRecorder()
	// 不带 X-Internal-Token ⇒ 应得 401（此前是 nil 解引用 panic）
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, path, nil))

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("未带内部令牌应 401（而不是 panic/500），得到 %d", rec.Code)
	}
	if !rl.hit(path) {
		t.Error("该端点应经过限流中间件（此前漏挂）")
	}
}
