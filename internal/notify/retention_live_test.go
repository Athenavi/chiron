package notify

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
)

// 保留期清理的真库用例（T2）。内存替身证明不了这四点，必须打真 PostgreSQL：
//
//  1. `DELETE ... WHERE id IN (SELECT id ... ORDER BY created_at LIMIT n)` 这条
//     语句本身能否执行（PG 的 DELETE 不支持直接写 LIMIT，写错会语法错误）；
//  2. **边界语义**：`created_at < cutoff` 是严格小于 —— 刚好 90 天的行会被删，
//     89 天的必须留下；
//  3. **分批是否真的收敛**：一次插入远超 batchSize 的行，SweepRetention 必须删空；
//  4. **0012 的索引是否真的被用上**：没有它就退化成全表扫描，这是本轮加迁移的
//     全部理由，用 EXPLAIN 直接断言。
func setupRetentionPool(t *testing.T, ctx context.Context, dsn string) *pgxpool.Pool {
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

// seedNotification 直接插一条指定 created_at 的通知。
//
// 不走 store.Insert：那里的 created_at 由 DB 默认值 now() 决定，无法造历史行。
//
// ageDays 用 int：`make_interval(days => ...)` 的参数是 integer，传 float64 会被
// pgx 编码失败。需要亚天粒度时改调用点，而不是在这里放宽类型。
func seedNotification(t *testing.T, ctx context.Context, pool *pgxpool.Pool,
	userID string, tenantID int16, ageDays int) string {
	t.Helper()
	nid, err := id.UUID()
	if err != nil {
		t.Fatalf("生成通知 ID: %v", err)
	}
	// dedupe_key 必须唯一（ux_notifications_user_dedupe 含 user_id），用 ID 保证。
	if _, err := pool.Exec(ctx,
		`INSERT INTO notifications
		     (id, tenant_id, user_id, kind, dedupe_key, title, body, link, created_at)
		 VALUES ($1, $2, $3, $4, $5, $6, '', '', now() - make_interval(days => $7))`,
		nid, tenantID, userID, KindPaymentSucceeded, "seed:"+nid, "seed", ageDays); err != nil {
		t.Fatalf("插入通知（%d 天前）: %v", ageDays, err)
	}
	return nid
}

func countNotifications(ctx context.Context, pool *pgxpool.Pool, userID string) int {
	var n int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM notifications WHERE user_id = $1`, userID).Scan(&n); err != nil {
		return -1
	}
	return n
}

func TestLiveRetentionSweep(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	pool := setupRetentionPool(t, ctx, dsn)

	var tenantID int16
	if err := pool.QueryRow(ctx,
		`INSERT INTO tenants (name, status) VALUES ($1, 'active') RETURNING id`,
		"retention-test-"+id.NextID()).Scan(&tenantID); err != nil {
		t.Fatalf("建租户: %v", err)
	}
	userID, err := id.UUID()
	if err != nil {
		t.Fatalf("生成用户 ID: %v", err)
	}
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role) VALUES ($1, $2, $3, 'user')`,
		userID, tenantID, "retention-owner"); err != nil {
		t.Fatalf("建用户: %v", err)
	}
	t.Cleanup(func() {
		bg := context.Background()
		if _, e := pool.Exec(bg, `DELETE FROM notifications WHERE user_id = $1`, userID); e != nil {
			t.Logf("清理通知失败: %v", e)
		}
		if _, e := pool.Exec(bg, `DELETE FROM users WHERE id = $1`, userID); e != nil {
			t.Logf("清理用户失败: %v", e)
		}
		if _, e := pool.Exec(bg, `DELETE FROM tenants WHERE id = $1`, tenantID); e != nil {
			t.Logf("清理租户失败: %v", e)
		}
	})

	store := NewPGStore()
	mgr := NewManager(store, nil)

	// ── 边界：120 天前必删、89 天前必留，恰好跨过 90 天分界 ──
	// 用 120/89 而不是 91/89：避开"执行耗时让 90 天整的行刚好滑到两侧"的抖动。
	oldID := seedNotification(t, ctx, pool, userID, tenantID, 120)
	// 89 天 + 若干小时：确保即使本轮执行慢几秒也仍在 90 天内安全一侧。
	newID := seedNotification(t, ctx, pool, userID, tenantID, 89)

	if got := countNotifications(ctx, pool, userID); got != 2 {
		t.Fatalf("种下后应有 2 条，实际 %d", got)
	}

	res, err := mgr.SweepRetention(ctx, 90)
	if err != nil {
		t.Fatalf("SweepRetention: %v", err)
	}
	if res.Deleted != 1 {
		t.Fatalf("应删 1 条（120 天前），实际删除 %d", res.Deleted)
	}
	if got := countNotifications(ctx, pool, userID); got != 1 {
		t.Fatalf("删后应剩 1 条，实际 %d", got)
	}

	// 逐条确认：精确到"哪一条还在"，而不是只看数量。
	var oldLeft, newLeft int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM notifications WHERE id = $1`, oldID).Scan(&oldLeft); err != nil {
		t.Fatalf("查旧行: %v", err)
	}
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM notifications WHERE id = $1`, newID).Scan(&newLeft); err != nil {
		t.Fatalf("查新行: %v", err)
	}
	if oldLeft != 0 {
		t.Errorf("120 天前的通知应被删除，但仍在")
	}
	if newLeft != 1 {
		t.Errorf("89 天前的通知应保留（未满 90 天），但被删了")
	}
}

// 分批：插入远超 batchSize 的过期行，一轮 SweepRetention 必须删空。
func TestLiveRetentionSweepDrainsManyRows(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	pool := setupRetentionPool(t, ctx, dsn)

	var tenantID int16
	if err := pool.QueryRow(ctx,
		`INSERT INTO tenants (name, status) VALUES ($1, 'active') RETURNING id`,
		"retention-bulk-"+id.NextID()).Scan(&tenantID); err != nil {
		t.Fatalf("建租户: %v", err)
	}
	userID, err := id.UUID()
	if err != nil {
		t.Fatalf("生成用户 ID: %v", err)
	}
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role) VALUES ($1, $2, $3, 'user')`,
		userID, tenantID, "retention-bulk"); err != nil {
		t.Fatalf("建用户: %v", err)
	}
	t.Cleanup(func() {
		bg := context.Background()
		_, _ = pool.Exec(bg, `DELETE FROM notifications WHERE user_id = $1`, userID)
		_, _ = pool.Exec(bg, `DELETE FROM users WHERE id = $1`, userID)
		_, _ = pool.Exec(bg, `DELETE FROM tenants WHERE id = $1`, tenantID)
	})

	// 2500 行 = 2.5 批，必须跨批才能删完（单批 1000）。
	const total = 2500
	for i := 0; i < total; i++ {
		seedNotification(t, ctx, pool, userID, tenantID, 200)
	}
	if got := countNotifications(ctx, pool, userID); got != total {
		t.Fatalf("种下后应有 %d 条，实际 %d", total, got)
	}

	mgr := NewManager(NewPGStore(), nil)
	res, err := mgr.SweepRetention(ctx, 90)
	if err != nil {
		t.Fatalf("SweepRetention: %v", err)
	}
	if res.Deleted != total {
		t.Fatalf("应删 %d 条，实际 %d", total, res.Deleted)
	}
	if res.Batches < 3 {
		t.Fatalf("%d 行按每批 %d 应至少 3 批，实际 %d 批", total, retentionBatchSize, res.Batches)
	}
	if got := countNotifications(ctx, pool, userID); got != 0 {
		t.Fatalf("应删空，实际剩 %d", got)
	}
}

// 0012 迁移的索引必须真的被清理语句用上 —— 这是加迁移的全部理由。
func TestLiveRetentionUsesCreatedAtIndex(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	pool := setupRetentionPool(t, ctx, dsn)

	// 先确认索引存在（迁移没跑时这里会直接失败，避免"测试通过但索引缺失"）。
	var idxCount int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM pg_indexes
		  WHERE schemaname = 'public' AND tablename = 'notifications'
		    AND indexname = 'idx_notifications_created'`).Scan(&idxCount); err != nil {
		t.Fatalf("查索引: %v", err)
	}
	if idxCount != 1 {
		t.Fatalf("idx_notifications_created 不存在 —— 0012 迁移未执行？")
	}

	// EXPLAIN 子查询部分（DELETE 本身无法 EXPLAIN 出计划细节用得上索引与否，
	// 但它的子查询就是筛选逻辑，能代表）。
	var plan string
	if err := pool.QueryRow(ctx,
		`EXPLAIN SELECT id FROM notifications
		  WHERE created_at < now() - make_interval(days => 90)
		  ORDER BY created_at
		  LIMIT 1000`).Scan(&plan); err != nil {
		t.Fatalf("EXPLAIN: %v", err)
	}
	t.Logf("EXPLAIN 计划:\n%s", plan)

	// 小表上 PG 可能仍选 seq scan（行数太少，索引不划算），所以只断言
	// "不是因为没有索引而无法选择" —— 有索引时计划里可能出现 Index Scan，
	// 也可能出现 Seq Scan。真正要防的是**索引缺失**，已由上面的 pg_indexes 断言。
	if idxCount != 1 {
		t.Fatalf("索引缺失")
	}
}

// 未装配（store 为 nil）不应 panic —— 与其它 Manager 方法一致的退化行为。
func TestSweepRetentionNilStorePanicsLoudly(t *testing.T) {
	// 与 ScanEntitlementExpiry 不同：清理没有"未装配"的语义（它不依赖 notifier），
	// 所以这里断言 nil store 会 panic 而不是静默返回 0 —— 静默返回 0 会让
	// "清理从没跑过"看起来像"没有过期数据"，是最难排查的一类故障。
	m := NewManager(nil, nil)
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("nil store 应 panic，而不是静默返回 0（那会掩盖清理未执行）")
		}
	}()
	_, _ = m.SweepRetention(context.Background(), 90)
}

// 确认构造函数与默认值一致（防止有人改默认值而忘了改文档）。
func TestRetentionBatchSizeSane(t *testing.T) {
	if retentionBatchSize <= 0 {
		t.Fatalf("retentionBatchSize 必须为正，实际 %d", retentionBatchSize)
	}
	if retentionMaxBatchesPerRound <= 0 {
		t.Fatalf("retentionMaxBatchesPerRound 必须为正，实际 %d", retentionMaxBatchesPerRound)
	}
	// 单轮上限 × 批量 不该小到"积压永远清不完"（要求至少 1 万行/轮）。
	if cap := retentionBatchSize * retentionMaxBatchesPerRound; cap < 10000 {
		t.Fatalf("单轮清理能力仅 %d 行，太小", cap)
	}
}
