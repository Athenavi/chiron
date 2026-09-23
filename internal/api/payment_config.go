package api

import (
	"fmt"
	"strings"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/billing"
)

// ── 支付渠道配置（system_settings.category = "payment"）──
//
// 背景：支付凭据原先只能由环境变量注入，任何改动都必须重启进程；后台「系统设置」里的
// 支付卡片只有两个非敏感字段，且 BillingHandler 从不读取该分组 —— 保存了也不生效。
//
// 现方案：后台「支付配置」页面把凭据写入 system_settings（敏感键由 settings.Store 依
// IsSensitive 判定后 AES-256-GCM 加密落库），与 env 合并得到**运行期生效配置**：
// DB 值优先、env 兜底。保存成功后立即热重建渠道客户端（无需滚动重启），并向其它副本广播。
//
// 键名与前端 views/admin/PaymentView.vue 的表单字段一一对应。

// settingsCategoryPayment 后台支付配置所在的 system_settings 分组。
const settingsCategoryPayment = "payment"

// paymentConfigKeys 允许写入 payment 分组的键（白名单）。
// 未列出的键一律忽略，避免脏键污染配置分组（同类分组由前后端共同约定）。
var paymentConfigKeys = []string{
	"public_base_url",
	"alipay_enabled", "alipay_app_id", "alipay_private_key", "alipay_public_key", "alipay_gateway",
	"wechat_enabled", "wechat_mch_id", "wechat_app_id", "wechat_api_v3_key",
	"wechat_mch_cert_serial_no", "wechat_mch_private_key",
	"paypal_enabled", "paypal_client_id", "paypal_secret", "paypal_sandbox",
}

// paymentChannels 渠道展示顺序（后台表单与用户端一致）。
var paymentChannels = []string{billing.ChannelAlipay, billing.ChannelWechat, billing.ChannelPayPal}

// paymentConfigKeySet 是 paymentConfigKeys 的查找集。
var paymentConfigKeySet = func() map[string]struct{} {
	m := make(map[string]struct{}, len(paymentConfigKeys))
	for _, k := range paymentConfigKeys {
		m[k] = struct{}{}
	}
	return m
}()

// filterPaymentConfig 仅保留白名单内的键。未列出的键静默忽略而非报错 ——
// 前端表单可能先行演进（多提交字段），不该因此让整次保存失败。
// 值为 nil 的键保留：settings.Store 用它表示「删除该键、回退环境变量」。
func filterPaymentConfig(in map[string]interface{}) map[string]interface{} {
	out := make(map[string]interface{}, len(in))
	for k, v := range in {
		if _, ok := paymentConfigKeySet[k]; ok {
			out[k] = v
		}
	}
	return out
}

// PaymentConfig 是运行期生效的支付配置快照：由 env（config.Config）构造后
// 再被 DB 中的值覆盖。因此零值不代表「未配置」——判断可用性请用 ChannelStatus。
type PaymentConfig struct {
	// PublicBaseURL 公网可达的基础地址，用于拼接支付宝/微信异步通知地址。
	PublicBaseURL string

	AlipayEnabled    bool
	AlipayAppID      string
	AlipayPrivateKey string // 应用私钥（PEM）
	AlipayPublicKey  string // 支付宝公钥（PEM）
	AlipayGateway    string

	WechatEnabled         bool
	WechatMchID           string
	WechatAppID           string
	WechatAPIv3Key        string
	WechatMchCertSerialNo string
	WechatMchPrivateKey   string // 商户 API 证书私钥（PEM）

	PayPalEnabled  bool
	PayPalClientID string
	PayPalSecret   string
	PayPalSandbox  bool
}

// PaymentChannelStatus 描述单个渠道的可用性。
// Missing 仅在 Enabled=true 且配置不全时非空（显式停用的渠道不列缺失项）。
type PaymentChannelStatus struct {
	Enabled  bool     `json:"enabled"`
	Currency string   `json:"currency"`
	Missing  []string `json:"missing,omitempty"`
}

// normalize 清理标识类字段里复制粘贴带进来的首尾空白与结尾斜杠。
//
// 这些**不可见字符**的代价极高：URL 尾部多一个空格后，请求路径会变成
// POST /gateway.do%20，网关直接回 Apache 404（"The requested URL was not found"），
// 而日志里地址看上去完全正确，会被误判成"网关地址写错了"。
// 密钥/私钥不在此列 —— PEM 内部的换行与空格具有语义，不能动。
func (p PaymentConfig) normalize() PaymentConfig {
	p.PublicBaseURL = strings.TrimRight(strings.TrimSpace(p.PublicBaseURL), "/")
	p.AlipayGateway = strings.TrimSpace(p.AlipayGateway)
	p.AlipayAppID = strings.TrimSpace(p.AlipayAppID)
	p.WechatMchID = strings.TrimSpace(p.WechatMchID)
	p.WechatAppID = strings.TrimSpace(p.WechatAppID)
	p.WechatMchCertSerialNo = strings.TrimSpace(p.WechatMchCertSerialNo)
	p.PayPalClientID = strings.TrimSpace(p.PayPalClientID)
	return p
}

// paymentConfigFromEnv 以环境变量为兜底基准构造配置。
// 渠道开关缺省为启用：保持"配置齐全即可用"的历史语义（env 无开关概念）。
func paymentConfigFromEnv(cfg *config.Config) PaymentConfig {
	p := PaymentConfig{AlipayEnabled: true, WechatEnabled: true, PayPalEnabled: true}
	if cfg == nil {
		return p.normalize()
	}
	p.PublicBaseURL = cfg.PublicBaseURL
	p.AlipayAppID = cfg.AlipayAppID
	p.AlipayPrivateKey = cfg.AlipayPrivateKey
	p.AlipayPublicKey = cfg.AlipayPublicKey
	p.AlipayGateway = cfg.AlipayGateway
	p.WechatMchID = cfg.WechatMchID
	p.WechatAppID = cfg.WechatAppID
	p.WechatAPIv3Key = cfg.WechatAPIv3Key
	p.WechatMchCertSerialNo = cfg.WechatMchCertSerialNo
	p.WechatMchPrivateKey = cfg.WechatMchPrivateKey
	p.PayPalClientID = cfg.PayPalClientID
	p.PayPalSecret = cfg.PayPalSecret
	p.PayPalSandbox = cfg.PayPalSandbox
	return p.normalize()
}

// applyMap 用 DB 中已持久化的键值覆盖配置；键不存在时沿用当前（兜底）值。
func (p PaymentConfig) applyMap(m map[string]interface{}) PaymentConfig {
	if len(m) == 0 {
		return p
	}
	applyStr(m, "public_base_url", &p.PublicBaseURL)
	applyBool(m, "alipay_enabled", &p.AlipayEnabled)
	applyStr(m, "alipay_app_id", &p.AlipayAppID)
	applyStr(m, "alipay_private_key", &p.AlipayPrivateKey)
	applyStr(m, "alipay_public_key", &p.AlipayPublicKey)
	applyStr(m, "alipay_gateway", &p.AlipayGateway)
	applyBool(m, "wechat_enabled", &p.WechatEnabled)
	applyStr(m, "wechat_mch_id", &p.WechatMchID)
	applyStr(m, "wechat_app_id", &p.WechatAppID)
	applyStr(m, "wechat_api_v3_key", &p.WechatAPIv3Key)
	applyStr(m, "wechat_mch_cert_serial_no", &p.WechatMchCertSerialNo)
	applyStr(m, "wechat_mch_private_key", &p.WechatMchPrivateKey)
	applyBool(m, "paypal_enabled", &p.PayPalEnabled)
	applyStr(m, "paypal_client_id", &p.PayPalClientID)
	applyStr(m, "paypal_secret", &p.PayPalSecret)
	applyBool(m, "paypal_sandbox", &p.PayPalSandbox)
	return p.normalize()
}

// applyStr 仅当键存在且为字符串时覆盖目标，避免用非法类型清空已有值。
func applyStr(m map[string]interface{}, key string, dst *string) {
	if s, ok := m[key].(string); ok {
		*dst = s
	}
}

// applyBool 仅当键存在且可解释为布尔时覆盖目标。
func applyBool(m map[string]interface{}, key string, dst *bool) {
	switch v := m[key].(type) {
	case bool:
		*dst = v
	case string:
		s := strings.ToLower(strings.TrimSpace(v))
		if s == "true" || s == "1" {
			*dst = true
		} else if s == "false" || s == "0" {
			*dst = false
		}
	}
}

// ChannelEnabled 返回渠道是否被启用（开关层面，不含配置完整性判断）。
func (p PaymentConfig) ChannelEnabled(channel string) bool {
	switch channel {
	case billing.ChannelAlipay:
		return p.AlipayEnabled
	case billing.ChannelWechat:
		return p.WechatEnabled
	case billing.ChannelPayPal:
		return p.PayPalEnabled
	}
	return false
}

// channelMissing 返回渠道缺失的必填项展示名（配置齐全时为空）。
// 微信额外要求 PublicBaseURL：Native 下单必须携带回调地址，否则支付结果无法回执。
func (p PaymentConfig) channelMissing(channel string) []string {
	var missing []string
	switch channel {
	case billing.ChannelAlipay:
		if p.AlipayAppID == "" {
			missing = append(missing, "AppID")
		}
		if p.AlipayPrivateKey == "" {
			missing = append(missing, "应用私钥")
		}
		if p.AlipayPublicKey == "" {
			missing = append(missing, "支付宝公钥")
		}
	case billing.ChannelWechat:
		if p.WechatMchID == "" {
			missing = append(missing, "商户号")
		}
		if p.WechatAppID == "" {
			missing = append(missing, "AppID")
		}
		if p.WechatAPIv3Key == "" {
			missing = append(missing, "APIv3 密钥")
		}
		if p.WechatMchCertSerialNo == "" {
			missing = append(missing, "商户证书序列号")
		}
		if p.WechatMchPrivateKey == "" {
			missing = append(missing, "商户私钥")
		}
		if p.PublicBaseURL == "" {
			missing = append(missing, "公网基础 URL")
		}
	case billing.ChannelPayPal:
		if p.PayPalClientID == "" {
			missing = append(missing, "Client ID")
		}
		if p.PayPalSecret == "" {
			missing = append(missing, "Secret")
		}
	}
	return missing
}

// ChannelCurrency 返回渠道结算币种（支付宝/微信为人民币，PayPal 为美元）。
func ChannelCurrency(channel string) string {
	if channel == billing.ChannelPayPal {
		return "USD"
	}
	return "CNY"
}

// AlipayNotifyURL 支付宝异步通知地址；未配置 PublicBaseURL 时返回空串。
func (p PaymentConfig) AlipayNotifyURL() string {
	return notifyURL(p.PublicBaseURL, "/v1/billing/callback/alipay")
}

// WechatNotifyURL 微信支付异步通知地址；未配置 PublicBaseURL 时返回空串。
func (p PaymentConfig) WechatNotifyURL() string {
	return notifyURL(p.PublicBaseURL, "/v1/billing/callback/wechat")
}

func notifyURL(baseURL, path string) string {
	if baseURL == "" {
		return ""
	}
	return strings.TrimRight(baseURL, "/") + path
}

// buildPaymentClients 按配置构造渠道客户端（nil 表示未启用或配置不全）。
//
// 纯构造：不改动任何共享状态，因此可同时用于「保存前校验」与「热重载」。
// 返回 errs 汇总因配置非法（如私钥 PEM 无法解析）导致的构造失败 ——
// 字段缺失不算错误（那是"尚未配置"，不是"配错了"）。
func buildPaymentClients(p PaymentConfig) (alipay *billing.AlipayClient, wechat *billing.WechatClient, errs []error) {
	if p.AlipayEnabled && len(p.channelMissing(billing.ChannelAlipay)) == 0 {
		client, err := billing.NewAlipayClient(
			p.AlipayAppID, p.AlipayPrivateKey, p.AlipayPublicKey, p.AlipayGateway, p.AlipayNotifyURL())
		if err != nil {
			errs = append(errs, fmt.Errorf("支付宝：%w", err))
		} else {
			alipay = client
		}
	}
	if p.WechatEnabled && len(p.channelMissing(billing.ChannelWechat)) == 0 {
		client, err := billing.NewWechatClient(
			p.WechatMchID, p.WechatAppID, p.WechatAPIv3Key, p.WechatMchCertSerialNo, p.WechatMchPrivateKey)
		if err != nil {
			errs = append(errs, fmt.Errorf("微信支付：%w", err))
		} else {
			wechat = client
		}
	}
	return alipay, wechat, errs
}

// joinErrs 把多个构造错误合并为一条可读消息。
func joinErrs(errs []error) string {
	parts := make([]string, 0, len(errs))
	for _, err := range errs {
		parts = append(parts, err.Error())
	}
	return strings.Join(parts, "；")
}
