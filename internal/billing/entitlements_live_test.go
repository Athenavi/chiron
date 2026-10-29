package billing

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/jackc/pgx/v5/pgxpool"
)

// setupLivePool 建立测试连接池并挂到 db.Pool，返回可用池。
//
// 关闭与清理的先后由 t.Cleanup 的 LIFO 顺序保证：本函数先注册「清空 db.Pool →
// 关闭池」，测试用例后注册的数据清理因此会**先**执行；而 t.Cleanup 又总是在测试
// 函数的所有 defer 之后才跑，所以不能像普通场景那样 `defer pool.Close()` ——
// 那会让清理语句撞 "closed pool"。
func setupLivePool(t *testing.T, ctx context.Context, dsn string) *pgxpool.Pool {
	t.Helper()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	db.PoolMu.Lock()
	db.Pool = pool
	db.PoolMu.Unlock()
	t.Cleanup(func() {
		db.PoolMu.Lock()
		db.Pool = nil
		db.PoolMu.Unlock()
		pool.Close()
	})
	return pool
}

// 一次性套餐的核心验收：付款确认后 **credits 与权益一次性到位**，
// 重复确认不重复发放，且过期后权益自动失效（不需要任何定时任务）。
func TestLivePackagePurchaseGrantsCreditsAndEntitlement(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	pool := setupLivePool(t, ctx, dsn)

	var tenantID int16
	if err := pool.QueryRow(ctx,
		`INSERT INTO tenants (name, status) VALUES ($1, 'active') RETURNING id`,
		"pkg-test-"+id.NextID()).Scan(&tenantID); err != nil {
		t.Fatalf("建租户: %v", err)
	}
	userID, err := id.UUID()
	if err != nil {
		t.Fatalf("生成用户 ID: %v", err)
	}
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role) VALUES ($1, $2, $3, 'user')`,
		userID, tenantID, "pkg-buyer"); err != nil {
		t.Fatalf("建用户: %v", err)
	}

	pkgID, err := id.UUID()
	if err != nil {
		t.Fatalf("生成套餐 ID: %v", err)
	}
	// 套餐 code 必须唯一：ux_billing_packages_code 是全站唯一索引，而上一次失败的
	// 运行可能残留同 code 的行（清理没跑到）。用随机后缀让本测试可重复执行。
	pkgCode := "test_starter_" + id.NextID()
	if _, err := pool.Exec(ctx,
		`INSERT INTO billing_packages
		     (id, code, name, credits, price_cents, currency, duration_days, allowed_models)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
		pkgID, pkgCode, "测试入门包", 5000, 9900, "CNY", 30,
		[]string{"gpt-4o-mini", "deepseek-chat"}); err != nil {
		t.Fatalf("建套餐: %v", err)
	}

	t.Cleanup(func() {
		bg := context.Background()
		// 逆序清理：先删引用方，再删被引用方。
		for _, q := range []string{
			`DELETE FROM user_entitlements WHERE user_id = $1`,
			`DELETE FROM payments WHERE user_id = $1`,
			`DELETE FROM credit_transactions WHERE user_id = $1`,
		} {
			if _, e := pool.Exec(bg, q, userID); e != nil {
				t.Logf("清理失败（%s）: %v", q, e)
			}
		}
		if _, e := pool.Exec(bg, `DELETE FROM billing_packages WHERE id = $1`, pkgID); e != nil {
			t.Logf("清理套餐失败: %v", e)
		}
		if _, e := pool.Exec(bg, `DELETE FROM users WHERE id = $1`, userID); e != nil {
			t.Logf("清理用户失败: %v", e)
		}
		if _, e := pool.Exec(bg, `DELETE FROM tenants WHERE id = $1`, tenantID); e != nil {
			t.Logf("清理租户失败: %v", e)
		}
	})

	var beforeCredits int
	if err := pool.QueryRow(ctx, `SELECT COALESCE(credits,0) FROM users WHERE id=$1`, userID).Scan(&beforeCredits); err != nil {
		t.Fatalf("读初始余额: %v", err)
	}

	store := NewPGStore()
	mgr := NewManager(store)
	defer mgr.Close()

	// ── 下单：credits/金额以目录为准，订单带上 package_code ──
	p := NewPayment(userID, ChannelAlipay, 5000, 9900, "CNY")
	p.PackageCode = pkgCode
	if err := store.CreatePayment(ctx, p); err != nil {
		t.Fatalf("建单: %v", err)
	}

	before := time.Now()
	got, credited, err := mgr.ConfirmPayment(ctx, p.ID, "trade_pkg_probe")
	if err != nil {
		t.Fatalf("确认支付: %v", err)
	}
	if !credited {
		t.Fatal("首次确认应当入账")
	}
	if got.PackageCode != pkgCode {
		t.Errorf("订单应回读 package_code=%s，实际 %q", pkgCode, got.PackageCode)
	}

	// ── ① credits 到位 ──
	var afterCredits int
	if err := pool.QueryRow(ctx, `SELECT COALESCE(credits,0) FROM users WHERE id=$1`, userID).Scan(&afterCredits); err != nil {
		t.Fatalf("读余额: %v", err)
	}
	if afterCredits != beforeCredits+5000 {
		t.Errorf("credits 应增加 5000（%d → %d），实际 %d", beforeCredits, beforeCredits+5000, afterCredits)
	}

	// ── ② 权益到位（且是快照）──
	acts, err := store.ActiveEntitlements(ctx, userID, time.Now())
	if err != nil {
		t.Fatalf("查权益: %v", err)
	}
	if len(acts) != 1 {
		t.Fatalf("应有 1 条生效权益，实际 %d", len(acts))
	}
	e := acts[0]
	if e.PackageCode != pkgCode {
		t.Errorf("权益套餐 code 应为 %s，实际 %s", pkgCode, e.PackageCode)
	}
	if e.PackageName != "测试入门包" {
		t.Errorf("权益应存套餐名快照，实际 %q", e.PackageName)
	}
	if len(e.AllowedModels) != 2 {
		t.Errorf("权益应存模型快照 2 个，实际 %v", e.AllowedModels)
	}
	// 有效期从支付成功起算（约 now + 30 天），不是自然月末。
	wantExpiry := e.StartsAt.AddDate(0, 0, 30)
	if d := e.ExpiresAt.Sub(wantExpiry); d > time.Minute || d < -time.Minute {
		t.Errorf("expires_at 应为 starts_at + 30 天，实际差 %v", d)
	}
	if e.StartsAt.Before(before.Add(-time.Minute)) {
		t.Errorf("starts_at 不应早于支付时刻，实际 %v < %v", e.StartsAt, before)
	}

	// ── ③ 重复确认幂等：credits 不再加、权益不再发 ──
	_, credited2, err := mgr.ConfirmPayment(ctx, p.ID, "trade_pkg_probe")
	if err != nil {
		t.Fatalf("重复确认: %v", err)
	}
	if credited2 {
		t.Error("重复确认不应再次入账")
	}
	var againCredits int
	if err := pool.QueryRow(ctx, `SELECT COALESCE(credits,0) FROM users WHERE id=$1`, userID).Scan(&againCredits); err != nil {
		t.Fatalf("读余额: %v", err)
	}
	if againCredits != afterCredits {
		t.Errorf("重复确认不应改变余额：%d → %d", afterCredits, againCredits)
	}
	var entCount int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM user_entitlements WHERE user_id=$1`, userID).Scan(&entCount); err != nil {
		t.Fatalf("数权益: %v", err)
	}
	if entCount != 1 {
		t.Errorf("重复确认不应重复发放权益，实际 %d 条", entCount)
	}

	// ── ④ 过期即失效（无定时任务，纯时间判定）──
	//
	// 必须把整个窗口挪到过去，而不是只改 expires_at：表上有
	// CHECK (expires_at > starts_at)，单独把 expires_at 设成早于 starts_at 会被约束拦下
	// （23514）。这条约束本身就是防"窗口倒挂"的，测试不能绕过它。
	if _, err := pool.Exec(ctx,
		`UPDATE user_entitlements
		    SET starts_at  = now() - interval '31 days',
		        expires_at = now() - interval '1 second'
		  WHERE user_id = $1`,
		userID); err != nil {
		t.Fatalf("改过期时间: %v", err)
	}
	expired, err := store.ActiveEntitlements(ctx, userID, time.Now())
	if err != nil {
		t.Fatalf("查过期权益: %v", err)
	}
	if len(expired) != 0 {
		t.Errorf("过期后不应再有生效权益，实际 %d 条", len(expired))
	}
	// 行本身仍在：过期是"失效"而不是"删除"，历史与审计需要它。
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM user_entitlements WHERE user_id=$1`, userID).Scan(&entCount); err != nil {
		t.Fatalf("数权益: %v", err)
	}
	if entCount != 1 {
		t.Errorf("过期后记录应保留 1 条，实际 %d", entCount)
	}
}

// 权益发放的幂等键是 payment_id：同一支付单重复发放必须被唯一索引拦下，
// 而不是插出第二条。这是"重复回调不重复发权益"的数据库级保证。
func TestLiveGrantEntitlementIsIdempotent(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	pool := setupLivePool(t, ctx, dsn)

	userID, err := id.UUID()
	if err != nil {
		t.Fatalf("生成用户 ID: %v", err)
	}
	payID := "pay_probe_" + id.NextID()
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM user_entitlements WHERE user_id = $1`, userID)
	})

	store := NewPGStore()
	now := time.Now()
	mk := func() *Entitlement {
		entID, e := id.UUID()
		if e != nil {
			t.Fatalf("生成权益 ID: %v", e)
		}
		return &Entitlement{
			ID:            entID,
			UserID:        userID,
			PackageCode:   "test_pro",
			PackageName:   "测试专业包",
			AllowedModels: []string{"gpt-4o"},
			PaymentID:     payID,
			StartsAt:      now,
			ExpiresAt:     now.AddDate(0, 0, 30),
		}
	}

	granted, err := store.GrantEntitlement(ctx, mk())
	if err != nil {
		t.Fatalf("首次发放: %v", err)
	}
	if !granted {
		t.Error("首次发放应返回 granted=true")
	}

	// 第二次（不同 id、同一 payment_id）必须被唯一索引拦下并翻译成 granted=false。
	granted2, err := store.GrantEntitlement(ctx, mk())
	if err != nil {
		t.Fatalf("重复发放不应报错（应翻译为已发放）: %v", err)
	}
	if granted2 {
		t.Error("同一支付单重复发放应返回 granted=false")
	}

	var n int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM user_entitlements WHERE payment_id=$1`, payID).Scan(&n); err != nil {
		t.Fatalf("数权益: %v", err)
	}
	if n != 1 {
		t.Errorf("同一支付单只应有 1 条权益，实际 %d", n)
	}
}
