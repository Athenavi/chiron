package billing

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 临时端到端验证：对真实库检查 payments.user_id 长度自检与下单落库路径。
// 该测试对 schema 状态自适应：未修复时应复现 value too long，修复后应写入成功。
func TestLivePaymentSchemaAndCreate(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
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

	// 1) 启动自检必须给出可执行的修复语句
	issue := store.paymentUserIDIssue(ctx)
	t.Logf("自检: %q", issue)
	if issue != "" && !strings.Contains(issue, "ALTER TABLE payments ALTER COLUMN user_id TYPE VARCHAR(36)") {
		t.Errorf("诊断未包含可执行修复语句: %q", issue)
	}

	// 2) 下单落库（36 字符 UUID）
	const uid = "f71e89c1-b275-4170-bde9-31441bb93426"
	p := NewPayment(uid, ChannelAlipay, 1000, 1000, "CNY")
	err = store.CreatePayment(ctx, p)

	if issue != "" {
		// schema 仍是 VARCHAR(32)：必须复现用户报的故障
		if err == nil {
			_, _ = pool.Exec(context.Background(), `DELETE FROM payments WHERE id=$1`, p.ID)
			t.Fatal("user_id 列过短却写入成功？")
		}
		t.Logf("复现预期故障: %v", err)
		if !strings.Contains(err.Error(), "value too long") {
			t.Errorf("失败原因不是列长度问题: %v", err)
		}
		return
	}

	if err != nil {
		t.Fatalf("下单落库失败: %v", err)
	}
	defer func() { _, _ = pool.Exec(context.Background(), `DELETE FROM payments WHERE id=$1`, p.ID) }()

	got, err := store.GetPayment(ctx, p.ID)
	if err != nil {
		t.Fatalf("回读订单失败: %v", err)
	}
	if got.UserID != uid {
		t.Errorf("回读 user_id = %q, want %q", got.UserID, uid)
	}
	if got.Status != PayStatusPending {
		t.Errorf("回读 status = %q", got.Status)
	}
}
