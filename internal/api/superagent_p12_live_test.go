package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// P1-2 的四条验收此前只有静态检查：读 SQL 能看到 `now() + make_interval(...)`、
// 能看到归属校验写在 INSERT 之前，但"PG 真的这样算"、"拒绝时表里真的没多出行"、
// "A 真的停不了 B"都只有跑一次才知道。
//
// 取值口径与既有 `*_live_test.go` 一致：`CHIRON_TEST_POSTGRES_DSN` 未设置即 skip。
// 跑法要带 `Live`：`go test -mod=mod -run 'Live' ./internal/api/`。

func withP12Pool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	pool := liveSmokePool(t)
	db.PoolMu.Lock()
	prev := db.Pool
	db.Pool = pool
	db.PoolMu.Unlock()
	t.Cleanup(func() {
		db.PoolMu.Lock()
		db.Pool = prev
		db.PoolMu.Unlock()
	})
	return pool
}

// p12Cleanup 按外键逆序清理：takeovers/schedules 引用 sessions，sessions 引用 users。
// 必须**在** liveSmokeFixture 之后注册 —— 否则它先于 fixture 的删除执行不到。
func p12Cleanup(t *testing.T, pool *pgxpool.Pool, userID string) {
	t.Helper()
	t.Cleanup(func() {
		ctx := context.Background()
		for _, sql := range []string{
			`DELETE FROM superagent_takeovers WHERE user_id = $1`,
			`DELETE FROM superagent_schedules WHERE user_id = $1`,
			`DELETE FROM sessions WHERE user_id = $1`,
		} {
			if _, err := pool.Exec(ctx, sql, userID); err != nil {
				t.Logf("清理失败 %q: %v", sql, err)
			}
		}
	})
}

func p12Session(t *testing.T, pool *pgxpool.Pool, tenantID int16, userID string) string {
	t.Helper()
	id := uuid.NewString()
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO sessions (id, tenant_id, user_id, title, status)
		 VALUES ($1, $2, $3, 'p12', 'active')`, id, tenantID, userID); err != nil {
		t.Fatalf("建会话失败: %v", err)
	}
	return id
}

func p12Post(t *testing.T, h *SuperAgentHandler, target string,
	tenantID int16, userID string, body any,
	fn func(*SuperAgentHandler, *httptest.ResponseRecorder, *http.Request)) *httptest.ResponseRecorder {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, target, bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{
		TenantID: tenantID,
		UserID:   userID,
	}))
	// `GetUserID` / `GetTenantID` 读的是 `TenantMiddleware` 写进去的两个 ctx key，
	// **不是** claims。只挂 claims 会得到 401（`auth_required`）—— 这正是本文件
	// 第一次跑真库时踩到的：测试脚手架比被测代码更容易假装成"实现有问题"。
	ctx := context.WithValue(req.Context(), CtxKeyUserID, userID)
	ctx = context.WithValue(ctx, CtxKeyTenantID, tenantID)
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()
	fn(h, rec, req)
	return rec
}

type p12TakeoverConfig struct {
	Enabled         bool     `json:"enabled"`
	SessionIDs      []string `json:"session_ids"`
	DurationSeconds int      `json:"duration_seconds"`
	TokenCap        int64    `json:"token_cap"`
	TickSeconds     int      `json:"tick_seconds"`
	MaxRounds       int      `json:"max_rounds"`
}

func p12SaveConfig(t *testing.T, h *SuperAgentHandler, tenantID int16, userID string,
	ids []string, duration int) *httptest.ResponseRecorder {
	t.Helper()
	return p12Post(t, h, "/v1/superagent/config", tenantID, userID,
		p12TakeoverConfig{
			Enabled: true, SessionIDs: ids, DurationSeconds: duration,
			TickSeconds: 180,
		},
		func(h *SuperAgentHandler, w *httptest.ResponseRecorder, r *http.Request) {
			h.SaveConfig(w, r)
		})
}

// ends_at 必须由 PostgreSQL 计算，而不是进程时钟。
//
// 这条断言用"精确整秒"来证明来源：`make_interval(secs => $5)` 在 PG 里产生的是
// 整秒间隔，两个 timestamptz 相减得到精确的 interval '600 seconds'；若改回 Go 侧
// `time.Now().Add(...)`，微秒级抖动会让差值落在 599.9999~600.0001 之间，这条会失败。
// 顺带钉住 `started_at`（DEFAULT now()）与 `ends_at` 用的是**同一个**事务时钟。
func TestLiveTakeoverEndsAtComesFromPostgresClock(t *testing.T) {
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)

	sid := p12Session(t, pool, tenantID, userID)
	rec := p12SaveConfig(t, NewSuperAgentHandler(nil, nil), tenantID, userID, []string{sid}, 600)
	if rec.Code != http.StatusCreated {
		t.Fatalf("开接管失败: status=%d body=%s", rec.Code, rec.Body.String())
	}

	var exact bool
	if err := pool.QueryRow(context.Background(), `
		SELECT (ends_at - started_at) = interval '600 seconds'
		  FROM superagent_takeovers
		 WHERE session_id = $1`, sid).Scan(&exact); err != nil {
		t.Fatalf("读回差异失败: %v", err)
	}
	if !exact {
		var delta string
		_ = pool.QueryRow(context.Background(),
			`SELECT (ends_at - started_at)::text FROM superagent_takeovers WHERE session_id = $1`,
			sid).Scan(&delta)
		t.Fatalf("ends_at - started_at 应为精确的 600 秒（说明两端都来自 PG 时钟），得到 %s", delta)
	}
}

// 归属校验必须发生在写表之前：跨用户的会话被拒时，表里一行都不该多出来。
//
// "先插后校验再回滚"在单测里看不出区别（最终都 rollback），但真库上会暴露成
// 序列号跳变、触发器副作用与 advisory lock 之外的可观测痕迹 —— 更重要的是
// 一旦有人把校验挪到 INSERT 之后且**不再回滚**，这里会立刻失败。
func TestLiveTakeoverRejectsForeignSessionBeforeInsert(t *testing.T) {
	pool := withP12Pool(t)
	tenantA, userA := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userA)

	// 同租户的另一个用户：验证判据是 user_id，而不是 tenant_id。
	tenantB, userB := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userB)

	foreign := p12Session(t, pool, tenantB, userB)

	rec := p12SaveConfig(t, NewSuperAgentHandler(nil, nil), tenantA, userA, []string{foreign}, 600)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("用别人的会话开接管应 403，得到 status=%d body=%s", rec.Code, rec.Body.String())
	}

	var rows int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM superagent_takeovers WHERE user_id = $1`, userA).Scan(&rows); err != nil {
		t.Fatalf("计数失败: %v", err)
	}
	if rows != 0 {
		t.Fatalf("拒绝后不该留下接管行，得到 %d 行", rows)
	}

	// 同租户也不行：判据是 user_id，租户一致不代表可以替别人开接管。
	var sameTenant int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM superagent_takeovers WHERE session_id = $1`, foreign).Scan(&sameTenant); err != nil {
		t.Fatalf("计数失败: %v", err)
	}
	if sameTenant != 0 {
		t.Fatalf("被拒的会话上不该有接管行，得到 %d 行", sameTenant)
	}
}

// A 停不了 B 的接管：`Stop` 的 WHERE 带 `user_id = $2`，所以别人的 id 落到"没有可停的接管"。
// 这里断言的是**状态没被改**，而不只是响应码 —— 只查响应码的话，SQL 里漏掉
// `AND user_id = $2` 但恰好返回 404 的实现也能蒙混过关。
func TestLiveStopCannotTouchAnotherUsersTakeover(t *testing.T) {
	pool := withP12Pool(t)
	tenantA, userA := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userA)

	tenantB, userB := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userB)

	sidB := p12Session(t, pool, tenantB, userB)
	h := NewSuperAgentHandler(nil, nil)
	if rec := p12SaveConfig(t, h, tenantB, userB, []string{sidB}, 600); rec.Code != http.StatusCreated {
		t.Fatalf("B 开接管失败: status=%d body=%s", rec.Code, rec.Body.String())
	}

	var takeoverID string
	if err := pool.QueryRow(context.Background(),
		`SELECT id FROM superagent_takeovers WHERE session_id = $1`, sidB).Scan(&takeoverID); err != nil {
		t.Fatalf("读回接管 id 失败: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/v1/superagent/takeovers/"+takeoverID+"/stop", nil)
	req.SetPathValue("id", takeoverID)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{
		TenantID: tenantA, UserID: userA,
	}))
	ctx := context.WithValue(req.Context(), CtxKeyUserID, userA)
	ctx = context.WithValue(ctx, CtxKeyTenantID, tenantA)
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()
	h.Stop(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("A 停 B 的接管应 404（不泄漏存在性），得到 status=%d body=%s",
			rec.Code, rec.Body.String())
	}

	var status string
	if err := pool.QueryRow(context.Background(),
		`SELECT status FROM superagent_takeovers WHERE id = $1`, takeoverID).Scan(&status); err != nil {
		t.Fatalf("读状态失败: %v", err)
	}
	if status != takeoverStatusActive {
		t.Fatalf("B 的接管不该被 A 改动，status=%s", status)
	}
}

// 第 6 个并发接管被拒、第 11 个定时任务被拒 —— 两条都是"配额判定与插入同事务"的证明。
// 单测能验 `limit` 常量，验不了 advisory lock 之后的 `count(*)` 与 INSERT 是否真的
// 落在同一个临界区。
func TestLiveTakeoverAndScheduleQuotas(t *testing.T) {
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)

	h := NewSuperAgentHandler(nil, nil)

	// ── 接管：一次请求最多 maxActiveTakeovers 个会话，所以分两批。
	first := make([]string, 0, maxActiveTakeovers)
	for i := 0; i < maxActiveTakeovers; i++ {
		first = append(first, p12Session(t, pool, tenantID, userID))
	}
	if rec := p12SaveConfig(t, h, tenantID, userID, first, 600); rec.Code != http.StatusCreated {
		t.Fatalf("开满 %d 个接管应成功: status=%d body=%s", maxActiveTakeovers, rec.Code, rec.Body.String())
	}

	sixth := p12Session(t, pool, tenantID, userID)
	rec := p12SaveConfig(t, h, tenantID, userID, []string{sixth}, 600)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("第 %d 个接管应被拒: status=%d body=%s", maxActiveTakeovers+1, rec.Code, rec.Body.String())
	}

	var active int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM superagent_takeovers
		  WHERE user_id = $1 AND status IN ('active','stopping')`, userID).Scan(&active); err != nil {
		t.Fatalf("计数失败: %v", err)
	}
	if active != maxActiveTakeovers {
		t.Fatalf("被拒后仍应恰好 %d 个生效接管，得到 %d", maxActiveTakeovers, active)
	}

	// ── 定时任务：同一批会话可以被多个任务引用，所以只需一个会话。
	schSession := p12Session(t, pool, tenantID, userID)
	mk := func() *httptest.ResponseRecorder {
		return p12Post(t, h, "/v1/superagent/schedules", tenantID, userID,
			scheduleRequest{
				Name: "p12", Schedule: "0 3 * * *", WindowSeconds: 600,
				SessionIDs: []string{schSession}, Enabled: false,
			},
			func(h *SuperAgentHandler, w *httptest.ResponseRecorder, r *http.Request) {
				h.CreateSchedule(w, r)
			})
	}
	for i := 0; i < maxSuperAgentSchedules; i++ {
		if rec := mk(); rec.Code != http.StatusCreated {
			t.Fatalf("第 %d 个定时任务应成功: status=%d body=%s", i+1, rec.Code, rec.Body.String())
		}
	}
	if rec := mk(); rec.Code != http.StatusBadRequest {
		t.Fatalf("第 %d 个定时任务应被拒: status=%d body=%s",
			maxSuperAgentSchedules+1, rec.Code, rec.Body.String())
	}

	var schedules int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM superagent_schedules WHERE user_id = $1`, userID).Scan(&schedules); err != nil {
		t.Fatalf("计数失败: %v", err)
	}
	if schedules != maxSuperAgentSchedules {
		t.Fatalf("被拒后仍应恰好 %d 条任务，得到 %d", maxSuperAgentSchedules, schedules)
	}
}
