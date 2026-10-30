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

// UserLocale 的 SQL 是 T1 新增的，且踩两个只有真库能暴露的坑：
//   - `users.settings` 列类型是 **json**（不是 jsonb），`->>` 必须显式 `::jsonb`，
//     否则某些 PG 版本报 "operator does not exist: json ->> unknown"；
//   - 列可为 NULL，未设置时要返回空串而不是报错。
func TestLiveUserLocale(t *testing.T) {
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
		"locale-test-"+id.NextID()).Scan(&tenantID); err != nil {
		t.Fatalf("建租户: %v", err)
	}
	userID, err := id.UUID()
	if err != nil {
		t.Fatalf("生成用户 ID: %v", err)
	}
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role) VALUES ($1, $2, $3, 'user')`,
		userID, tenantID, "locale-owner"); err != nil {
		t.Fatalf("建用户: %v", err)
	}
	t.Cleanup(func() {
		bg := context.Background()
		if _, e := pool.Exec(bg, `DELETE FROM users WHERE id = $1`, userID); e != nil {
			t.Logf("清理用户失败: %v", e)
		}
		if _, e := pool.Exec(bg, `DELETE FROM tenants WHERE id = $1`, tenantID); e != nil {
			t.Logf("清理租户失败: %v", e)
		}
	})

	store := NewPGStore()

	// ── settings 为 NULL：返回空串，不是错误 ──
	loc, err := store.UserLocale(ctx, userID)
	if err != nil {
		t.Fatalf("settings 为 NULL 时 UserLocale 不应报错: %v", err)
	}
	if loc != "" {
		t.Fatalf("settings 为 NULL 时应返回空串，实际 %q", loc)
	}

	// ── 写入 locale（走 UpdateProfile 同款 `||` 局部合并）──
	if _, err := pool.Exec(ctx,
		`UPDATE users SET settings = COALESCE(settings::jsonb, '{}'::jsonb) || $1::jsonb WHERE id = $2`,
		`{"locale":"en-US"}`, userID); err != nil {
		t.Fatalf("写入 locale: %v", err)
	}
	loc, err = store.UserLocale(ctx, userID)
	if err != nil {
		t.Fatalf("UserLocale: %v", err)
	}
	if loc != "en-US" {
		t.Fatalf("应读到 en-US，实际 %q", loc)
	}

	// ── 局部合并不冲掉其它设置，且能一路读回 locale ──
	if _, err := pool.Exec(ctx,
		`UPDATE users SET settings = COALESCE(settings::jsonb, '{}'::jsonb) || $1::jsonb WHERE id = $2`,
		`{"tts":{"voice":"x"}}`, userID); err != nil {
		t.Fatalf("写入其它设置: %v", err)
	}
	loc, _ = store.UserLocale(ctx, userID)
	if loc != "en-US" {
		t.Fatalf("写入其它设置后 locale 应保持不变，实际 %q", loc)
	}
	var hasTTS bool
	if err := pool.QueryRow(ctx,
		`SELECT settings::jsonb ? 'tts' FROM users WHERE id = $1`, userID).Scan(&hasTTS); err != nil {
		t.Fatalf("查 tts: %v", err)
	}
	if !hasTTS {
		t.Fatal("tts 设置应保留")
	}

	// ── settings 里没有 locale 键（存在其它键）：返回空串 ──
	if _, err := pool.Exec(ctx,
		`UPDATE users SET settings = '{"theme":"dark"}'::json WHERE id = $1`, userID); err != nil {
		t.Fatalf("重置 settings: %v", err)
	}
	loc, err = store.UserLocale(ctx, userID)
	if err != nil {
		t.Fatalf("UserLocale: %v", err)
	}
	if loc != "" {
		t.Fatalf("无 locale 键时应返回空串，实际 %q", loc)
	}

	// ── 阿语值也能正确读出（避免某些字符集问题）──
	if _, err := pool.Exec(ctx,
		`UPDATE users SET settings = '{"locale":"ar"}'::json WHERE id = $1`, userID); err != nil {
		t.Fatalf("写入 ar: %v", err)
	}
	loc, _ = store.UserLocale(ctx, userID)
	if loc != "ar" {
		t.Fatalf("应读到 ar，实际 %q", loc)
	}
}
