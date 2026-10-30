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

// 真库测试：内存替身证明不了这四处，必须打真 PostgreSQL。
//
//  1. `ON CONFLICT (user_id, dedupe_key) DO NOTHING` 是否真的命中
//     `ux_notifications_user_dedupe`（索引名/列序写错时 SQL 会直接报错）；
//  2. `COALESCE((SELECT ...), TRUE)` 的"缺行即默认开启"语义；
//  3. MarkRead 的 `user_id` 条件是否真的挡住跨用户改状态；
//  4. 部分索引 `WHERE read_at IS NULL` 与 UnreadCount 的口径是否一致。
func setupNotifyPool(t *testing.T, ctx context.Context, dsn string) *pgxpool.Pool {
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

// seedUser 建一个临时租户 + 用户。通知表对两者都有外键，必须先备齐。
func seedUser(t *testing.T, ctx context.Context, pool *pgxpool.Pool, name string) (int16, string) {
	t.Helper()
	var tenantID int16
	if err := pool.QueryRow(ctx,
		`INSERT INTO tenants (name, status) VALUES ($1, 'active') RETURNING id`,
		"notify-test-"+id.NextID()).Scan(&tenantID); err != nil {
		t.Fatalf("建租户: %v", err)
	}
	userID, err := id.UUID()
	if err != nil {
		t.Fatalf("生成用户 ID: %v", err)
	}
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role) VALUES ($1, $2, $3, 'user')`,
		userID, tenantID, name); err != nil {
		t.Fatalf("建用户: %v", err)
	}
	return tenantID, userID
}

func TestLiveNotifyStore(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	pool := setupNotifyPool(t, ctx, dsn)
	store := NewPGStore()

	tenantID, userID := seedUser(t, ctx, pool, "notify-owner")
	_, otherUserID := seedUser(t, ctx, pool, "notify-other")

	t.Cleanup(func() {
		bg := context.Background()
		// 逆序删：通知与偏好先走，再删用户/租户（外键方向如此）。
		for _, uid := range []string{userID, otherUserID} {
			if _, e := pool.Exec(bg, `DELETE FROM notifications WHERE user_id = $1`, uid); e != nil {
				t.Logf("清理通知失败: %v", e)
			}
			if _, e := pool.Exec(bg, `DELETE FROM user_notification_prefs WHERE user_id = $1`, uid); e != nil {
				t.Logf("清理偏好失败: %v", e)
			}
			if _, e := pool.Exec(bg, `DELETE FROM users WHERE id = $1`, uid); e != nil {
				t.Logf("清理用户失败: %v", e)
			}
		}
		if _, e := pool.Exec(bg, `DELETE FROM tenants WHERE id = $1`, tenantID); e != nil {
			t.Logf("清理租户失败: %v", e)
		}
	})

	base := func(key string) *Notification {
		return &Notification{
			TenantID:  tenantID,
			UserID:    userID,
			Kind:      KindPaymentSucceeded,
			DedupeKey: key,
			Title:     "支付成功",
			Body:      "已到账 1000 credits",
			Link:      "/billing",
			Payload:   map[string]any{"payment_id": "pay_1"},
		}
	}

	// ── Insert：首次写入成功，重复键返回 (false, nil) 而不是错误 ──
	first := base("payment_succeeded:pay_1")
	inserted, err := store.Insert(ctx, first)
	if err != nil {
		t.Fatalf("Insert: %v", err)
	}
	if !inserted {
		t.Fatal("首次 Insert 应返回 inserted=true")
	}
	if first.ID == "" {
		t.Fatal("Insert 后 ID 仍为空 —— 调用方拿不到主键")
	}

	dup := base("payment_succeeded:pay_1")
	inserted, err = store.Insert(ctx, dup)
	if err != nil {
		t.Fatalf("重复 Insert 不应报错，实际: %v", err)
	}
	if inserted {
		t.Fatal("重复键应返回 inserted=false（ON CONFLICT DO NOTHING 未生效）")
	}

	// 同一 dedupe_key 换个人必须能写入 —— 去重键带 user_id。
	other := base("payment_succeeded:pay_1")
	other.UserID = otherUserID
	inserted, err = store.Insert(ctx, other)
	if err != nil {
		t.Fatalf("他人同键 Insert: %v", err)
	}
	if !inserted {
		t.Fatal("同一事件应能同时通知另一个用户")
	}

	// 再补两条，用于验证列表顺序与未读计数。
	for _, k := range []string{"payment_succeeded:pay_2", "payment_succeeded:pay_3"} {
		if _, err := store.Insert(ctx, base(k)); err != nil {
			t.Fatalf("Insert %s: %v", k, err)
		}
	}

	// ── List：时间倒序 ──
	list, err := store.List(ctx, userID, 10, false)
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(list) != 3 {
		t.Fatalf("应有 3 条通知，实际 %d", len(list))
	}
	for i := 1; i < len(list); i++ {
		if list[i-1].CreatedAt.Before(list[i].CreatedAt) {
			t.Fatalf("列表未按时间倒序: [%d]=%v < [%d]=%v",
				i-1, list[i-1].CreatedAt, i, list[i].CreatedAt)
		}
	}
	// payload jsonb 必须能扫描回来（否则前端拿不到 payment_id 等细节）。
	var sawPayload bool
	for _, n := range list {
		if n.Payload != nil && n.Payload["payment_id"] == "pay_1" {
			sawPayload = true
		}
	}
	if !sawPayload {
		t.Fatal("payload jsonb 未正确回读")
	}
	// link / title 也要落库正确。
	if list[0].Title != "支付成功" || list[0].Link != "/billing" {
		t.Fatalf("字段回读不符: title=%q link=%q", list[0].Title, list[0].Link)
	}

	// ── UnreadCount ──
	n, err := store.UnreadCount(ctx, userID)
	if err != nil {
		t.Fatalf("UnreadCount: %v", err)
	}
	if n != 3 {
		t.Fatalf("未读数应为 3，实际 %d", n)
	}
	// 别人的未读不能算进我的角标。
	if nOther, err := store.UnreadCount(ctx, otherUserID); err != nil || nOther != 1 {
		t.Fatalf("他人未读数应为 1，实际 %d (err=%v)", nOther, err)
	}

	// ── MarkRead：跨用户必须被挡住 ──
	target := list[0].ID
	affected, err := store.MarkRead(ctx, otherUserID, []string{target})
	if err != nil {
		t.Fatalf("MarkRead: %v", err)
	}
	if affected != 0 {
		t.Fatalf("他人通知不应被改成已读，实际影响 %d 行", affected)
	}

	affected, err = store.MarkRead(ctx, userID, []string{target})
	if err != nil {
		t.Fatalf("MarkRead: %v", err)
	}
	if affected != 1 {
		t.Fatalf("应影响 1 行，实际 %d", affected)
	}
	// 重复标记不重复计数（read_at IS NULL 条件）。
	affected, err = store.MarkRead(ctx, userID, []string{target})
	if err != nil {
		t.Fatalf("重复 MarkRead: %v", err)
	}
	if affected != 0 {
		t.Fatalf("重复标记应影响 0 行，实际 %d", affected)
	}

	// 未读数随之下降；unreadOnly 列表也少一条。
	if n, _ := store.UnreadCount(ctx, userID); n != 2 {
		t.Fatalf("标读一条后未读应为 2，实际 %d", n)
	}
	unreadOnly, err := store.List(ctx, userID, 10, true)
	if err != nil {
		t.Fatalf("List unreadOnly: %v", err)
	}
	if len(unreadOnly) != 2 {
		t.Fatalf("未读列表应为 2 条，实际 %d", len(unreadOnly))
	}

	// ── MarkAllRead ──
	affected, err = store.MarkAllRead(ctx, userID)
	if err != nil {
		t.Fatalf("MarkAllRead: %v", err)
	}
	if affected != 2 {
		t.Fatalf("应把剩余 2 条标读，实际 %d", affected)
	}
	if n, _ := store.UnreadCount(ctx, userID); n != 0 {
		t.Fatalf("全标读后未读应为 0，实际 %d", n)
	}

	// ── 偏好：缺行即默认开启 ──
	enabled, err := store.EmailEnabled(ctx, userID, KindEntitlementExpiring)
	if err != nil {
		t.Fatalf("EmailEnabled: %v", err)
	}
	if !enabled {
		t.Fatal("未设置过的事件类型应默认开启邮件")
	}

	// 显式关掉后必须读到 false。
	if err := store.SetEmailEnabled(ctx, userID, KindEntitlementExpiring, false); err != nil {
		t.Fatalf("SetEmailEnabled: %v", err)
	}
	enabled, err = store.EmailEnabled(ctx, userID, KindEntitlementExpiring)
	if err != nil {
		t.Fatalf("EmailEnabled: %v", err)
	}
	if enabled {
		t.Fatal("显式关闭后应为 false")
	}

	// upsert 覆盖：再开回来。
	if err := store.SetEmailEnabled(ctx, userID, KindEntitlementExpiring, true); err != nil {
		t.Fatalf("SetEmailEnabled 覆盖: %v", err)
	}
	enabled, _ = store.EmailEnabled(ctx, userID, KindEntitlementExpiring)
	if !enabled {
		t.Fatal("upsert 覆盖后应回到 true")
	}
	// 覆盖不能产生重复行（主键 (user_id, kind) 生效）。
	var prefRows int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM user_notification_prefs WHERE user_id = $1`, userID).Scan(&prefRows); err != nil {
		t.Fatalf("查偏好行数: %v", err)
	}
	if prefRows != 1 {
		t.Fatalf("upsert 后应只有 1 行偏好，实际 %d", prefRows)
	}

	// ListPrefs 只反映显式设置过的键（默认值由 Manager.Prefs 补）。
	prefs, err := store.ListPrefs(ctx, userID)
	if err != nil {
		t.Fatalf("ListPrefs: %v", err)
	}
	if len(prefs) != 1 || prefs[KindEntitlementExpiring] != true {
		t.Fatalf("ListPrefs 应只含显式设置过的 1 项，实际 %v", prefs)
	}
}
