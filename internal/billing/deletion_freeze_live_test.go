package billing

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/athenavi/chiron/internal/id"
)

// ── 注销冻结期的入账去向（真库）──
//
// 场景：用户申请注销（此时余额已按门槛要求清零），随后一笔**退款**到账。
//
// 若直接加到 credits，用户立刻又能消费 —— "清空资产"当场失效，
// 而账号仍处于 pending_deletion，语义自相矛盾。
// 正确去向是 credits_frozen：钱不丢（撤销时原样还回），但不可消费。
//
// 这条只有真库能验：那段逻辑写在单个 UPDATE 的 CASE 表达式里，
// 纯函数测不到 SQL 行为。
func TestLiveAddCreditsDuringPendingDeletionGoesToFrozen(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	pool := setupLivePool(t, ctx, dsn)
	store := NewPGStore()

	tenantID, err := newTestTenant(ctx, pool)
	if err != nil {
		t.Fatalf("建租户: %v", err)
	}
	userID, err := id.UUID()
	if err != nil {
		t.Fatalf("生成用户 ID: %v", err)
	}
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role) VALUES ($1, $2, $3, 'user')`,
		userID, tenantID, "freeze-credit"); err != nil {
		t.Fatalf("建用户: %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM credit_transactions WHERE user_id = $1`, userID)
		_, _ = pool.Exec(context.Background(), `DELETE FROM users WHERE id = $1`, userID)
	})

	// ① 冻结期：余额 0、状态 pending_deletion，来一笔 100 的退款。
	if _, err := pool.Exec(ctx,
		`UPDATE users SET credits = 0, credits_frozen = 0, account_status = 'pending_deletion'
		  WHERE id = $1`, userID); err != nil {
		t.Fatalf("设置冻结态: %v", err)
	}
	if _, err := store.AtomicAddBalance(ctx, userID, 100, "refund"); err != nil {
		t.Fatalf("冻结期入账失败: %v", err)
	}

	var credits, frozen int
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(credits, 0), COALESCE(credits_frozen, 0) FROM users WHERE id = $1`,
		userID).Scan(&credits, &frozen); err != nil {
		t.Fatalf("读回余额: %v", err)
	}
	if credits != 0 {
		t.Fatalf("冻结期入账不该变成可消费余额（credits=%d）：否则清空资产失效", credits)
	}
	if frozen != 100 {
		t.Fatalf("冻结期入账应进 credits_frozen（期望 100，得到 %d）：钱不能凭空消失", frozen)
	}

	// ② 撤销后（active）充值必须正常进 credits。
	if _, err := pool.Exec(ctx,
		`UPDATE users SET account_status = 'active', credits = 0, credits_frozen = NULL
		  WHERE id = $1`, userID); err != nil {
		t.Fatalf("恢复正常态: %v", err)
	}
	if _, err := store.AtomicAddBalance(ctx, userID, 50, "recharge"); err != nil {
		t.Fatalf("正常入账失败: %v", err)
	}
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(credits, 0) FROM users WHERE id = $1`, userID).Scan(&credits); err != nil {
		t.Fatalf("读回余额: %v", err)
	}
	if credits != 50 {
		t.Fatalf("active 账号入账应进 credits（期望 50，得到 %d）", credits)
	}
}

// newTestTenant 建一个临时租户并返回 id。
func newTestTenant(ctx context.Context, pool *pgxpool.Pool) (int16, error) {
	var tenantID int16
	err := pool.QueryRow(ctx,
		`INSERT INTO tenants (name, status) VALUES ($1, 'active') RETURNING id`,
		"deletion-freeze-"+id.NextID()).Scan(&tenantID)
	return tenantID, err
}
