package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
)

// 「同一会话不会并发两个 runtime」的**HTTP 级**取证。
//
// 此前这条保证只有锁原语级的用例（session_coord_live_test.go），而 round-14 写进对外保证表时
// 只能标注"两实例端到端尚未自动化"。这里用**真实路由**（registerAgentRoutes 注册的
// `POST /v1/agent/submit`）+ 真实 Redis，验证"另一个实例持有 run 锁时，本实例的提交被拒"，
// 并用一条正对照证明它**不是**"什么都拒"。
//
// 不启第二个进程、也不需要引擎：锁属于网关侧，提交在触达引擎之前就会被拒。

func submitOnce(t *testing.T, sid string) *httptest.ResponseRecorder {
	t.Helper()
	mux := http.NewServeMux()
	// authMW 换成一个只注入 claims 的透传（本条测的是锁，不是鉴权）；其余中间件透传。
	injectClaims := func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx := auth.WithClaims(r.Context(), &auth.Claims{TenantID: "t1", UserID: "u1"})
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
	passthrough := func(next http.Handler) http.Handler { return next }
	registerAgentRoutes(
		mux, injectClaims, passthrough, passthrough, passthrough,
		nil, // submitHandler：被拒路径用不到；放行路径由其 goroutine 触发 panic 并被 recover 吞掉
		nil, // billingMgr
		NewSharedSemaphore(db.Redis, "pytest-submit-gate", 10),
		NewTenantResourceManager(db.Redis),
		nil, nil, nil, nil, // eventHub / sessionMgr / authenticator / rpaHub
		"pytest-internal-token", time.Second,
	)

	body := strings.NewReader(`{"content":"hi","session_id":"` + sid + `"}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/agent/submit", body)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	return rec
}

func TestLiveSubmitRejectedWhenAnotherInstanceHoldsRunLock(t *testing.T) {
	withLiveRedis(t)
	sid := "pytest-submit-lock-" + uuid.NewString()
	ctx := context.Background()

	// 模拟"另一个网关实例正持有该会话的 run 锁"
	release, ok, err := AcquireSessionRunLock(ctx, sid, "other-instance-token")
	if err != nil || !ok {
		t.Fatalf("预占 run 锁失败：ok=%v err=%v", ok, err)
	}
	defer release()

	rec := submitOnce(t, sid)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("另一实例持有 run 锁时必须拒（400），得到 %d：%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "already has a turn in progress") {
		t.Fatalf("拒绝理由必须说清会话已在跑，得到 %s", rec.Body.String())
	}
}

func TestLiveSubmitPassesLockCheckWhenLockFree(t *testing.T) {
	withLiveRedis(t)
	sid := "pytest-submit-free-" + uuid.NewString()

	// 正对照：没有别的实例持锁 ⇒ 不该被"会话忙"拒掉（此后的执行链与本条无关）。
	rec := submitOnce(t, sid)
	if rec.Code == http.StatusBadRequest &&
		strings.Contains(rec.Body.String(), "already has a turn in progress") {
		t.Fatalf("无锁占用时不应被判为会话忙：%s", rec.Body.String())
	}
	if rec.Code != http.StatusAccepted {
		t.Fatalf("无锁占用时应受理（202），得到 %d：%s", rec.Code, rec.Body.String())
	}
	// 等后台 run 收尾（它会在 nil submitHandler 上 panic 并被 recover，然后释放锁）——
	// 否则测试结束时仍有 goroutine 触碰全局 db.Redis，与 withLiveRedis 的还原相互竞争。
	waitLockReleased(t, sid)
}

func waitLockReleased(t *testing.T, sid string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		release, ok, err := AcquireSessionRunLock(context.Background(), sid, "pytest-probe")
		if err == nil && ok {
			release()
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("等待 run 锁释放超时：%s", sid)
}
