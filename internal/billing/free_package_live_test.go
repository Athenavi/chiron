package billing

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/id"
)

// 0 元且 0 额度的试用包（credits=0）必须能正常发放权益。
//
// 回归背景：`ConfirmPayment` 原先无条件调 `AddCredits`，而它拒绝 `amount <= 0`，
// 于是这类订单会被判定"入账失败"并退回 pending —— 免费包永远发不出去，
// 用户每次点击都看到支付失败，且没有任何线索。
//
// 本测试钉住：credits=0 时**跳过入账但照发权益**，订单落到 paid。
func TestLiveFreePackageGrantsEntitlementWithoutCredits(t *testing.T) {
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
		"free-pkg-"+id.NextID()).Scan(&tenantID); err != nil {
		t.Fatalf("建租户: %v", err)
	}
	userID, err := id.UUID()
	if err != nil {
		t.Fatalf("生成用户 ID: %v", err)
	}
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role) VALUES ($1, $2, $3, 'user')`,
		userID, tenantID, "free-claimer"); err != nil {
		t.Fatalf("建用户: %v", err)
	}

	pkgID, err := id.UUID()
	if err != nil {
		t.Fatalf("生成套餐 ID: %v", err)
	}
	// code 带随机后缀：上一次失败的运行可能残留同 code 的行。
	pkgCode := "test_free_" + id.NextID()
	if _, err := pool.Exec(ctx,
		`INSERT INTO billing_packages
		     (id, code, name, credits, price_cents, currency, duration_days, allowed_models)
		 VALUES ($1, $2, $3, 0, 0, 'CNY', 14, $4)`,
		pkgID, pkgCode, "测试免费包", []string{"gpt-4o-mini"}); err != nil {
		t.Fatalf("建套餐: %v", err)
	}

	t.Cleanup(func() {
		bg := context.Background()
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
	if err := pool.QueryRow(ctx, `SELECT COALESCE(credits,0) FROM users WHERE id=$1`, userID).
		Scan(&beforeCredits); err != nil {
		t.Fatalf("读初始余额: %v", err)
	}

	store := NewPGStore()
	mgr := NewManager(store)
	defer mgr.Close()

	p := NewPayment(userID, ChannelFree, 0, 0, "CNY")
	p.PackageCode = pkgCode
	if err := store.CreatePayment(ctx, p); err != nil {
		t.Fatalf("建单: %v", err)
	}

	got, credited, err := mgr.ConfirmPayment(ctx, p.ID, "free_"+p.ID)
	if err != nil {
		t.Fatalf("0 额度订单确认不应失败（否则免费包永远发不出去）: %v", err)
	}
	if !credited {
		t.Fatal("首次确认应视为入账成功（credits=0 也要发权益）")
	}
	if got.PackageCode != pkgCode {
		t.Errorf("订单应回读 package_code=%s，实际 %q", pkgCode, got.PackageCode)
	}

	// 订单必须落到 paid，而不是被退回 pending。
	after, err := store.GetPayment(ctx, p.ID)
	if err != nil {
		t.Fatalf("回读订单: %v", err)
	}
	if after.Status != PayStatusPaid {
		t.Fatalf("订单应为 paid，实际 %s（被退回 pending 说明走了入账失败分支）", after.Status)
	}

	// 余额不变（credits=0），但权益必须存在。
	var afterCredits int
	if err := pool.QueryRow(ctx, `SELECT COALESCE(credits,0) FROM users WHERE id=$1`, userID).
		Scan(&afterCredits); err != nil {
		t.Fatalf("读余额: %v", err)
	}
	if afterCredits != beforeCredits {
		t.Errorf("credits=0 的免费包不应改变余额：%d → %d", beforeCredits, afterCredits)
	}

	var entCount int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM user_entitlements WHERE user_id = $1 AND package_code = $2`,
		userID, pkgCode).Scan(&entCount); err != nil {
		t.Fatalf("查权益: %v", err)
	}
	if entCount != 1 {
		t.Fatalf("应发放 1 份权益，实际 %d 份", entCount)
	}

	// 幂等：再确认一次不应重复发放（MarkPaymentPaid 的 pending 条件挡住）。
	if _, credited2, err := mgr.ConfirmPayment(ctx, p.ID, "free_"+p.ID); err != nil {
		t.Fatalf("重复确认不应报错: %v", err)
	} else if credited2 {
		t.Error("重复确认不应再次入账")
	}
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM user_entitlements WHERE user_id = $1 AND package_code = $2`,
		userID, pkgCode).Scan(&entCount); err != nil {
		t.Fatalf("复查权益: %v", err)
	}
	if entCount != 1 {
		t.Errorf("重复确认后权益应仍为 1 份，实际 %d 份", entCount)
	}
}
