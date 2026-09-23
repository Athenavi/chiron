package api

import (
	"context"
	"errors"
	"log/slog"
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/settings"
)

// ── 支付配置：热加载与 HTTP 接口 ──
//
// 配置模型与加载逻辑见 payment_config.go。本文件负责：
//   - 从 DB 重载配置并热应用到渠道客户端（本实例 + 跨副本广播订阅者）；
//   - 后台「支付配置」页面的读写接口（/v1/admin/payments）；
//   - 用户端「哪些渠道可用」查询（/v1/billing/channels）。

// ReloadPaymentConfig 从 DB 重新加载支付配置、热重建渠道客户端并返回生效配置。
//
// 这是生产路径**唯一**的配置应用入口：启动注入、后台保存、跨副本广播订阅都走它。
// 以 DB 为唯一事实源（而非"当前值 + 本次增量"），因此无论调用方提交的是全量还是部分
// 字段，运行期生效配置都与落库结果一致 —— 不会出现"运行期按增量算、重启后按 DB 读"
// 两套结果不一致的情况（后者典型症状是部分提交后其它渠道凭据被静默回退成 env 值）。
func (h *BillingHandler) ReloadPaymentConfig(ctx context.Context, store *settings.Store) PaymentConfig {
	var m map[string]interface{}
	if store != nil {
		loaded, err := store.LoadConfig(ctx, settingsCategoryPayment)
		if err != nil {
			// 读不到就保持当前配置：宁可沿用旧凭据，也不要因一次 DB 抖动把所有渠道清空
			slog.Warn("load payment config failed; keeping current config", "error", err)
			return h.paymentConfig()
		}
		m = loaded
	}
	cfg := h.applyConfigMap(m)

	// 加载结果必须可见：配置"重启后丢失"这类问题，全靠这行日志区分是
	// 「没落库（db_keys=0）」还是「落了库但没读出来/读出来没生效」。
	st := h.ChannelStatus()
	slog.Info("payment config loaded",
		"db_keys", len(m),
		"store_available", store != nil,
		"public_base_url_set", cfg.PublicBaseURL != "",
		"alipay_ready", st[billing.ChannelAlipay].Enabled,
		"wechat_ready", st[billing.ChannelWechat].Enabled,
		"paypal_ready", st[billing.ChannelPayPal].Enabled)
	return cfg
}

// applyConfigMap 以 env 为基准应用一组键值并热重建渠道客户端。
// 包内可见：供测试直接构造"某份配置下的渠道可用性"，生产路径统一经 ReloadPaymentConfig
// 从 DB 取值，不直接调用它。
func (h *BillingHandler) applyConfigMap(m map[string]interface{}) PaymentConfig {
	cfg := paymentConfigFromEnv(h.cfg).applyMap(m)
	h.ApplyPaymentConfig(cfg)
	return cfg
}

// ChannelStatus 汇总各渠道当前可用性（后台展示与用户端过滤共用）。
// 读锁内只做字段拷贝，客户端构造等耗时操作不在此路径上。
func (h *BillingHandler) ChannelStatus() map[string]PaymentChannelStatus {
	h.payMu.RLock()
	p := h.payCfg
	alipay, wechat := h.alipay, h.wechat
	h.payMu.RUnlock()

	out := make(map[string]PaymentChannelStatus, len(paymentChannels))
	for _, ch := range paymentChannels {
		out[ch] = channelStatus(p, ch, alipay, wechat)
	}
	return out
}

// channelStatus 判定单渠道可用性：开关打开 + 必填项齐全 + 客户端构造成功。
func channelStatus(p PaymentConfig, channel string, alipay *billing.AlipayClient, wechat *billing.WechatClient) PaymentChannelStatus {
	st := PaymentChannelStatus{Enabled: p.ChannelEnabled(channel), Currency: ChannelCurrency(channel)}
	if !st.Enabled {
		// 显式停用：不列缺失项（那是管理员的主动选择，不是配置问题）
		return st
	}
	missing := p.channelMissing(channel)
	if len(missing) == 0 {
		// 字段齐全但客户端没构造出来，说明是凭据本身非法（如私钥 PEM 解析失败）
		switch channel {
		case billing.ChannelAlipay:
			if alipay == nil {
				missing = append(missing, "客户端初始化失败（请检查应用私钥/支付宝公钥格式）")
			}
		case billing.ChannelWechat:
			if wechat == nil {
				missing = append(missing, "客户端初始化失败（请检查商户私钥格式）")
			}
		}
	}
	st.Missing = missing
	st.Enabled = len(missing) == 0
	return st
}

// paymentConfigToMap 把生效配置展开为前端表单键值。
// 含明文凭据 —— 面向已鉴权的管理员，与后台其它分组（Redis 密码、S3 Secret）的回显口径一致。
func paymentConfigToMap(p PaymentConfig) map[string]interface{} {
	return map[string]interface{}{
		"public_base_url": p.PublicBaseURL,

		"alipay_enabled":     p.AlipayEnabled,
		"alipay_app_id":      p.AlipayAppID,
		"alipay_private_key": p.AlipayPrivateKey,
		"alipay_public_key":  p.AlipayPublicKey,
		"alipay_gateway":     p.AlipayGateway,

		"wechat_enabled":            p.WechatEnabled,
		"wechat_mch_id":             p.WechatMchID,
		"wechat_app_id":             p.WechatAppID,
		"wechat_api_v3_key":         p.WechatAPIv3Key,
		"wechat_mch_cert_serial_no": p.WechatMchCertSerialNo,
		"wechat_mch_private_key":    p.WechatMchPrivateKey,

		"paypal_enabled":   p.PayPalEnabled,
		"paypal_client_id": p.PayPalClientID,
		"paypal_secret":    p.PayPalSecret,
		"paypal_sandbox":   p.PayPalSandbox,
	}
}

// callbackURLs 返回需要在支付渠道后台登记的回调地址（未配置公网 URL 时为空串）。
func (p PaymentConfig) callbackURLs() map[string]string {
	return map[string]string{
		billing.ChannelAlipay: p.AlipayNotifyURL(),
		billing.ChannelWechat: p.WechatNotifyURL(),
	}
}

// GetPaymentConfig GET /v1/admin/payments
// 返回当前生效配置（DB 覆盖 env 的结果）、各渠道可用性与回调地址。
func (h *AdminHandler) GetPaymentConfig(w http.ResponseWriter, r *http.Request) {
	if h.billingHandler == nil {
		InternalError(w, "billing unavailable")
		return
	}
	p := h.billingHandler.paymentConfig()
	OK(w, map[string]interface{}{
		"config":        paymentConfigToMap(p),
		"channels":      h.billingHandler.ChannelStatus(),
		"callback_urls": p.callbackURLs(),
	})
}

// SavePaymentConfig PUT /v1/admin/payments
//
// 保存前先按目标配置构造渠道客户端做校验：凭据配错（如私钥 PEM 非法）直接返回 400
// 且不落库，避免把"看起来保存成功、实际用不了"的配置写进去。字段缺失不算错误 ——
// 那是"还没配置"，不是"配错了"。
//
// 保存成功后本实例立即热生效，并广播给其它副本（订阅者按同样路径重载）。
func (h *AdminHandler) SavePaymentConfig(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Config map[string]interface{} `json:"config"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Config == nil {
		BadRequest(w, "config is required")
		return
	}
	if h.billingHandler == nil {
		InternalError(w, "billing unavailable")
		return
	}
	store := h.ensureSettingsStore()
	if store == nil {
		InternalError(w, "settings store unavailable")
		return
	}

	// 仅接受白名单键；值为 null 表示删除该键（回退环境变量默认值）
	filtered := filterPaymentConfig(body.Config)

	// 保存前校验：按本次提交的目标配置构造渠道客户端。
	// 目的不是完整模拟落库结果，而是拦住"配错了"（如私钥 PEM 无法解析）——
	// 这类错误若放行，渠道会静默不可用，排查成本远高于一次 400。
	target := paymentConfigFromEnv(h.cfg).applyMap(filtered)
	if _, _, errs := buildPaymentClients(target); len(errs) > 0 {
		BadRequest(w, "支付渠道配置校验失败："+joinErrs(errs))
		return
	}

	// 审计字段：记用户 ID（claims.ID 是 JWT 的 jti，不是用户标识）
	userID := ""
	if claims := auth.GetClaims(r.Context()); claims != nil {
		userID = claims.UserID
	}
	ctx := r.Context()
	if err := store.SaveConfig(ctx, settingsCategoryPayment, filtered, userID); err != nil {
		slog.Error("save payment config failed", "error", err)
		if errors.Is(err, settings.ErrEncryptedKeyNotFound) {
			InternalError(w, "APP_SECRET 未配置，无法加密支付凭据；请注入 APP_SECRET 后重启")
			return
		}
		// 带上底层原因（admin-only 接口）：笼统的"保存失败"让运维无从下手 ——
		// 本次故障本可以靠一句 42P10 直接定位，却只显示出 "failed to save payment config"。
		InternalError(w, "保存支付配置失败："+err.Error())
		return
	}

	// 本实例热生效：从 DB 重载并重建渠道客户端（无需重启）。
	// 与广播订阅者走同一入口、同一事实源，副本间不会漂移。
	applied := h.billingHandler.ReloadPaymentConfig(ctx, store)
	// 跨副本广播：订阅者按同一路径重载，消除多副本配置不一致
	if err := PublishSettingsChanged(ctx, settingsCategoryPayment, filtered); err != nil {
		slog.Warn("publish payment settings changed failed", "error", err)
	}

	slog.Info("payment config saved", "keys", len(filtered), "updated_by", userID)
	OK(w, map[string]interface{}{
		"status":        "saved",
		"config":        paymentConfigToMap(applied),
		"channels":      h.billingHandler.ChannelStatus(),
		"callback_urls": applied.callbackURLs(),
	})
}

// ListPaymentChannels GET /v1/billing/channels
//
// 返回后台已配置启用的支付渠道。充值页据此只展示真正可用的渠道 ——
// 否则用户选中未配置的渠道，要等到下单时才拿到 501。
// 只暴露 id/币种，不泄露缺失项等配置细节。
func (h *BillingHandler) ListPaymentChannels(w http.ResponseWriter, r *http.Request) {
	status := h.ChannelStatus()
	out := make([]map[string]interface{}, 0, len(paymentChannels))
	for _, ch := range paymentChannels {
		st := status[ch]
		if !st.Enabled {
			continue
		}
		out = append(out, map[string]interface{}{"id": ch, "currency": st.Currency})
	}
	OK(w, map[string]interface{}{"channels": out})
}
