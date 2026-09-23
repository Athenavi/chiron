package settings

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// 临时端到端验证：对真实 Postgres 跑 SaveConfig/LoadConfig 的完整往返。
// 通过 CHIRON_TEST_POSTGRES_DSN 启用；未设置则跳过（仓库默认不依赖任何外部服务）。
func TestLiveSaveConfigUpsert(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置，跳过真实库验证")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer pool.Close()

	// 用独立分类，避免触碰任何真实配置
	const category = "__live_upsert_test__"
	if _, err := pool.Exec(ctx, `DELETE FROM system_settings WHERE category=$1`, category); err != nil {
		t.Fatalf("cleanup: %v", err)
	}
	defer func() {
		if _, err := pool.Exec(context.Background(), `DELETE FROM system_settings WHERE category=$1`, category); err != nil {
			t.Errorf("final cleanup: %v", err)
		}
	}()

	store := New(pool, "live-test-app-secret")

	// 含换行的多行值：模拟 PEM 私钥/公钥。JSON 往返若按"去引号"处理会把 \n 变成
	// 字面反斜杠+n，这类值正是最容易被打断的数据。
	const multiLine = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0B\nAQEFAASCBKcwggSjAgEAAoIBAQ\n-----END PRIVATE KEY-----\n"

	// 1) 首次写入（走 INSERT 分支）—— 旧实现在这一步就会 42P10 失败
	if err := store.SaveConfig(ctx, category, map[string]interface{}{
		"public_base_url": "https://first.example.com",
	}, "tester"); err != nil {
		t.Fatalf("首次保存失败（ON CONFLICT 依赖唯一约束的老问题？）: %v", err)
	}

	// 2) 覆盖同一个键（走 UPDATE 分支）+ 布尔值 + 敏感键（加密）与非敏感多行值（明文）
	if err := store.SaveConfig(ctx, category, map[string]interface{}{
		"public_base_url":   "https://second.example.com",
		"alipay_enabled":    true,
		"paypal_secret":     multiLine, // 敏感 → 加密入库
		"alipay_public_key": multiLine, // 非敏感 → 明文入库
	}, "tester"); err != nil {
		t.Fatalf("覆盖保存失败: %v", err)
	}

	// 3) 值为 nil = 删除该键（回退 env）
	if err := store.SaveConfig(ctx, category, map[string]interface{}{
		"alipay_enabled": nil,
	}, "tester"); err != nil {
		t.Fatalf("删除键失败: %v", err)
	}

	// 4) 读回并校验
	got, err := store.LoadConfig(ctx, category)
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if got["public_base_url"] != "https://second.example.com" {
		t.Errorf("public_base_url = %v, want 覆盖后的值", got["public_base_url"])
	}
	if got["paypal_secret"] != multiLine {
		t.Errorf("加密键的多行值往返被破坏:\n got %q\nwant %q", got["paypal_secret"], multiLine)
	}
	if got["alipay_public_key"] != multiLine {
		t.Errorf("明文键的多行值往返被破坏:\n got %q\nwant %q", got["alipay_public_key"], multiLine)
	}
	if _, ok := got["alipay_enabled"]; ok {
		t.Error("值为 nil 的键应被删除、不再返回")
	}

	// 5) 每个键只能有一行（upsert 未产生重复）
	var dup int
	if err := pool.QueryRow(ctx, `
		SELECT count(*) FROM (
			SELECT category, key FROM system_settings WHERE category=$1
			 GROUP BY category, key HAVING count(*) > 1
		) d`, category).Scan(&dup); err != nil {
		t.Fatalf("查重复: %v", err)
	}
	if dup != 0 {
		t.Errorf("出现了 %d 个重复 (category,key) 组合", dup)
	}

	// 6) 布尔值往返
	if err := store.SaveConfig(ctx, category, map[string]interface{}{"paypal_sandbox": true}, "tester"); err != nil {
		t.Fatalf("保存布尔值失败: %v", err)
	}
	got, err = store.LoadConfig(ctx, category)
	if err != nil {
		t.Fatalf("LoadConfig(2): %v", err)
	}
	if got["paypal_sandbox"] != true {
		t.Errorf("paypal_sandbox = %#v, want true", got["paypal_sandbox"])
	}
}
