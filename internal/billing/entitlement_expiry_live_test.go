package billing

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
)

// ExpiringEntitlements 的窗口 SQL 用了 `($2 || ' days')::interval` 这种拼接，
// 以及 `starts_at <= now < expires_at <= now+window` 三重边界 —— 这些只有真库
// 能证明，内存替身把它们全绕过了。
func TestLiveExpiringEntitlements(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

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

	var tenantID int16
	if err := pool.QueryRow(ctx,
		`INSERT INTO tenants (name, status) VALUES ($1, 'active') RETURNING id`,
		"expiry-test-"+id.NextID()).Scan(&tenantID); err != nil {
		t.Fatalf("建租户: %v", err)
	}
	userID, err := id.UUID()
	if err != nil {
		t.Fatalf("生成用户 ID: %v", err)
	}
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role) VALUES ($1, $2, $3, 'user')`,
		userID, tenantID, "expiry-owner"); err != nil {
		t.Fatalf("建用户: %v", err)
	}

	now := time.Now()
	// 五份权益覆盖全部边界：窗口内 3 天档、1 天档、窗口内非档位、窗口外、已过期。
	type seed struct {
		label  string
		offset time.Duration // expires_at = now + offset
		start  time.Duration // starts_at = now + start
	}
	seeds := []seed{
		{"in-3d", 72 * time.Hour, -time.Hour},
		{"in-1d", 24 * time.Hour, -time.Hour},
		{"in-2.5d", 60 * time.Hour, -time.Hour},
		{"out-of-window", 10 * 24 * time.Hour, -time.Hour},
		{"expired", -time.Hour, -48 * time.Hour},
		{"not-started", 48 * time.Hour, time.Hour}, // starts_at 在未来
	}
	ids := map[string]string{}
	for _, s := range seeds {
		eid, err := id.UUID()
		if err != nil {
			t.Fatalf("生成权益 ID: %v", err)
		}
		ids[s.label] = eid
		if _, err := pool.Exec(ctx,
			`INSERT INTO user_entitlements
			     (id, user_id, package_code, package_name, allowed_models,
			      payment_id, starts_at, expires_at)
			 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
			eid, userID, "test_pkg_"+s.label, "测试包 "+s.label,
			[]string{"gpt-4o-mini"}, "pay_test_"+eid,
			now.Add(s.start), now.Add(s.offset)); err != nil {
			t.Fatalf("建权益 %s: %v", s.label, err)
		}
	}

	t.Cleanup(func() {
		bg := context.Background()
		if _, e := pool.Exec(bg, `DELETE FROM user_entitlements WHERE user_id = $1`, userID); e != nil {
			t.Logf("清理权益失败: %v", e)
		}
		if _, e := pool.Exec(bg, `DELETE FROM users WHERE id = $1`, userID); e != nil {
			t.Logf("清理用户失败: %v", e)
		}
		if _, e := pool.Exec(bg, `DELETE FROM tenants WHERE id = $1`, tenantID); e != nil {
			t.Logf("清理租户失败: %v", e)
		}
	})

	store := NewPGStore()
	got, err := store.ExpiringEntitlements(ctx, now, expiryScanWindowDays)
	if err != nil {
		t.Fatalf("ExpiringEntitlements: %v", err)
	}

	// 允许同一库里其它测试的残留，只断言本次种子里的成员关系。
	found := map[string]bool{}
	for _, e := range got {
		found[e.PackageCode] = true
		// 窗口内结果必须满足：已生效、未过期。
		if e.StartsAt.After(now) {
			t.Errorf("%s 尚未生效却被返回（starts_at=%v）", e.PackageCode, e.StartsAt)
		}
		if !e.ExpiresAt.After(now) {
			t.Errorf("%s 已过期/刚好到期却被返回（expires_at=%v）", e.PackageCode, e.ExpiresAt)
		}
	}

	want := []string{"test_pkg_in-3d", "test_pkg_in-1d", "test_pkg_in-2.5d"}
	for _, w := range want {
		if !found[w] {
			t.Errorf("窗口内权益 %s 未被返回", w)
		}
	}
	notWant := []string{
		"test_pkg_out-of-window", // 10 天后到期，超出 3 天窗口
		"test_pkg_expired",       // 已过期
		"test_pkg_not-started",   // 尚未生效
	}
	for _, w := range notWant {
		if found[w] {
			t.Errorf("不应返回 %s", w)
		}
	}
}
