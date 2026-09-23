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

	const clean = `DELETE FROM system_settings WHERE category='payment'`
	if _, err := pool.Exec(ctx, clean); err != nil {
		t.Fatalf("cleanup: %v", err)
	}
	defer func() { _, _ = pool.Exec(context.Background(), clean) }()

	cfg := &config.Config{PublicBaseURL: "https://api.example.com"}
	h := &AdminHandler{
		cfg:            cfg,
		appSecret:      "live-test-secret",
		settingsStore:  settings.New(pool, "live-test-secret"),
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
