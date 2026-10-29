package api

import (
	"context"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 邀请码消费的**真实 PostgreSQL** 用例。
//
// 为什么必须是真库：`auth_invite_test.go` 用假 Tx 钉住了"只发一条语句 + 条件齐全"
// 这个**充分条件**，但"并发下恰好一个成功"是 PostgreSQL 行锁语义的产物 ——
// UPDATE 拿到行锁后**重新评估 WHERE** 这件事，任何假替身都表达不了。
//
// 取值口径与既有 `*_live_test.go` 一致：`CHIRON_TEST_POSTGRES_DSN` 未设置则 skip
// （CI 的 real-stack job 会注入）。

func liveInvitePool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置：跳过真实 PostgreSQL 用例")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatalf("解析 DSN 失败: %v", err)
	}
	// 并发用例要真正并发：连接数必须不小于并发度，否则一部分 goroutine 只是排队，
	// 竞争窗口被人为缩窄，测不出问题。
	cfg.MaxConns = 32
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatalf("连接 PostgreSQL 失败: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

// liveInvite 建一个只属于本次用例的租户 + 邀请码，用例结束删除。
//
// `maxUses = 0` 表示不限次；`expiresAt` 为 nil 表示永不过期。
// `tenant_invites.tenant_id` 没有外键，但仍然建真租户 —— 否则用例通过也说明不了
// 线上数据形态下的行为。
func liveInvite(t *testing.T, pool *pgxpool.Pool, maxUses int, expiresAt *time.Time) (code string, tenantID int16) {
	t.Helper()
	ctx := context.Background()

	if err := pool.QueryRow(ctx,
		`INSERT INTO tenants (name, status) VALUES ($1, $2) RETURNING id`,
		"invite-live-test", "active").Scan(&tenantID); err != nil {
		t.Fatalf("建租户失败: %v", err)
	}

	// code 是 varchar(64)：前缀 12 + uuid 36 = 48，留有余量。
	code = "live-inv-" + uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO tenant_invites (code, tenant_id, max_uses, uses, expires_at)
		 VALUES ($1, $2, $3, 0, $4)`,
		code, tenantID, maxUses, expiresAt); err != nil {
		t.Fatalf("建邀请码失败: %v", err)
	}

	t.Cleanup(func() {
		// 邀请码先删再删租户，避免将来补上外键后清理顺序失效。
		if _, err := pool.Exec(context.Background(),
			`DELETE FROM tenant_invites WHERE code = $1`, code); err != nil {
			t.Logf("清理邀请码 %s 失败: %v", code, err)
		}
		if _, err := pool.Exec(context.Background(),
			`DELETE FROM tenants WHERE id = $1`, tenantID); err != nil {
			t.Logf("清理租户 %d 失败: %v", tenantID, err)
		}
	})
	return code, tenantID
}

func liveInviteUses(t *testing.T, pool *pgxpool.Pool, code string) int {
	t.Helper()
	var uses int
	if err := pool.QueryRow(context.Background(),
		`SELECT uses FROM tenant_invites WHERE code = $1`, code).Scan(&uses); err != nil {
		t.Fatalf("读 uses 失败: %v", err)
	}
	return uses
}

const inviteExhaustedMessage = "邀请码无效、已过期或已达使用上限"

// TestLiveConsumeInvite_SingleUseInviteAdmitsExactlyOne 是这次修复的核心用例。
//
// 旧实现（先 SELECT 校验、再 UPDATE 自增）在 8 个并发事务下会让 `max_uses = 1`
// 的码被消费多次：每个事务的 SELECT 都读到 `uses = 0`，随后各自 +1。
func TestLiveConsumeInvite_SingleUseInviteAdmitsExactlyOne(t *testing.T) {
	pool := liveInvitePool(t)
	code, wantTenant := liveInvite(t, pool, 1, nil)

	const concurrency = 8
	type outcome struct {
		tenantID int16
		err      error
	}
	results := make([]outcome, concurrency)

	// start 是一个栅栏：先让所有 goroutine 都开好事务，再一起放行，
	// 尽最大可能让它们同时撞到 UPDATE 上。
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < concurrency; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			ctx := context.Background()

			tx, err := pool.Begin(ctx)
			if err != nil {
				results[i] = outcome{err: fmt.Errorf("begin: %w", err)}
				return
			}
			<-start

			tenantID, err := consumeInvite(ctx, tx, code)
			if err != nil {
				// 失败必须回滚，否则会一直占着行锁把其他 goroutine 卡住。
				_ = tx.Rollback(ctx)
				results[i] = outcome{err: err}
				return
			}
			// 提交才能证明 uses 真的落库（回滚的话计数也回滚，测不出超发）。
			results[i] = outcome{tenantID: tenantID, err: tx.Commit(ctx)}
		}(i)
	}
	close(start)
	wg.Wait()

	succeeded := 0
	for i, r := range results {
		if r.err == nil {
			succeeded++
			if r.tenantID != wantTenant {
				t.Errorf("第 %d 个成功者拿到租户 %d，应为 %d", i, r.tenantID, wantTenant)
			}
			continue
		}
		// 失败者必须是"码已用满"这一条，不能是数据库层报错（那说明是别的问题）。
		if r.err.Error() != inviteExhaustedMessage {
			t.Errorf("第 %d 个失败者的错误 = %q, want %q", i, r.err.Error(), inviteExhaustedMessage)
		}
	}

	if succeeded != 1 {
		t.Fatalf("max_uses=1 的邀请码在 %d 个并发事务下成功了 %d 次，必须恰好 1 次",
			concurrency, succeeded)
	}
	if got := liveInviteUses(t, pool, code); got != 1 {
		t.Fatalf("落库 uses = %d, want 1 —— 计数必须与实际成功次数一致", got)
	}
}

// TestLiveConsumeInvite_SingleUseInviteRejectsSecondAttempt 是不依赖并发调度的
// 互补用例：即使并发窗口没被撞上，"用满后再用一次"也必须被拒。
func TestLiveConsumeInvite_SingleUseInviteRejectsSecondAttempt(t *testing.T) {
	pool := liveInvitePool(t)
	code, _ := liveInvite(t, pool, 1, nil)
	ctx := context.Background()

	consume := func() (int16, error) {
		tx, err := pool.Begin(ctx)
		if err != nil {
			t.Fatalf("begin: %v", err)
		}
		tenantID, err := consumeInvite(ctx, tx, code)
		if err != nil {
			_ = tx.Rollback(ctx)
			return 0, err
		}
		return tenantID, tx.Commit(ctx)
	}

	if _, err := consume(); err != nil {
		t.Fatalf("第一次消费应成功，实际报错: %v", err)
	}
	if _, err := consume(); err == nil {
		t.Fatal("第二次消费应被拒，实际成功 —— max_uses 限制失效")
	} else if err.Error() != inviteExhaustedMessage {
		t.Errorf("第二次消费的错误 = %q, want %q", err.Error(), inviteExhaustedMessage)
	}
	if got := liveInviteUses(t, pool, code); got != 1 {
		t.Fatalf("被拒的那次不应增加计数，uses = %d, want 1", got)
	}
}

// TestLiveConsumeInvite_UnlimitedInviteIsNotCapped：`max_uses = 0` 表示不限次。
// 这条容易在改写 SQL 时被 `uses < max_uses` 误伤成"永远用不了"。
func TestLiveConsumeInvite_UnlimitedInviteIsNotCapped(t *testing.T) {
	pool := liveInvitePool(t)
	code, _ := liveInvite(t, pool, 0, nil)
	ctx := context.Background()

	const attempts = 5
	for i := 0; i < attempts; i++ {
		tx, err := pool.Begin(ctx)
		if err != nil {
			t.Fatalf("begin: %v", err)
		}
		if _, err := consumeInvite(ctx, tx, code); err != nil {
			_ = tx.Rollback(ctx)
			t.Fatalf("第 %d 次消费应成功（max_uses=0 不限次），实际报错: %v", i+1, err)
		}
		if err := tx.Commit(ctx); err != nil {
			t.Fatalf("第 %d 次提交失败: %v", i+1, err)
		}
	}
	if got := liveInviteUses(t, pool, code); got != attempts {
		t.Fatalf("uses = %d, want %d", got, attempts)
	}
}

// TestLiveConsumeInvite_ExpiredInviteIsRejected：过期判断也必须落在同一条 UPDATE 里。
func TestLiveConsumeInvite_ExpiredInviteIsRejected(t *testing.T) {
	pool := liveInvitePool(t)
	expired := time.Now().Add(-time.Hour)
	code, _ := liveInvite(t, pool, 0, &expired)
	ctx := context.Background()

	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	defer tx.Rollback(ctx)

	if _, err := consumeInvite(ctx, tx, code); err == nil {
		t.Fatal("过期邀请码应被拒，实际成功")
	} else if err.Error() != inviteExhaustedMessage {
		t.Errorf("错误 = %q, want %q", err.Error(), inviteExhaustedMessage)
	}
}

// TestLiveConsumeInvite_UnknownCodeIsRejected：不存在的码不能因为"影响 0 行"
// 被误判成成功（Scan 必须在没有 RETURNING 行时返回 ErrNoRows）。
func TestLiveConsumeInvite_UnknownCodeIsRejected(t *testing.T) {
	pool := liveInvitePool(t)
	ctx := context.Background()

	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	defer tx.Rollback(ctx)

	if _, err := consumeInvite(ctx, tx, "no-such-invite-"+uuid.NewString()); err == nil {
		t.Fatal("不存在的邀请码应被拒，实际成功")
	} else if err.Error() != inviteExhaustedMessage {
		t.Errorf("错误 = %q, want %q", err.Error(), inviteExhaustedMessage)
	}
}
