package api

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"sync"
	"testing"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/billing"
)

// testRSAKeyPair 生成一对 RSA 密钥 PEM（PKCS#8 私钥 + PKIX 公钥），
// 供支付宝渠道的凭据解析用例使用。
func testRSAKeyPair(t *testing.T) (privPEM, pubPEM string) {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate rsa key: %v", err)
	}
	privDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatalf("marshal private key: %v", err)
	}
	pubDER, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatalf("marshal public key: %v", err)
	}
	privPEM = string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: privDER}))
	pubPEM = string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: pubDER}))
	return privPEM, pubPEM
}

// 渠道开关缺省为启用：env 没有开关概念，缺省值必须保持"配置齐全即可用"的历史语义。
func TestPaymentConfigFromEnvEnablesChannelsByDefault(t *testing.T) {
	p := paymentConfigFromEnv(&config.Config{})
	for _, ch := range paymentChannels {
		if !p.ChannelEnabled(ch) {
			t.Fatalf("渠道 %s 的开关应缺省为启用", ch)
		}
	}
}

func TestPaymentConfigFromEnvReadsConfig(t *testing.T) {
	p := paymentConfigFromEnv(&config.Config{
		PublicBaseURL:  "https://api.example.com",
		AlipayAppID:    "env-app",
		AlipayGateway:  "https://openapi.alipay.com/gateway.do",
		WechatMchID:    "env-mch",
		PayPalClientID: "env-client",
		PayPalSandbox:  true,
	})

	if p.PublicBaseURL != "https://api.example.com" {
		t.Errorf("PublicBaseURL = %q", p.PublicBaseURL)
	}
	if p.AlipayAppID != "env-app" || p.AlipayGateway != "https://openapi.alipay.com/gateway.do" {
		t.Errorf("支付宝字段未从 env 读入: %+v", p)
	}
	if p.WechatMchID != "env-mch" || p.PayPalClientID != "env-client" || !p.PayPalSandbox {
		t.Errorf("微信/PayPal 字段未从 env 读入: %+v", p)
	}
}

// 未配置任何渠道时不应报告可用（否则充值页会展示下单必然失败的渠道）。
func TestPaymentConfigEmptyIsNotReady(t *testing.T) {
	p := paymentConfigFromEnv(nil)
	for _, ch := range paymentChannels {
		if len(p.channelMissing(ch)) == 0 {
			t.Errorf("渠道 %s 在空配置下不应无缺失项", ch)
		}
	}
	if p.AlipayNotifyURL() != "" || p.WechatNotifyURL() != "" {
		t.Error("未配置 PublicBaseURL 时回调地址应为空串")
	}
}

func TestApplyMapOverridesAndFallsBack(t *testing.T) {
	base := paymentConfigFromEnv(&config.Config{AlipayAppID: "env-app", WechatMchID: "env-mch"})
	got := base.applyMap(map[string]interface{}{
		"alipay_app_id":  "db-app",  // 覆盖
		"wechat_mch_id":  nil,       // 删除语义：不覆盖 → 保持 env 值
		"paypal_sandbox": "true",    // 字符串布尔可解释
		"alipay_enabled": false,     // 布尔关闭
		"unknown_key":    "ignored", // 非白名单键不参与映射
	})

	if got.AlipayAppID != "db-app" {
		t.Errorf("AlipayAppID = %q, want db-app", got.AlipayAppID)
	}
	if got.WechatMchID != "env-mch" {
		t.Errorf("WechatMchID = %q, want env-mch（nil 不应覆盖）", got.WechatMchID)
	}
	if !got.PayPalSandbox {
		t.Error("PayPalSandbox 应被字符串 \"true\" 打开")
	}
	if got.AlipayEnabled {
		t.Error("AlipayEnabled 应被 false 关闭")
	}
}

// 类型不符的值不应清空已有配置（避免前端传错类型就把凭据抹掉）。
func TestApplyMapIgnoresWrongType(t *testing.T) {
	base := paymentConfigFromEnv(&config.Config{AlipayAppID: "env-app"})
	got := base.applyMap(map[string]interface{}{"alipay_app_id": 42, "alipay_enabled": "maybe"})
	if got.AlipayAppID != "env-app" {
		t.Errorf("AlipayAppID = %q, want env-app（非字符串不应覆盖）", got.AlipayAppID)
	}
	if !got.AlipayEnabled {
		t.Error("AlipayEnabled 不应被无法解释的字符串改动")
	}
}

func TestFilterPaymentConfigKeepsWhitelistOnly(t *testing.T) {
	in := map[string]interface{}{
		"alipay_app_id": "app",
		"unknown_key":   "drop",
		"wechat_mch_id": nil, // 删除语义必须保留
	}
	out := filterPaymentConfig(in)

	if _, ok := out["unknown_key"]; ok {
		t.Error("非白名单键应被过滤")
	}
	if v, ok := out["wechat_mch_id"]; !ok || v != nil {
		t.Error("值为 nil 的白名单键必须保留（表示回退 env）")
	}
	if out["alipay_app_id"] != "app" {
		t.Error("白名单键应原样保留")
	}
	if len(out) != 2 {
		t.Errorf("过滤后键数 = %d, want 2", len(out))
	}
}

// 微信 Native 下单必须携带回调地址，缺少公网 URL 时应报告为缺失项。
func TestChannelMissing(t *testing.T) {
	p := PaymentConfig{}
	alipay := p.channelMissing(billing.ChannelAlipay)
	if len(alipay) != 3 {
		t.Errorf("支付宝缺失项 = %v, want 3 项", alipay)
	}
	wechat := p.channelMissing(billing.ChannelWechat)
	if !containsStr(wechat, "公网基础 URL") {
		t.Errorf("微信缺失项应包含公网基础 URL，got %v", wechat)
	}
	paypal := p.channelMissing(billing.ChannelPayPal)
	if len(paypal) != 2 {
		t.Errorf("PayPal 缺失项 = %v, want 2 项", paypal)
	}
}

func TestNotifyURLJoinsPath(t *testing.T) {
	p := PaymentConfig{PublicBaseURL: "https://api.example.com/"}
	if got := p.AlipayNotifyURL(); got != "https://api.example.com/v1/billing/callback/alipay" {
		t.Errorf("AlipayNotifyURL = %q", got)
	}
	if got := p.WechatNotifyURL(); got != "https://api.example.com/v1/billing/callback/wechat" {
		t.Errorf("WechatNotifyURL = %q", got)
	}
}

// 凭据非法（私钥无法解析）必须让渠道不可用并给出可读原因，
// 而不是"看起来配好了、下单时才失败"。
func TestBuildPaymentClientsRejectsInvalidKey(t *testing.T) {
	_, pub := testRSAKeyPair(t)
	_, _, errs := buildPaymentClients(PaymentConfig{
		AlipayEnabled:    true,
		AlipayAppID:      "app",
		AlipayPrivateKey: "not-a-pem",
		AlipayPublicKey:  pub,
	})
	if len(errs) == 0 {
		t.Fatal("非法私钥应产生构造错误")
	}

	priv, pubKey := testRSAKeyPair(t)
	alipay, _, errs := buildPaymentClients(PaymentConfig{
		AlipayEnabled:    true,
		AlipayAppID:      "app",
		AlipayPrivateKey: priv,
		AlipayPublicKey:  pubKey,
	})
	if len(errs) != 0 {
		t.Fatalf("合法凭据不应产生错误: %v", errs)
	}
	if alipay == nil {
		t.Fatal("合法凭据应构造出支付宝客户端")
	}
}

// 仅"未配置"的渠道不构造客户端，也不算错误。
func TestBuildPaymentClientsSkipsUnconfigured(t *testing.T) {
	alipay, wechat, errs := buildPaymentClients(PaymentConfig{AlipayEnabled: true, WechatEnabled: true, PayPalEnabled: true})
	if len(errs) != 0 {
		t.Fatalf("字段缺失不是错误: %v", errs)
	}
	if alipay != nil || wechat != nil {
		t.Error("未配置齐全的渠道不应构造客户端")
	}
}

func TestChannelStatusReflectsConfig(t *testing.T) {
	priv, pub := testRSAKeyPair(t)
	h := NewBillingHandler(nil, nil, &config.Config{
		PublicBaseURL:    "https://api.example.com",
		AlipayAppID:      "app",
		AlipayPrivateKey: priv,
		AlipayPublicKey:  pub,
	})

	st := h.ChannelStatus()
	if !st[billing.ChannelAlipay].Enabled {
		t.Errorf("支付宝应可用: %+v", st[billing.ChannelAlipay])
	}
	if st[billing.ChannelAlipay].Currency != "CNY" {
		t.Errorf("支付宝币种 = %q, want CNY", st[billing.ChannelAlipay].Currency)
	}
	if st[billing.ChannelWechat].Enabled {
		t.Error("微信未配置，不应可用")
	}
	if st[billing.ChannelPayPal].Enabled {
		t.Error("PayPal 未配置，不应可用")
	}
	if st[billing.ChannelPayPal].Currency != "USD" {
		t.Errorf("PayPal 币种 = %q, want USD", st[billing.ChannelPayPal].Currency)
	}
}

// 显式停用的渠道：Enabled=false 且不列缺失项（是管理员的主动选择，不是配置问题）。
func TestChannelStatusDisabledChannelHasNoMissing(t *testing.T) {
	priv, pub := testRSAKeyPair(t)
	h := NewBillingHandler(nil, nil, &config.Config{
		PublicBaseURL:    "https://api.example.com",
		AlipayAppID:      "app",
		AlipayPrivateKey: priv,
		AlipayPublicKey:  pub,
	})
	h.applyConfigMap(map[string]interface{}{"alipay_enabled": false})

	st := h.ChannelStatus()[billing.ChannelAlipay]
	if st.Enabled {
		t.Error("停用后不应可用")
	}
	if len(st.Missing) != 0 {
		t.Errorf("停用渠道不应报告缺失项: %v", st.Missing)
	}
}

// 字段齐全但私钥格式非法：可用性必须为 false 并说明原因。
func TestChannelStatusReportsInvalidCredentials(t *testing.T) {
	_, pub := testRSAKeyPair(t)
	h := NewBillingHandler(nil, nil, &config.Config{
		AlipayAppID:      "app",
		AlipayPrivateKey: "not-a-pem",
		AlipayPublicKey:  pub,
	})

	st := h.ChannelStatus()[billing.ChannelAlipay]
	if st.Enabled {
		t.Error("私钥非法时渠道不应可用")
	}
	if !containsStr(st.Missing, "客户端初始化失败（请检查应用私钥/支付宝公钥格式）") {
		t.Errorf("应提示客户端初始化失败，got %v", st.Missing)
	}
}

// 热重载路径与请求路径并发（-race 下验证锁的正确性）。
func TestPaymentConfigConcurrentAccess(t *testing.T) {
	h := NewBillingHandler(nil, nil, &config.Config{})

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				h.paymentConfig()
				h.ChannelStatus()
			}
		}()
	}
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				h.applyConfigMap(map[string]interface{}{
					"public_base_url": "https://api.example.com",
					"paypal_enabled":  i%2 == 0,
				})
			}
		}(i)
	}
	wg.Wait()
}

// paymentConfigKeys 与 paymentConfigToMap 必须严格对齐：
// 新增配置项时若漏改映射，后台表单会静默丢失该字段。
func TestPaymentConfigKeysMatchSerialization(t *testing.T) {
	m := paymentConfigToMap(PaymentConfig{})
	if len(m) != len(paymentConfigKeys) {
		t.Fatalf("paymentConfigToMap 暴露 %d 个键，paymentConfigKeys 有 %d 个", len(m), len(paymentConfigKeys))
	}
	for _, k := range paymentConfigKeys {
		if _, ok := m[k]; !ok {
			t.Errorf("paymentConfigKeys 中的 %q 未在 paymentConfigToMap 中暴露", k)
		}
	}
}

// 表单序列化 → 反序列化应还原全部配置（除缺省开关语义外）。
func TestPaymentConfigRoundTrip(t *testing.T) {
	want := PaymentConfig{
		PublicBaseURL:         "https://api.example.com",
		AlipayEnabled:         true,
		AlipayAppID:           "app",
		AlipayPrivateKey:      "priv",
		AlipayPublicKey:       "pub",
		AlipayGateway:         "https://openapi.alipay.com/gateway.do",
		WechatEnabled:         true,
		WechatMchID:           "mch",
		WechatAppID:           "wxapp",
		WechatAPIv3Key:        "v3key",
		WechatMchCertSerialNo: "serial",
		WechatMchPrivateKey:   "mchpriv",
		PayPalEnabled:         true,
		PayPalClientID:        "client",
		PayPalSecret:          "secret",
		PayPalSandbox:         true,
	}
	got := paymentConfigFromEnv(nil).applyMap(paymentConfigToMap(want))
	if got != want {
		t.Errorf("往返后配置不一致:\n got %+v\nwant %+v", got, want)
	}
}

// 后台保存凭据 → 渠道从「未生效」变为「已生效」，删除键后回退环境变量（热生效核心链路）。
func TestApplyConfigMapEnablesChannelAfterSave(t *testing.T) {
	h := NewBillingHandler(nil, nil, &config.Config{PublicBaseURL: "https://api.example.com"})
	if st := h.ChannelStatus()[billing.ChannelAlipay]; st.Enabled {
		t.Fatalf("无凭据时支付宝不应可用: %+v", st)
	}

	priv, pub := testRSAKeyPair(t)
	h.applyConfigMap(map[string]interface{}{
		"alipay_app_id":      "app",
		"alipay_private_key": priv,
		"alipay_public_key":  pub,
	})
	if st := h.ChannelStatus()[billing.ChannelAlipay]; !st.Enabled {
		t.Fatalf("保存凭据后支付宝应可用: %+v", st)
	}

	// 值为 nil 表示删除该键（回退环境变量；此处 env 无值）→ 重新变为不可用
	h.applyConfigMap(map[string]interface{}{
		"alipay_app_id":      nil,
		"alipay_private_key": nil,
		"alipay_public_key":  nil,
	})
	if st := h.ChannelStatus()[billing.ChannelAlipay]; st.Enabled {
		t.Fatalf("清空凭据后支付宝不应可用: %+v", st)
	}
}

// 复制粘贴常带不可见空白：URL 尾部多一个空格会让请求落到 /gateway.do%20，
// 网关回 Apache 404，而日志里地址看上去完全正确 —— 极易被误判成"地址写错了"。
func TestPaymentConfigNormalizesWhitespace(t *testing.T) {
	got := paymentConfigFromEnv(nil).applyMap(map[string]interface{}{
		"public_base_url":  "  https://api.example.com/  ",
		"alipay_gateway":   "https://openapi-sandbox.dl.alipaydev.com/gateway.do ",
		"alipay_app_id":    " app-x\n",
		"paypal_client_id": "\tclient-y",
	})

	if got.PublicBaseURL != "https://api.example.com" {
		t.Errorf("public_base_url 未规范化: %q", got.PublicBaseURL)
	}
	if got.AlipayGateway != "https://openapi-sandbox.dl.alipaydev.com/gateway.do" {
		t.Errorf("alipay_gateway 未规范化: %q", got.AlipayGateway)
	}
	if got.AlipayAppID != "app-x" {
		t.Errorf("alipay_app_id 未规范化: %q", got.AlipayAppID)
	}
	if got.PayPalClientID != "client-y" {
		t.Errorf("paypal_client_id 未规范化: %q", got.PayPalClientID)
	}

	// 密钥类不动：PEM 内部的换行与缩进具有语义，trim 会破坏私钥
	const pem = "-----BEGIN PRIVATE KEY-----\n  MIIEvQIBADANBg\n-----END PRIVATE KEY-----\n"
	if got := paymentConfigFromEnv(nil).applyMap(map[string]interface{}{
		"alipay_private_key": pem,
	}); got.AlipayPrivateKey != pem {
		t.Error("私钥不应被规范化，否则 PEM 解析会失败")
	}
}

func containsStr(list []string, want string) bool {
	for _, v := range list {
		if v == want {
			return true
		}
	}
	return false
}
