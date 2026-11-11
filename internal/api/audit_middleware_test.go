package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// 审计日志必须在 handler 返回后仍能写入。
//
// LoggingMiddleware 是在 next.ServeHTTP 返回**之后**才把任务塞进 auditLogCh，
// 而真实 net/http server 在 ServeHTTP 返回时立即取消请求 context。如果入队时
// 直接传 r.Context()，worker 真正执行 XAdd 时 ctx 早已 Done，写入必然失败 ——
// 由于失败路径只打 Warn、不影响响应，线上表现为「审计日志几乎全丢」且极难察觉。
//
// 本用例显式模拟「ServeHTTP 返回后取消 ctx」，锁住这个语义。
func TestAuditLogContextSurvivesRequestCancellation(t *testing.T) {
	// 排空通道，避免其他用例残留干扰断言。
	for len(auditLogCh) > 0 {
		<-auditLogCh
	}

	h := LoggingMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
	}))

	reqCtx, cancel := context.WithCancel(context.Background())
	defer cancel()

	req := httptest.NewRequest(http.MethodPost, "/v1/things", nil)
	req = req.WithContext(auth.WithClaims(reqCtx, &auth.Claims{UserID: "u1", TenantID: 7}))

	h.ServeHTTP(httptest.NewRecorder(), req)

	// 模拟 server 在 ServeHTTP 返回后取消请求 ctx。
	cancel()

	var task auditLogTask
	select {
	case task = <-auditLogCh:
	default:
		t.Fatal("非 GET 请求未入队任何审计任务")
	}

	if task.userID != "u1" || task.tenantID != 7 {
		t.Fatalf("审计任务未携带 claims：userID=%q tenantID=%d", task.userID, task.tenantID)
	}
	if task.status != http.StatusCreated {
		t.Fatalf("审计任务状态码 = %d，期望 %d", task.status, http.StatusCreated)
	}

	// 核心断言：入队的 ctx 不能继承请求的取消链。
	if err := task.ctx.Err(); err != nil {
		t.Fatalf("入队 ctx 已随之取消（%v）：worker 写 Redis 会失败，审计日志将静默丢失；"+
			"入队时应使用 context.WithoutCancel", err)
	}
}

// GET/OPTIONS 与 5xx 响应不产生审计记录，避免通道被无意义条目占满。
func TestAuditLogSkipsReadOnlyAndServerErrors(t *testing.T) {
	for len(auditLogCh) > 0 {
		<-auditLogCh
	}

	cases := []struct {
		name    string
		method  string
		status  int
		wantLog bool
	}{
		{"GET 不记录", http.MethodGet, http.StatusOK, false},
		{"OPTIONS 不记录", http.MethodOptions, http.StatusNoContent, false},
		{"5xx 不记录", http.MethodPost, http.StatusInternalServerError, false},
		{"POST 正常记录", http.MethodPost, http.StatusOK, true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			for len(auditLogCh) > 0 {
				<-auditLogCh
			}

			status := tc.status
			h := LoggingMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(status)
			}))

			req := httptest.NewRequest(tc.method, "/v1/things", nil)
			req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1"}))

			h.ServeHTTP(httptest.NewRecorder(), req)

			got := len(auditLogCh) > 0
			if got != tc.wantLog {
				t.Fatalf("%s %d：入队=%v，期望 %v", tc.method, tc.status, got, tc.wantLog)
			}
		})
	}
}
