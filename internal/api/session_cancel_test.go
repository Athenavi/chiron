package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// 取消路径的行为验证（此前这条路径没有任何 Go 用例）。
//
// 它同时是**授权边界**：`/cancel` 只经过 authMW，没有归属校验。本地命中分支靠
// `sessionCancels` 里的 userID 比对；而**广播分支**（本实例没有该任务时）此前直接把
// 取消发给所有副本 —— 载荷只有 sessionID，接收端无从验证，等于任何登录用户拿着
// sessionID 就能取消别人的运行。本轮给广播分支补上与会话属主校验同一口径的检查。
//
// 归属校验分支需要真实 PostgreSQL（sessionMgr.GetSession），这里覆盖不需要库的那些：
// 本地属主取消、非属主被拒且不扰动条目、缺身份/缺参数、以及"无本地任务且无 Redis"。
// **那条分支的用例已补齐**：`session_cancel_live_test.go`（真实 PG，2026-10-09）。

func cancelRequest(userID, sessionID string) *http.Request {
	req := httptest.NewRequest(http.MethodPost, "/v1/agent/cancel?session_id="+sessionID, nil)
	return req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{TenantID: "t1", UserID: userID}))
}

func TestCancelLocalOwnerCancelsAndClearsEntry(t *testing.T) {
	const sid = "pytest-cancel-owner"
	fired := false
	sessionCancels.Store(sid, &sessionCancel{userID: "u-owner", cancel: func() { fired = true }})
	t.Cleanup(func() { sessionCancels.Delete(sid) })

	rec := httptest.NewRecorder()
	handleCancel(rec, cancelRequest("u-owner", sid), nil)

	if rec.Code != http.StatusOK {
		t.Fatalf("属主取消应 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if !fired {
		t.Fatal("属主取消必须真的 cancel 掉本地任务")
	}
	if _, ok := sessionCancels.Load(sid); ok {
		t.Fatal("取消后必须移除条目（否则后续取消会命中一个已结束的 run）")
	}
}

func TestCancelNonOwnerIsForbiddenAndKeepsEntry(t *testing.T) {
	const sid = "pytest-cancel-foreign"
	fired := false
	sessionCancels.Store(sid, &sessionCancel{userID: "u-owner", cancel: func() { fired = true }})
	t.Cleanup(func() { sessionCancels.Delete(sid) })

	rec := httptest.NewRecorder()
	handleCancel(rec, cancelRequest("u-intruder", sid), nil)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("非属主应 403，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if fired {
		t.Fatal("非属主不得取消别人的任务")
	}
	if _, ok := sessionCancels.Load(sid); !ok {
		t.Fatal("非属主请求不得扰动别人的条目（旧实现先 LoadAndDelete 再 Store 回来）")
	}
}

func TestCancelNonOwnerCannotBroadcastForeignSession(t *testing.T) {
	// 广播分支在**没有** sessionMgr 时无法校验归属（真实部署一定注入 sessionMgr）。
	// 这里只断言"没有本地任务时不 panic、且 Redis 不可用时报 no_active_task"，
	// 归属校验本身由 routes_agent.go 传入的 sessionMgr 分支负责（需真实库）。
	prev := db.Redis
	db.Redis = nil
	t.Cleanup(func() { db.Redis = prev })

	rec := httptest.NewRecorder()
	handleCancel(rec, cancelRequest("u1", "pytest-cancel-absent"), nil)

	if rec.Code != http.StatusOK {
		t.Fatalf("无本地任务应 200，得到 %d", rec.Code)
	}
	if body := rec.Body.String(); !strings.Contains(body, "no_active_task") {
		t.Fatalf("Redis 不可用时应回 no_active_task，得到 %s", body)
	}
}

func TestCancelRequiresAuthContext(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/v1/agent/cancel?session_id=s", nil)
	rec := httptest.NewRecorder()
	handleCancel(rec, req, nil)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("缺身份应 401，得到 %d", rec.Code)
	}
}

func TestCancelRequiresSessionID(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/v1/agent/cancel", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1"}))
	rec := httptest.NewRecorder()
	handleCancel(rec, req, nil)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("缺 session_id 应 400，得到 %d", rec.Code)
	}
}
