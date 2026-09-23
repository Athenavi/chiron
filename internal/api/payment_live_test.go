package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/settings"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 临时端到端验证：走真实 Postgres 跑 PUT/GET /v1/admin/payments 的完整保存路径。
func TestLivePaymentConfigEndToEnd(t *testing.T) {
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

	// 备份真实配置并在结束时恢复：payment 是生产分类，测试不得吞掉用户数据
	appSecret := backupPaymentConfig(t, ctx, pool)

	cfg := &config.Config{PublicBaseURL: "https://api.example.com"}
	h := &AdminHandler{
		cfg:            cfg,
		appSecret:      appSecret,
		settingsStore:  settings.New(pool, appSecret),
		billingHandler: NewBillingHandler(nil, nil, cfg),
	}

	claims := &auth.Claims{UserID: "11111111-2222-3333-4444-555555555555", Role: "owner"}
	priv, pub := testRSAKeyPair(t)

	call := func(method string, handler http.HandlerFunc, body map[string]interface{}) *httptest.ResponseRecorder {
		raw, _ := json.Marshal(body)
		req := httptest.NewRequest(method, "/v1/admin/payments", bytes.NewReader(raw))
		req = req.WithContext(auth.WithClaims(req.Context(), claims))
		rec := httptest.NewRecorder()
		handler(rec, req)
		return rec
	}

	// 1) 保存三渠道配置（保存前校验 + 加密落库 + 热生效）
	rec := call(http.MethodPut, h.SavePaymentConfig, map[string]interface{}{
		"config": map[string]interface{}{
			"public_base_url":    "https://api.example.com",
			"alipay_app_id":      "app-live",
			"alipay_private_key": priv,
			"alipay_public_key":  pub,
			"paypal_client_id":   "client-live",
			"paypal_secret":      "secret-live",
		},
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT 保存失败 status=%d body=%s", rec.Code, rec.Body.String())
	}

	var saved struct {
		Success bool `json:"success"`
		Data    struct {
			Config   map[string]interface{} `json:"config"`
			Channels map[string]struct {
				Enabled  bool     `json:"enabled"`
				Currency string   `json:"currency"`
				Missing  []string `json:"missing"`
			} `json:"channels"`
			CallbackURLs map[string]string `json:"callback_urls"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &saved); err != nil {
		t.Fatalf("解析响应: %v (%s)", err, rec.Body.String())
	}
	if !saved.Success {
		t.Fatalf("success=false: %s", rec.Body.String())
	}
	if !saved.Data.Channels["alipay"].Enabled {
		t.Errorf("支付宝应已生效: %+v", saved.Data.Channels["alipay"])
	}
	if !saved.Data.Channels["paypal"].Enabled {
		t.Errorf("PayPal 应已生效: %+v", saved.Data.Channels["paypal"])
	}
	if saved.Data.Channels["wechat"].Enabled {
		t.Errorf("微信未配置不应生效: %+v", saved.Data.Channels["wechat"])
	}
	if got := saved.Data.CallbackURLs["alipay"]; got != "https://api.example.com/v1/billing/callback/alipay" {
		t.Errorf("回调地址 = %q", got)
	}
	// 热生效：内存中的渠道状态同步
	if st := h.billingHandler.ChannelStatus(); !st[billing.ChannelAlipay].Enabled {
		t.Errorf("热生效失败，支付宝仍未可用: %+v", st[billing.ChannelAlipay])
	}

	// 2) 落库校验：私钥加密、AppID 明文、updated_by 记用户 ID
	var encrypted bool
	var storedValue, updatedBy string
	if err := pool.QueryRow(ctx,
		`SELECT encrypted, value::text, COALESCE(updated_by,'') FROM system_settings
		  WHERE category='payment' AND key='alipay_private_key'`).Scan(&encrypted, &storedValue, &updatedBy); err != nil {
		t.Fatalf("查落库: %v", err)
	}
	if !encrypted {
		t.Error("私钥应加密落库")
	}
	if bytes.Contains([]byte(storedValue), []byte("BEGIN PRIVATE KEY")) {
		t.Error("落库内容不应包含明文私钥")
	}
	if updatedBy != claims.UserID {
		t.Errorf("updated_by = %q, want %q", updatedBy, claims.UserID)
	}

	// 3) GET 回读：管理员拿到明文（与 Redis/S3 回显口径一致）
	rec = call(http.MethodGet, h.GetPaymentConfig, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET 失败 status=%d", rec.Code)
	}
	var loaded struct {
		Data struct {
			Config map[string]interface{} `json:"config"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &loaded); err != nil {
		t.Fatalf("解析 GET: %v", err)
	}
	if loaded.Data.Config["alipay_app_id"] != "app-live" {
		t.Errorf("回读 alipay_app_id = %v", loaded.Data.Config["alipay_app_id"])
	}
	if loaded.Data.Config["alipay_private_key"] != priv {
		t.Error("私钥未正确解密回填")
	}

	// 4) 停用渠道：不再可用，但凭据仍在
	rec = call(http.MethodPut, h.SavePaymentConfig, map[string]interface{}{
		"config": map[string]interface{}{
			"alipay_enabled":     false,
			"alipay_app_id":      "app-live",
			"alipay_private_key": priv,
			"alipay_public_key":  pub,
			"paypal_client_id":   "client-live",
			"paypal_secret":      "secret-live",
		},
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("停用保存失败 status=%d body=%s", rec.Code, rec.Body.String())
	}
	st := h.billingHandler.ChannelStatus()[billing.ChannelAlipay]
	if st.Enabled {
		t.Error("停用后支付宝不应可用")
	}
	if len(st.Missing) != 0 {
		t.Errorf("主动停用不应报告缺失项: %v", st.Missing)
	}

	// 5) 非法私钥：保存前校验应 400 且不落库
	rec = call(http.MethodPut, h.SavePaymentConfig, map[string]interface{}{
		"config": map[string]interface{}{
			"alipay_enabled":     true,
			"alipay_app_id":      "app-live",
			"alipay_private_key": "-----BEGIN PRIVATE KEY-----\nnot-base64\n",
			"alipay_public_key":  pub,
		},
	})
	if rec.Code != http.StatusBadRequest {
		t.Errorf("非法私钥应 400，got %d: %s", rec.Code, rec.Body.String())
	}
	// 校验失败不应回写：库里 alipay_enabled 仍是第 4 步写入的 false
	var enabledText string
	if err := pool.QueryRow(ctx,
		`SELECT value::text FROM system_settings WHERE category='payment' AND key='alipay_enabled'`).Scan(&enabledText); err != nil {
		t.Fatalf("查 alipay_enabled: %v", err)
	}
	if enabledText != "false" {
		t.Errorf("校验失败却改动了落库配置：alipay_enabled = %s", enabledText)
	}
}

// backupPaymentConfig 备份真实 payment 配置，并在测试结束后恢复。
//
// payment 是后台「支付配置」页面正在使用的**生产分类**，测试绝不能把它吞掉 ——
// 早期版本用 `DELETE ... WHERE category='payment'` 清理，直接抹掉了用户刚保存的配置。
// 另外必须用**与生产一致的 APP_SECRET**，否则敏感字段解不出来、备份会残缺。
// 返回可用的 appSecret，供测试构造 store/handler 使用。
func backupPaymentConfig(t *testing.T, ctx context.Context, pool *pgxpool.Pool) string {
	t.Helper()
	secret := config.LoadAllowUnconfigured().AppSecret
	store := settings.New(pool, secret)

	backup, err := store.LoadConfig(ctx, settingsCategoryPayment)
	if err != nil {
		t.Fatalf("备份 payment 配置: %v", err)
	}
	t.Logf("已备份 %d 个 payment 键，测试结束后恢复", len(backup))

	t.Cleanup(func() {
		// 必须自建连接：t.Cleanup 在测试函数返回之后才执行，晚于调用方的
		// `defer pool.Close()` —— 复用外层 pool 会得到 "closed pool"，恢复静默失败。
		bg, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		restorePool, err := pgxpool.New(bg, os.Getenv("CHIRON_TEST_POSTGRES_DSN"))
		if err != nil {
			t.Errorf("恢复配置时连接数据库失败: %v", err)
			return
		}
		defer restorePool.Close()

		if _, err := restorePool.Exec(bg, `DELETE FROM system_settings WHERE category='payment'`); err != nil {
			t.Errorf("清理测试数据失败: %v", err)
			return
		}
		if len(backup) == 0 {
			t.Logf("测试前无 payment 配置，已清空测试数据")
			return
		}
		// 明文备份经 SaveConfig 重新加密写回，语义与测试前等价
		if err := settings.New(restorePool, secret).SaveConfig(bg, settingsCategoryPayment, backup, "live-test-restore"); err != nil {
			t.Errorf("恢复原有 payment 配置失败: %v", err)
			return
		}
		t.Logf("已恢复 %d 个 payment 键", len(backup))
	})
	return secret
}

// 复刻「支付渠道配置在重启后丢失」：后台保存 → 服务重启 → 配置必须依然生效。
// 用全新的 BillingHandler 模拟重启后的进程（env 里没有任何支付凭据，只能靠 DB 重载恢复）。
func TestLivePaymentConfigSurvivesRestart(t *testing.T) {
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

	// 备份真实配置并在结束时恢复：payment 是生产分类，测试不得吞掉用户数据
	secret := backupPaymentConfig(t, ctx, pool)
	store := settings.New(pool, secret)
	cfg := &config.Config{PublicBaseURL: "https://api.example.com"}
	priv, pub := testRSAKeyPair(t)

	// ── 进程 1：后台保存配置 ──
	writer := &AdminHandler{
		cfg:            cfg,
		appSecret:      secret,
		settingsStore:  store,
		billingHandler: NewBillingHandler(nil, nil, cfg),
	}
	raw, _ := json.Marshal(map[string]interface{}{
		"config": map[string]interface{}{
			"public_base_url":    "https://api.example.com",
			"alipay_app_id":      "app-restart",
			"alipay_private_key": priv,
			"alipay_public_key":  pub,
			"paypal_client_id":   "client-restart",
			"paypal_secret":      "secret-restart",
		},
	})
	req := httptest.NewRequest(http.MethodPut, "/v1/admin/payments", bytes.NewReader(raw))
	req = req.WithContext(auth.WithClaims(req.Context(),
		&auth.Claims{UserID: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", Role: "owner"}))
	rec := httptest.NewRecorder()
	writer.SavePaymentConfig(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("保存失败 status=%d body=%s", rec.Code, rec.Body.String())
	}

	// 落库必须真实可见 —— 重启后读的就是它
	var stored int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM system_settings WHERE category='payment'`).Scan(&stored); err != nil {
		t.Fatalf("查落库: %v", err)
	}
	if stored == 0 {
		t.Fatal("保存返回 200 但 system_settings 里没有数据 —— 这正是配置丢失的根因")
	}

	// ── 进程 2：模拟重启（全新 handler + 启动时的那次重载）──
	reader := NewBillingHandler(nil, nil, cfg)
	if reader.ChannelStatus()[billing.ChannelAlipay].Enabled {
		t.Fatal("测试前提不成立：重载前不应有可用渠道（env 里没有凭据）")
	}
	reader.ReloadPaymentConfig(ctx, store) // 等价于 gateway_router 启动时的调用

	st := reader.ChannelStatus()
	if !st[billing.ChannelAlipay].Enabled {
		t.Errorf("重启后支付宝应恢复可用: %+v", st[billing.ChannelAlipay])
	}
	if !st[billing.ChannelPayPal].Enabled {
		t.Errorf("重启后 PayPal 应恢复可用: %+v", st[billing.ChannelPayPal])
	}
	if st[billing.ChannelWechat].Enabled {
		t.Errorf("微信未配置不应可用: %+v", st[billing.ChannelWechat])
	}

	// 凭据逐字节一致（PEM 多行最容易被持久化打断）
	p := reader.paymentConfig()
	if p.AlipayPrivateKey != priv {
		t.Error("重启后应用私钥未完整恢复")
	}
	if p.AlipayAppID != "app-restart" || p.PayPalSecret != "secret-restart" {
		t.Errorf("重启后凭据不一致: app_id=%q", p.AlipayAppID)
	}
	if p.PublicBaseURL != "https://api.example.com" {
		t.Errorf("重启后 public_base_url 丢失: %q", p.PublicBaseURL)
	}
}
