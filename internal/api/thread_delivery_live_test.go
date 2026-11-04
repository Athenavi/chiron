package api

import (
	"context"
	"testing"

	"github.com/google/uuid"
)

// P1-1 投递层队列侧的验收（roadmap P1-1.1 投递路径第 3、4 步的下半段）。
//
// 这一层有三件事只有跑一次才知道真假：
//   - `FOR UPDATE SKIP LOCKED` 写在 UPDATE … WHERE (…) = (SELECT …) 这种形态里到底
//     能不能编译执行（子查询里带 LIMIT + 锁定子句，语法位置很容易写错）；
//   - 排序是不是真的"最早优先"，而且同 created_at 时结果稳定；
//   - 收尾的 UPDATE 是不是幂等 —— 它会被重复调用（回合结束 + 进程退出清理）。
//
// 取值口径与既有 `*_live_test.go` 一致：`CHIRON_TEST_POSTGRES_DSN` 未设置即 skip。
// 跑法：`go test -mod=mod -run 'LiveThreadDelivery' ./internal/api/`。

func TestLiveClaimThreadDeliveryTakesOldestFirst(t *testing.T) {
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	ctx := context.Background()
	sessionID := p12Session(t, pool, tenantID, userID)

	// threads 引用 users，必须在 fixture 删 users **之前**清掉。
	// t.Cleanup 是 LIFO：这里后注册，所以先执行。
	// 两条 SQL 的过滤列不同（投递行按 session_id，thread 按 user_id），
	// 不能塞进一个循环里共用同一个参数 —— 那样 where 永远不匹配，行会留着不删。
	t.Cleanup(func() {
		// 顺序必须由引用方到被引用方：投递行 → threads → sessions。
		// 漏掉 sessions 会让 fixture 删 users 时撞 `sessions_user_id_fkey`，
		// 表现为一堆"清理失败"告警 + 库里留下残留行。
		for _, c := range []struct {
			sql string
			arg string
		}{
			{`DELETE FROM thread_deliveries WHERE session_id = $1`, sessionID},
			{`DELETE FROM threads WHERE user_id = $1`, userID},
			{`DELETE FROM sessions WHERE id = $1`, sessionID},
		} {
			if _, err := pool.Exec(context.Background(), c.sql, c.arg); err != nil {
				t.Logf("清理失败 %q: %v", c.sql, err)
			}
		}
	})

	seed := func(createdAt string, eventKey string) string {
		t.Helper()
		threadID := uuid.NewString()
		if _, err := pool.Exec(ctx,
			`INSERT INTO threads (id, tenant_id, user_id, source_kind, external_key, created_at)
			 VALUES ($1, $2, $3, 'job', $4, $5)`,
			threadID, tenantID, userID, eventKey, createdAt); err != nil {
			t.Fatalf("建 thread 失败: %v", err)
		}
		if _, err := pool.Exec(ctx,
			`INSERT INTO thread_deliveries (thread_id, event_key, session_id, status, created_at)
			 VALUES ($1, $2, $3, 'queued', $4)`,
			threadID, eventKey, sessionID, createdAt); err != nil {
			t.Fatalf("建投递行失败: %v", err)
		}
		return threadID
	}

	// 故意乱序插入：先晚后早，"最早优先"才不是插入顺序的巧合。
	lateKey := "evt-late"
	lateThread := seed("2026-01-02 00:00:00+00", lateKey)
	earlyKey := "evt-early"
	earlyThread := seed("2026-01-01 00:00:00+00", earlyKey)

	got, err := claimNextThreadDelivery(ctx, sessionID)
	if err != nil {
		t.Fatalf("领队失败: %v", err)
	}
	if got.EventKey != earlyKey || got.ThreadID != earlyThread {
		t.Fatalf("应领最早的一条 (%s/%s)，实际领到 (%s/%s)",
			earlyKey, earlyThread, got.EventKey, got.ThreadID)
	}

	// 领走即置 running：再领一次必须拿到第二条，而不是同一条。
	second, err := claimNextThreadDelivery(ctx, sessionID)
	if err != nil {
		t.Fatalf("第二次领队失败: %v", err)
	}
	if second.EventKey != lateKey || second.ThreadID != lateThread {
		t.Fatalf("应领第二条 (%s/%s)，实际领到 (%s/%s)",
			lateKey, lateThread, second.EventKey, second.ThreadID)
	}

	// 队列空了要给出可判别的哨兵错误，而不是 nil, nil —— 后者会让调用方
	// 拿着零值结构体去开一个 thread_id 为空的回合。
	if _, emptyErr := claimNextThreadDelivery(ctx, sessionID); emptyErr != errNoPendingDelivery {
		t.Fatalf("空队列应返回 errNoPendingDelivery，实际 %v", emptyErr)
	}

	// running 的行不该被再次领走（状态机单向）。
	var running int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM thread_deliveries WHERE session_id = $1 AND status = 'running'`,
		sessionID).Scan(&running); err != nil {
		t.Fatalf("统计 running 失败: %v", err)
	}
	if running != 2 {
		t.Fatalf("应有 2 行 running，实际 %d", running)
	}
}

func TestLiveFinishThreadDeliveryIsIdempotent(t *testing.T) {
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	ctx := context.Background()
	sessionID := p12Session(t, pool, tenantID, userID)

	t.Cleanup(func() {
		// 与上一条用例同因：漏删 sessions 会让 fixture 删 users 时撞
		// `sessions_user_id_fkey`，留下残留行并打印一串"清理失败"。
		for _, c := range []struct {
			sql string
			arg string
		}{
			{`DELETE FROM thread_deliveries WHERE session_id = $1`, sessionID},
			{`DELETE FROM threads WHERE user_id = $1`, userID},
			{`DELETE FROM sessions WHERE id = $1`, sessionID},
		} {
			if _, err := pool.Exec(context.Background(), c.sql, c.arg); err != nil {
				t.Logf("清理失败 %q: %v", c.sql, err)
			}
		}
	})

	threadID := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO threads (id, tenant_id, user_id, source_kind, external_key)
		 VALUES ($1, $2, $3, 'subagent', $4)`, threadID, tenantID, userID, "evt-1"); err != nil {
		t.Fatalf("建 thread 失败: %v", err)
	}
	if _, err := pool.Exec(ctx,
		`INSERT INTO thread_deliveries (thread_id, event_key, session_id, status)
		 VALUES ($1, 'evt-1', $2, 'queued')`, threadID, sessionID); err != nil {
		t.Fatalf("建投递行失败: %v", err)
	}

	d, err := claimNextThreadDelivery(ctx, sessionID)
	if err != nil {
		t.Fatalf("领队失败: %v", err)
	}
	if err := finishThreadDelivery(ctx, d, threadDeliveryDone); err != nil {
		t.Fatalf("收尾失败: %v", err)
	}

	// 重复收尾必须**不生效**：`WHERE status = 'running'` 让它自然幂等。
	// 没有这个条件的话，第二次调用会把已 done 的行改写成 dropped —— 审计上
	// 表现为"跑完了又变成没跑"，而这正是这套状态机最该避免的自相矛盾。
	if err := finishThreadDelivery(ctx, d, threadDeliveryDropped); err != nil {
		t.Fatalf("重复收尾应静默成功，实际 %v", err)
	}
	var status string
	if err := pool.QueryRow(ctx,
		`SELECT status FROM thread_deliveries WHERE thread_id = $1 AND event_key = $2`,
		threadID, "evt-1").Scan(&status); err != nil {
		t.Fatalf("读状态失败: %v", err)
	}
	if status != threadDeliveryDone {
		t.Fatalf("重复收尾后状态应仍为 done，实际 %q", status)
	}

	// 非法终态直接报错，而不是悄悄写成 queued/running 这种会被人重投的值。
	if err := finishThreadDelivery(ctx, d, threadDeliveryQueued); err == nil {
		t.Fatal("finishThreadDelivery 应拒绝非终态")
	}
}
