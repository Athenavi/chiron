package api

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 审计保留清理的核心验收（M3）：过期行被删、未过期行保留、分批进行。
//
// 为什么必须在真库上跑：`created_at` 是 `timestamp without time zone`，
// 而 cutoff 是 UTC 时刻 —— 两者的比较依赖 `::timestamptz` 这个 cast 与
// 会话时区。纯函数测试看不到这一层，而"删多了"或"一条都不删"正是这里
// 最容易出的错。
func TestLiveSweepAuditRetentionDeletesOnlyExpired(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置：跳过真实 PostgreSQL 用例")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("连接 PostgreSQL 失败: %v", err)
	}
	t.Cleanup(pool.Close)

	// 挂到全局管理器：SweepAuditRetention 走 db.GlobalDBManager。
	db.PoolMu.Lock()
	prevPool := db.Pool
	db.Pool = pool
	db.PoolMu.Unlock()
	t.Cleanup(func() {
		db.PoolMu.Lock()
		db.Pool = prevPool
		db.PoolMu.Unlock()
	})

	var tenantID int16
	if err := pool.QueryRow(ctx, `SELECT id FROM tenants ORDER BY id LIMIT 1`).Scan(&tenantID); err != nil {
		t.Skipf("库里没有 tenant 可用（%v）：跳过审计保留用例", err)
	}

	// 前缀带纳秒（十六进制，比十进制短）：保证并发/重复执行时不与其他运行的行冲突。
	// audit_logs.id 是 varchar(36)，所以前缀必须足够短 —— 十进制纳秒 19 位会撞上限。
	prefix := fmt.Sprintf("pr%x-", time.Now().UnixNano())
	insert := func(suffix string, age time.Duration) string {
		id := prefix + suffix
		// user_id 留 NULL：audit_logs.user_id 有外键指向 users(id)，
		// 而系统事件（清理、定时任务）本来就没有用户。
		if _, err := pool.Exec(ctx,
			`INSERT INTO audit_logs (id, tenant_id, user_id, action, resource_type, details, created_at)
			 VALUES ($1, $2, NULL, 'probe.retention', 'probe', '{}'::json, now() - $3::interval)`,
			id, tenantID, age.String()); err != nil {
			t.Fatalf("插入探针行 %s: %v", id, err)
		}
		return id
	}

	expired1 := insert("expired-1", 400*24*time.Hour)
	expired2 := insert("expired-2", 300*24*time.Hour)
	alive := insert("alive", 10*24*time.Hour)

	t.Cleanup(func() {
		bg := context.Background()
		_, _ = pool.Exec(bg, `DELETE FROM audit_logs WHERE id LIKE $1`, prefix+"%")
	})

	// 保留 180 天：前两行（400/300 天前）应被删，第三行（10 天前）应保留。
	res, err := SweepAuditRetention(ctx, DefaultAuditRetentionDays)
	if err != nil {
		t.Fatalf("清理失败: %v", err)
	}
	if res.Cutoff.IsZero() {
		t.Fatal("Cutoff 必须被设置")
	}
	if res.Deleted < 2 {
		t.Errorf("至少应删除 2 行探针数据，实际 %d（batches=%d）", res.Deleted, res.Batches)
	}
	if res.Batches < 1 {
		t.Errorf("至少应执行 1 批，实际 %d", res.Batches)
	}

	for _, id := range []string{expired1, expired2} {
		var exists bool
		if err := pool.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM audit_logs WHERE id = $1)`, id).Scan(&exists); err != nil {
			t.Fatalf("查探针 %s: %v", id, err)
		}
		if exists {
			t.Errorf("过期行 %s 应被删除", id)
		}
	}

	var aliveExists bool
	if err := pool.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM audit_logs WHERE id = $1)`, alive).Scan(&aliveExists); err != nil {
		t.Fatalf("查存活行: %v", err)
	}
	if !aliveExists {
		t.Errorf("未过期行 %s 必须保留（清理误伤了新数据）", alive)
	}
}

// 保留天数 <= 0 必须回落默认值，而不是"删光"。
//
// 这是不可恢复风险的唯一防线：配置手滑写 0，若被解释成
// "删除 cutoff=now() 之前的全部记录"，等于一键清空审计。
func TestLiveSweepAuditRetentionZeroDaysFallsBackToDefault(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置：跳过真实 PostgreSQL 用例")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("连接 PostgreSQL 失败: %v", err)
	}
	t.Cleanup(pool.Close)

	db.PoolMu.Lock()
	prevPool := db.Pool
	db.Pool = pool
	db.PoolMu.Unlock()
	t.Cleanup(func() {
		db.PoolMu.Lock()
		db.Pool = prevPool
		db.PoolMu.Unlock()
	})

	var tenantID int16
	if err := pool.QueryRow(ctx, `SELECT id FROM tenants ORDER BY id LIMIT 1`).Scan(&tenantID); err != nil {
		t.Skipf("库里没有 tenant 可用（%v）：跳过用例", err)
	}

	// 同上：十六进制纳秒，避免超过 varchar(36)。
	prefix := fmt.Sprintf("pz%x-", time.Now().UnixNano())
	// 10 天前的行：如果 0 被当成"删光"，它会被误删。
	id := prefix + "recent"
	if _, err := pool.Exec(ctx,
		`INSERT INTO audit_logs (id, tenant_id, user_id, action, resource_type, details, created_at)
		 VALUES ($1, $2, NULL, 'probe.retention', 'probe', '{}'::json, now() - interval '10 days')`,
		id, tenantID); err != nil {
		t.Fatalf("插入探针行: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM audit_logs WHERE id LIKE $1`, prefix+"%")
	})

	res, err := SweepAuditRetention(ctx, 0)
	if err != nil {
		t.Fatalf("清理失败: %v", err)
	}
	// Cutoff 必须按默认 180 天算，而不是 now()。
	wantCutoff := time.Now().AddDate(0, 0, -DefaultAuditRetentionDays)
	if diff := res.Cutoff.Sub(wantCutoff); diff > time.Minute || diff < -time.Minute {
		t.Fatalf("Cutoff = %s, want ≈ %s（0 必须回落默认 %d 天，不能是 now()）",
			res.Cutoff, wantCutoff, DefaultAuditRetentionDays)
	}

	var exists bool
	if err := pool.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM audit_logs WHERE id = $1)`, id).Scan(&exists); err != nil {
		t.Fatalf("查探针: %v", err)
	}
	if !exists {
		t.Error("保留天数传 0 时不得删除近期记录（应回落默认值，绝不解释成'删光'）")
	}
}
