package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/session"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 取消路径**归属校验分支**的真实 PostgreSQL 用例。
//
// 为什么需要它：`session_cancel_test.go` 覆盖的是**不需要库**的分支（本地属主 / 非属主、
// 缺身份、缺参数），并且**自陈**过 ——
//
//	"归属校验分支需要真实 PostgreSQL（sessionMgr.GetSession），这里覆盖不需要库的那些"
//
// 而那条分支正是**授权边界**（`handlers.go:96-110`）：本实例没有该会话的本地任务时，
// 请求会走**广播分支**，而广播载荷只有 sessionID（给子 Agent 的那条更是不带身份），
// 接收端无从验证 —— **不校验就等于任何登录用户拿着 sessionID 就能取消别人的运行**
// （跨租户 DoS）。它对应 `docs/deployment-multi-instance.md` §9「取消**只能取消自己的会话**」
// 一行里点名的"归属校验分支需要真实 PG 的自动化用例**尚未补**"。
//
// 取值口径与既有 `*_live_test.go` 一致：`CHIRON_TEST_POSTGRES_DSN` 未设置则 skip
// （CI 的 real-stack job 会注入）。
//
// 建数据时的两个坑（都踩过，写下来免得下次再撞）：
//   - `sessions.user_id` 在 SQL 里被 `$3::uuid` 转型 ⇒ 属主 id **必须是 UUID 字符串**；
//   - `sessions.id` 是 **varchar(36)** ⇒ 会话 id 只能是裸 UUID，**不能加前缀**；
//   - `sessions.user_id` 有外键指向 `users` ⇒ 必须先建用户（`users` 只有 `id` 必填）。
func withLiveSessionManager(t *testing.T) (*session.Manager, *pgxpool.Pool) {
	t.Helper()
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置：跳过真实 PostgreSQL 用例")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("连接 PostgreSQL 失败: %v", err)
	}
	t.Cleanup(pool.Close)
	return session.NewManager(pool, db.Redis), pool
}

// liveUser 建一个只属于本次用例的用户（`users` 表只有 `id` 是必填），用例结束删除。
// `name` 打上可检索的标记，便于核对"跑完没有残留"。
func liveUser(t *testing.T, pool *pgxpool.Pool) string {
	t.Helper()
	id := uuid.NewString()
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO users (id, name) VALUES ($1, $2)`, id, "pytest-cancel-live"); err != nil {
		t.Fatalf("建用户失败: %v", err)
	}
	t.Cleanup(func() {
		if _, err := pool.Exec(context.Background(), `DELETE FROM users WHERE id = $1`, id); err != nil {
			t.Logf("清理用户 %s 失败: %v", id, err)
		}
	})
	return id
}

// liveSession 建一个只属于本次用例的会话，用例结束删除（不污染开发库）。
// 清理按 LIFO：会话先删、用户后删，否则会撞 `sessions_user_id_fkey`。
func liveSession(t *testing.T, mgr *session.Manager, owner string) string {
	t.Helper()
	sid := uuid.NewString()
	if _, err := mgr.CreateSession(context.Background(), sid, owner, "cancel live test"); err != nil {
		t.Fatalf("建会话失败: %v", err)
	}
	t.Cleanup(func() {
		if err := mgr.DeleteSession(context.Background(), sid); err != nil {
			t.Logf("清理会话 %s 失败: %v", sid, err)
		}
	})
	return sid
}

// **授权断言**：别人的会话 ⇒ 403，且理由说明归属不符。
func TestLiveCancelForeignSessionIsForbidden(t *testing.T) {
	mgr, pool := withLiveSessionManager(t)
	owner, intruder := liveUser(t, pool), liveUser(t, pool)
	sid := liveSession(t, mgr, owner)

	if _, ok := sessionCancels.Load(sid); ok {
		t.Fatalf("前置条件不成立：%s 不该有本地任务条目（否则测的是本地分支）", sid)
	}

	rec := httptest.NewRecorder()
	handleCancel(rec, cancelRequest(intruder, sid), mgr)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("非属主取消别人的会话应 403，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "does not belong") {
		t.Fatalf("403 的理由应说明归属不符，得到 %s", rec.Body.String())
	}
}

// **正对照**：属主走同一条分支必须放行 —— 否则上一条只是"永远 403"的假保证。
func TestLiveCancelOwnerSessionPassesOwnershipCheck(t *testing.T) {
	mgr, pool := withLiveSessionManager(t)
	owner := liveUser(t, pool)
	sid := liveSession(t, mgr, owner)

	// 让广播结果可预测：没有 Redis 时 `CancelSessionBroadcast` 失败 ⇒ `no_active_task`。
	// 200（而非 403）即证明归属校验放行了。
	prev := db.Redis
	db.Redis = nil
	t.Cleanup(func() { db.Redis = prev })

	rec := httptest.NewRecorder()
	handleCancel(rec, cancelRequest(owner, sid), mgr)

	if rec.Code != http.StatusOK {
		t.Fatalf("属主取消应 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if body := rec.Body.String(); !strings.Contains(body, "no_active_task") {
		t.Fatalf("无 Redis 时应回 no_active_task，得到 %s", body)
	}
}

// 会话不存在：**既不广播也不报错**（否则所有副本都去找一个不存在的会话）。
func TestLiveCancelUnknownSessionIsNoop(t *testing.T) {
	mgr, _ := withLiveSessionManager(t)

	rec := httptest.NewRecorder()
	handleCancel(rec, cancelRequest(uuid.NewString(), uuid.NewString()), mgr)

	if rec.Code != http.StatusOK {
		t.Fatalf("会话不存在应 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if body := rec.Body.String(); !strings.Contains(body, "no_active_task") {
		t.Fatalf("会话不存在应回 no_active_task，得到 %s", body)
	}
}
