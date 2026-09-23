package billing

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 入账失败时订单必须退回 pending —— 否则订单停在 paid，后续查询都会被幂等分支
// 跳过，这笔支付将永远不入账且无法自愈（线上表现：钱付了、余额不动）。
func TestLiveConfirmPaymentRevertsOnCreditFailure(t *testing.T) {
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
	defer pool.Close()

	db.PoolMu.Lock()
	db.Pool = pool
	db.PoolMu.Unlock()
	defer func() {
		db.PoolMu.Lock()
		db.Pool = nil
		db.PoolMu.Unlock()
	}()

	store := NewPGStore()
	mgr := NewManager(store)
	defer mgr.Close()

	// 故意指向一个不存在的用户：AddCredits 必然失败（UPDATE 影响 0 行），
	// 从而触发回退路径。payments.user_id 无外键，可以安全造这种订单。
	const ghostUser = "00000000-0000-0000-0000-0000000000ff"
	p := NewPayment(ghostUser, ChannelAlipay, 10000, 10000, "CNY")
	if err := store.CreatePayment(ctx, p); err != nil {
		t.Fatalf("建单: %v", err)
	}
	defer func() { _, _ = pool.Exec(context.Background(), `DELETE FROM payments WHERE id=$1`, p.ID) }()

	_, credited, err := mgr.ConfirmPayment(ctx, p.ID, "trade_probe")
	if err == nil {
		t.Fatal("对不存在的用户入账应当失败")
	}
	if credited {
		t.Fatal("credited 应为 false")
	}
	t.Logf("入账按预期失败: %v", err)

	after, err := store.GetPayment(ctx, p.ID)
	if err != nil {
		t.Fatalf("回读订单: %v", err)
	}
	if after.Status != PayStatusPending {
		t.Errorf("入账失败后订单必须退回 pending（否则永远无法自愈），实际 status=%s", after.Status)
	} else {
		t.Log("✅ 订单已退回 pending，下一次查询/回调可以重试入账")
	}
	if after.PaidAt != nil {
		t.Error("退回 pending 时应清空 paid_at")
	}

	// 顺带确认：失败时不应产生任何流水
	var txCount int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM credit_transactions WHERE user_id=$1`, ghostUser).Scan(&txCount); err != nil {
		t.Fatalf("查流水: %v", err)
	}
	if txCount != 0 {
		t.Errorf("入账失败不应留下流水，实际 %d 条", txCount)
	}
}
