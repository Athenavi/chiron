package billing

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/settings"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 临时诊断：用真实沙箱凭据验证「非 ASCII 转义」修复是否让各类 subject 都能出码。
func TestDiagAlipaySubjectIsolation(t *testing.T) {
	if os.Getenv("DIAG_ALIPAY_REAL") == "" {
		t.Skip("DIAG_ALIPAY_REAL 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	pool, err := pgxpool.New(ctx, os.Getenv("CHIRON_TEST_POSTGRES_DSN"))
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer pool.Close()

	store := settings.New(pool, config.LoadAllowUnconfigured().AppSecret)
	m, err := store.LoadConfig(ctx, "payment")
	if err != nil {
		t.Fatalf("读配置: %v", err)
	}
	appID, _ := m["alipay_app_id"].(string)
	gateway, _ := m["alipay_gateway"].(string)
	privPEM, _ := m["alipay_private_key"].(string)
	pubKey, _ := m["alipay_public_key"].(string)

	client, err := NewAlipayClient(appID, privPEM, pubKey, gateway, "")
	if err != nil {
		t.Fatalf("构造客户端: %v", err)
	}

	ts := time.Now().Format("150405")
	cases := []struct{ name, subject string }{
		{"纯 ASCII", "abcdef"},
		{"含空格", "abc def"},
		{"多行中文（服务端形态）", fmt.Sprintf("chiron 充值 %d credits", 1000)},
		{"日文", "あいう"},
		{"韩文", "한글"},
		{"欧元符号", "€100"},
		{"emoji", "🙂 recharge"},
		{"中文+特殊字符", "充值 & 退款"},
	}

	pass, fail := 0, 0
	for i, c := range cases {
		qr, err := client.Precreate(ctx, fmt.Sprintf("diagV%d%s", i, ts), 100, c.subject)
		if err != nil {
			fail++
			msg := err.Error()
			if len(msg) > 90 {
				msg = msg[:90]
			}
			t.Logf("%-24s -> ❌ %s", c.name, msg)
			continue
		}
		pass++
		t.Logf("%-24s -> ✅ %s", c.name, qr)
	}
	t.Logf("汇总: 成功 %d / 失败 %d", pass, fail)
}
