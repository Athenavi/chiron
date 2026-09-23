package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/db"
)

type BillingHandler struct {
	mgr           *billing.Manager
	authenticator *auth.Authenticator
	cfg           *config.Config
	payPalClient  *http.Client

	// payMu 保护下列支付凭据与渠道客户端：后台「支付配置」保存后会热替换
	// （见 ApplyPaymentConfig），而请求处理路径是并发的，必须加锁读取。
	payMu  sync.RWMutex
	payCfg PaymentConfig
	alipay *billing.AlipayClient // nil = 未配置/未启用，充值入口返回 501
	wechat *billing.WechatClient // nil = 未配置/未启用
}

func NewBillingHandler(mgr *billing.Manager, authenticator *auth.Authenticator, cfg *config.Config) *BillingHandler {
	h := &BillingHandler{
		mgr: mgr, authenticator: authenticator, cfg: cfg,
		payPalClient: &http.Client{Timeout: 30 * time.Second},
	}
	// 初始凭据来自环境变量；后台配置（system_settings.payment）在启动时由
	// ReloadPaymentConfig 叠加，两者合并结果才是生效配置。
	h.ApplyPaymentConfig(paymentConfigFromEnv(cfg))
	return h
}

// ApplyPaymentConfig 热替换支付凭据并重建渠道客户端。
// 单个渠道构造失败（如私钥 PEM 非法）只令该渠道不可用并记日志，不影响其它渠道，
// 也不回滚已保存的配置 —— 避免把「配错了」放大成「全都不能用」。
func (h *BillingHandler) ApplyPaymentConfig(p PaymentConfig) {
	alipay, wechat, errs := buildPaymentClients(p)
	if len(errs) > 0 {
		slog.Error("payment client init failed", "error", joinErrs(errs))
	}
	h.payMu.Lock()
	defer h.payMu.Unlock()
	h.payCfg = p
	h.alipay = alipay
	h.wechat = wechat
}

// paymentConfig 返回当前生效的支付配置快照（值拷贝，调用方可安全读取）。
func (h *BillingHandler) paymentConfig() PaymentConfig {
	h.payMu.RLock()
	defer h.payMu.RUnlock()
	return h.payCfg
}

// alipayClient 返回当前支付宝客户端（nil 表示未配置或未启用）。
func (h *BillingHandler) alipayClient() *billing.AlipayClient {
	h.payMu.RLock()
	defer h.payMu.RUnlock()
	return h.alipay
}

// wechatClient 返回当前微信支付客户端（nil 表示未配置或未启用）。
func (h *BillingHandler) wechatClient() *billing.WechatClient {
	h.payMu.RLock()
	defer h.payMu.RUnlock()
	return h.wechat
}

func (h *BillingHandler) firstOrigin() string {
	allow := currentCORSAllowOrigin()
	if allow == "" && h.cfg != nil {
		// 兜底：启动注入前的构造期值（正常路径 gateway_router 会先 SetCORSAllowOrigin）
		allow = h.cfg.CORSOrigins
	}
	parts := strings.SplitN(allow, ",", 2)
	if len(parts) == 0 {
		return ""
	}
	return strings.TrimRight(parts[0], "/ ")
}

func (h *BillingHandler) GetBalance(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	balance, err := h.mgr.GetBalance(userID)
	if err != nil {
		slog.Error("get balance failed", "user_id", userID, "error", err)
		InternalError(w, "failed to get balance")
		return
	}

	// Also return daily free count for diagnosis
	count, countErr := h.mgr.DailyFreeCount(r.Context(), userID)
	diag := map[string]interface{}{
		"user_id":              userID,
		"balance":              balance,
		"daily_free_limit":     billing.DailyFreeLimit,
		"daily_free_used":      count,
		"daily_free_remaining": billing.DailyFreeLimit - count,
		"within_free_quota":    count < billing.DailyFreeLimit,
	}
	if countErr != nil {
		diag["daily_free_error"] = countErr.Error()
	}

	OK(w, diag)
}

func (h *BillingHandler) GetHistory(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	history, err := h.mgr.GetHistory(r.Context(), userID, 50)
	if err != nil {
		slog.Error("get billing history failed", "user_id", userID, "error", err)
		InternalError(w, "failed to get billing history")
		return
	}

	OK(w, map[string]interface{}{"user_id": userID, "history": history})
}

// GetUsage returns aggregated usage stats for the user: daily token consumption and costs.
func (h *BillingHandler) GetUsage(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT DATE(created_at::timestamp) as day,
			COUNT(*) as tx_count,
			SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as credits_spent,
			SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as credits_added
		 FROM credit_transactions
		 WHERE user_id = $1 AND created_at::timestamp >= NOW() - INTERVAL '30 days'
		 GROUP BY DATE(created_at::timestamp)
		 ORDER BY day DESC`, userID)
	if err != nil {
		slog.Error("get usage stats failed", "user_id", userID, "error", err, "db_available", db.GlobalDBManager.IsAvailable())
		InternalError(w, fmt.Sprintf("failed to get usage stats: %v", err))
		return
	}
	defer rows.Close()

	type dailyUsage struct {
		Day          string `json:"day"`
		TxCount      int    `json:"tx_count"`
		CreditsSpent int    `json:"credits_spent"`
		CreditsAdded int    `json:"credits_added"`
	}

	var daily []dailyUsage
	for rows.Next() {
		var d dailyUsage
		var dayTime time.Time
		if err := rows.Scan(&dayTime, &d.TxCount, &d.CreditsSpent, &d.CreditsAdded); err != nil {
			continue
		}
		d.Day = dayTime.Format("2006-01-02")
		daily = append(daily, d)
	}
	if err := rows.Err(); err != nil {
		InternalError(w, "failed to iterate daily usage")
		return
	}

	// Compute totals
	totalSpent := 0
	totalAdded := 0
	for _, d := range daily {
		totalSpent += d.CreditsSpent
		totalAdded += d.CreditsAdded
	}

	OK(w, map[string]interface{}{
		"daily":       daily,
		"total_spent": totalSpent,
		"total_added": totalAdded,
		"period_days": 30,
	})
}

func (h *BillingHandler) Recharge(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if !auth.HasPermission(claims, auth.PermAdminWrite) {
		Forbidden(w, "admin permission required")
		return
	}
	userID := claims.UserID

	var body struct {
		Amount int `json:"amount"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Amount <= 0 {
		BadRequest(w, "amount > 0 required")
		return
	}

	balance, err := h.mgr.AddCredits(userID, "recharge", body.Amount)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "internal error")
		return
	}

	OK(w, map[string]interface{}{"user_id": userID, "amount": body.Amount, "balance": balance})
}

func (h *BillingHandler) resolveUserID(r *http.Request) string {
	claims := auth.GetClaims(r.Context())
	if claims != nil {
		return claims.UserID
	}
	return ""
}

// ── 支付下单 / 查询 / 回调 ──────────────────────────────────────────────

// CreatePayment 创建充值订单并调用支付渠道预下单。
// body: {credits: int, channel: "alipay" | "wechat" | "paypal"}
// 返回订单信息（alipay/wechat 含 qr_code 二维码内容；paypal 含 checkout_url）。
func (h *BillingHandler) CreatePayment(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	userID := claims.UserID

	var body struct {
		Credits int    `json:"credits"`
		Channel string `json:"channel"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Credits <= 0 {
		BadRequest(w, "credits > 0 required")
		return
	}
	switch body.Channel {
	case billing.ChannelAlipay, billing.ChannelWechat, billing.ChannelPayPal:
	default:
		BadRequest(w, "channel must be alipay / wechat / paypal")
		return
	}
	// 后台可将某个渠道停用（不改动其凭据），此时直接拒绝下单，避免落一笔永远付不掉的订单。
	if !h.paymentConfig().ChannelEnabled(body.Channel) {
		JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "该支付渠道已停用"})
		return
	}

	// 1 credit = 1 分；支付宝/微信为人民币，PayPal 为美元
	amountCents := int64(body.Credits)
	currency := "CNY"
	if body.Channel == billing.ChannelPayPal {
		currency = "USD"
	}

	p := billing.NewPayment(userID, body.Channel, body.Credits, amountCents, currency)
	if err := h.mgr.CreatePayment(r.Context(), p); err != nil {
		slog.Error("create payment failed", "error", err)
		InternalError(w, "failed to create payment order")
		return
	}

	ctx := r.Context()
	subject := fmt.Sprintf("chiron 充值 %d credits", body.Credits)
	switch body.Channel {
	case billing.ChannelAlipay:
		alipay := h.alipayClient()
		if alipay == nil {
			JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "支付宝支付未配置（后台「支付配置」或 ALIPAY_* 环境变量）"})
			return
		}
		qr, err := alipay.Precreate(ctx, p.ID, amountCents, subject)
		if err != nil {
			if mErr := h.mgr.MarkPaymentFailed(ctx, p.ID); mErr != nil {
				slog.Error("payment status update failed", "error", mErr)
			}
			// 把渠道给出的具体原因一并返回（前端显示在括号里）。只回"支付下单失败"
			// 会让用户与运维都无从下手 —— 例如网关配错时返回非 JSON 的情况。
			logAndRespond(w, err, http.StatusInternalServerError, "支付下单失败："+err.Error())
			return
		}
		if err := h.mgr.UpdatePaymentProvider(ctx, p.ID, qr, p.ID); err != nil {
			slog.Warn("update alipay payment provider", "error", err)
		}
		p.QRCode = qr

	case billing.ChannelWechat:
		wechat := h.wechatClient()
		if wechat == nil {
			JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "微信支付未配置（后台「支付配置」或 WXPAY_* 环境变量）"})
			return
		}
		notifyURL := h.paymentConfig().WechatNotifyURL()
		if notifyURL == "" {
			JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "公网基础 URL 未配置，无法接收微信回调"})
			return
		}
		codeURL, err := wechat.Precreate(ctx, p.ID, amountCents, subject, notifyURL)
		if err != nil {
			if mErr := h.mgr.MarkPaymentFailed(ctx, p.ID); mErr != nil {
				slog.Error("payment status update failed", "error", mErr)
			}
			logAndRespond(w, err, http.StatusInternalServerError, "微信下单失败："+err.Error())
			return
		}
		if err := h.mgr.UpdatePaymentProvider(ctx, p.ID, codeURL, p.ID); err != nil {
			slog.Warn("update wechat payment provider", "error", err)
		}
		p.QRCode = codeURL

	case billing.ChannelPayPal:
		h.createPayPalPayment(w, r, userID, body.Credits, p)
		return
	}

	OK(w, h.paymentResponse(p))
}

// GetOrder 查询订单状态（前端轮询）。pending 时主动向渠道查询一次兜底，
// 已支付则幂等入账并返回最新状态。
func (h *BillingHandler) GetOrder(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "order id required")
		return
	}
	ctx := r.Context()

	p, err := h.mgr.GetPayment(ctx, id)
	if err != nil || p == nil {
		NotFound(w, "payment order not found")
		return
	}
	if p.UserID != claims.UserID {
		Forbidden(w, "access denied")
		return
	}

	if p.Status == billing.PayStatusPending {
		// 渠道查询兜底（失败不阻塞响应）
		switch p.Channel {
		case billing.ChannelAlipay:
			if client := h.alipayClient(); client != nil {
				if tradeNo, paid, qErr := client.Query(ctx, p.ID); qErr == nil && paid {
					if _, _, cErr := h.mgr.ConfirmPayment(ctx, p.ID, tradeNo); cErr != nil {
						slog.Warn("alipay query confirm failed", "error", cErr)
					}
				}
			}
		case billing.ChannelWechat:
			if client := h.wechatClient(); client != nil {
				if tradeNo, paid, qErr := client.Query(ctx, p.ID); qErr == nil && paid {
					if _, _, cErr := h.mgr.ConfirmPayment(ctx, p.ID, tradeNo); cErr != nil {
						slog.Warn("wechat query confirm failed", "error", cErr)
					}
				}
			}
		}
		if fresh, fErr := h.mgr.GetPayment(ctx, id); fErr == nil && fresh != nil {
			p = fresh
		}
	}

	OK(w, h.paymentResponse(p))
}

// AlipayCallback 支付宝异步通知（无认证，靠签名验签）。
// 验签通过且金额一致则幂等入账；返回 "success"（支付宝协议要求）。
func (h *BillingHandler) AlipayCallback(w http.ResponseWriter, r *http.Request) {
	alipay := h.alipayClient()
	if alipay == nil {
		JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "alipay not configured"})
		return
	}
	if err := r.ParseForm(); err != nil {
		BadRequest(w, "bad form")
		return
	}
	params := make(map[string]string, len(r.PostForm))
	for k, vs := range r.PostForm {
		if len(vs) > 0 {
			params[k] = vs[0]
		}
	}

	outTradeNo, tradeNo, ok := alipay.VerifyCallback(params)
	if !ok {
		slog.Warn("alipay callback verify failed")
		BadRequest(w, "signature verification failed")
		return
	}

	ctx := r.Context()
	p, err := h.mgr.GetPayment(ctx, outTradeNo)
	if err != nil || p == nil {
		slog.Warn("alipay callback unknown order", "out_trade_no", outTradeNo)
		BadRequest(w, "unknown order")
		return
	}

	// 金额校验：回调 total_amount（元）必须与订单一致
	if amt, aErr := strconv.ParseFloat(params["total_amount"], 64); aErr == nil {
		if int64(amt*100) != p.AmountCents {
			slog.Warn("alipay callback amount mismatch", "order", p.ID, "got", params["total_amount"], "want", p.AmountCents)
			BadRequest(w, "amount mismatch")
			return
		}
	}

	if _, _, err := h.mgr.ConfirmPayment(ctx, p.ID, tradeNo); err != nil {
		slog.Error("alipay callback confirm failed", "error", err)
		InternalError(w, "confirm failed")
		return
	}
	slog.Info("alipay payment confirmed", "order", p.ID, "user", p.UserID, "credits", p.Credits)

	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Write([]byte("success"))
}

// WechatCallback 微信支付回调（无认证，靠平台证书验签 + AES-GCM 解密）。
// 验签/金额校验通过则幂等入账；返回微信要求的 {"code":"SUCCESS"}。
func (h *BillingHandler) WechatCallback(w http.ResponseWriter, r *http.Request) {
	wechat := h.wechatClient()
	if wechat == nil {
		JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "wechat not configured"})
		return
	}
	outTradeNo, tradeNo, paid, amountCents, err := wechat.ParseCallback(r)
	if err != nil {
		slog.Warn("wechat callback parse failed", "error", err)
		BadRequest(w, "invalid wechat callback")
		return
	}
	ctx := r.Context()
	p, err := h.mgr.GetPayment(ctx, outTradeNo)
	if err != nil || p == nil {
		slog.Warn("wechat callback unknown order", "out_trade_no", outTradeNo)
		BadRequest(w, "unknown order")
		return
	}
	if amountCents != nil && *amountCents != p.AmountCents {
		slog.Warn("wechat callback amount mismatch", "order", p.ID, "got", amountCents, "want", p.AmountCents)
		BadRequest(w, "amount mismatch")
		return
	}
	if !paid {
		// 未支付成功（如 USERPAYING），不处理但正常应答
		OK(w, map[string]string{"code": "SUCCESS", "message": "成功"})
		return
	}
	if _, _, err := h.mgr.ConfirmPayment(ctx, p.ID, tradeNo); err != nil {
		slog.Error("wechat callback confirm failed", "error", err)
		InternalError(w, "confirm failed")
		return
	}
	slog.Info("wechat payment confirmed", "order", p.ID, "user", p.UserID, "credits", p.Credits)
	OK(w, map[string]string{"code": "SUCCESS", "message": "成功"})
}

// paymentResponse 序列化订单（不暴露内部字段）。
func (h *BillingHandler) paymentResponse(p *billing.Payment) map[string]interface{} {
	resp := map[string]interface{}{
		"id":           p.ID,
		"channel":      p.Channel,
		"credits":      p.Credits,
		"amount_cents": p.AmountCents,
		"currency":     p.Currency,
		"status":       p.Status,
		"created_at":   p.CreatedAt,
	}
	if p.QRCode != "" {
		resp["qr_code"] = p.QRCode
	}
	return resp
}

// ── PayPal ────────────────────────────────────────────────────────────────

// createPayPalPayment 创建 PayPal 订单并落库 payments，返回 checkout_url。
func (h *BillingHandler) createPayPalPayment(w http.ResponseWriter, r *http.Request, userID string, credits int, p *billing.Payment) {
	if pcfg := h.paymentConfig(); pcfg.PayPalClientID == "" || pcfg.PayPalSecret == "" {
		JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "PayPal not configured"})
		return
	}

	amount := fmt.Sprintf("%.2f", float64(credits)/100)
	orderID, approvalURL, err := h.payPalCreateOrder(r.Context(), credits, amount, userID)
	if err != nil {
		slog.Error("paypal order failed", "error", err)
		if mErr := h.mgr.MarkPaymentFailed(r.Context(), p.ID); mErr != nil {
			slog.Error("payment status update failed", "error", mErr)
		}
		InternalError(w, "PayPal 下单失败："+err.Error())
		return
	}

	if err := h.mgr.UpdatePaymentProvider(r.Context(), p.ID, "", orderID); err != nil {
		slog.Warn("update paypal payment provider", "error", err)
	}

	OK(w, map[string]interface{}{
		"id":           p.ID,
		"channel":      p.Channel,
		"credits":      p.Credits,
		"amount_cents": p.AmountCents,
		"currency":     p.Currency,
		"status":       p.Status,
		"checkout_url": approvalURL,
		"created_at":   p.CreatedAt,
	})
}

// PayPalCapture captures an approved PayPal order and credits the user.
func (h *BillingHandler) PayPalCapture(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body struct {
		OrderID string `json:"order_id"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.OrderID == "" {
		BadRequest(w, "order_id required")
		return
	}
	if result, err := h.payPalCaptureOrder(r.Context(), body.OrderID); err != nil {
		InternalError(w, "PayPal capture failed")
		return
	} else if status, _ := result["status"].(string); status != "COMPLETED" {
		slog.Error("paypal capture not completed", "order", body.OrderID, "status", status)
		InternalError(w, "PayPal capture was not completed")
		return
	}

	// 从 payments 表按 provider_order_id 定位订单并幂等入账
	var payID, credits string
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT id, credits::text FROM payments
		 WHERE provider_order_id = $1 AND user_id = $2 AND status = 'pending'`,
		body.OrderID, userID).Scan(&payID, &credits)
	if err != nil {
		slog.Info("paypal capture already processed or not found", "order", body.OrderID)
		OK(w, map[string]interface{}{"status": "already_processed"})
		return
	}
	if _, _, err := h.mgr.ConfirmPayment(r.Context(), payID, body.OrderID); err != nil {
		slog.Error("paypal capture credit failed", "error", err)
		InternalError(w, "crediting failed")
		return
	}
	balance, balErr := h.mgr.GetBalance(userID)
	if balErr != nil {
		balance = 0
	}
	OK(w, map[string]interface{}{"status": "completed", "balance": balance, "credits": credits})
}

// ── PayPal REST API helpers ───────────────────────────────────────────────

func (h *BillingHandler) payPalBaseURL() string {
	if h.paymentConfig().PayPalSandbox {
		return "https://api-m.sandbox.paypal.com"
	}
	return "https://api-m.paypal.com"
}

func (h *BillingHandler) payPalAccessToken(ctx context.Context) (string, error) {
	pcfg := h.paymentConfig()
	p := strings.NewReader("grant_type=client_credentials")
	req, err := http.NewRequestWithContext(ctx, "POST", h.payPalBaseURL()+"/v1/oauth2/token", p)
	if err != nil {
		return "", fmt.Errorf("paypal token request: %w", err)
	}
	req.SetBasicAuth(pcfg.PayPalClientID, pcfg.PayPalSecret)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := h.payPalClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("paypal token: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		bodyBytes, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("paypal token: HTTP %d: %s", resp.StatusCode, string(bodyBytes))
	}
	var r struct {
		AccessToken string `json:"access_token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return "", fmt.Errorf("paypal token decode: %w", err)
	}
	return r.AccessToken, nil
}

func (h *BillingHandler) payPalCreateOrder(ctx context.Context, credits int, amount, userID string) (string, string, error) {
	token, err := h.payPalAccessToken(ctx)
	if err != nil {
		return "", "", err
	}
	body, _ := json.Marshal(map[string]interface{}{
		"intent": "CAPTURE",
		"purchase_units": []map[string]interface{}{{
			"reference_id": userID,
			"description":  fmt.Sprintf("%d Credits", credits),
			"amount":       map[string]string{"currency_code": "USD", "value": amount},
		}},
		"payment_source": map[string]interface{}{
			"paypal": map[string]interface{}{
				"experience_context": map[string]string{
					"payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
					"return_url":                fmt.Sprintf("%s/billing?success=1&provider=paypal", h.firstOrigin()),
					"cancel_url":                fmt.Sprintf("%s/billing?canceled=1", h.firstOrigin()),
				},
			},
		},
	})
	req, err := http.NewRequestWithContext(ctx, "POST", h.payPalBaseURL()+"/v2/checkout/orders", strings.NewReader(string(body)))
	if err != nil {
		return "", "", fmt.Errorf("paypal create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := h.payPalClient.Do(req)
	if err != nil {
		return "", "", fmt.Errorf("paypal create: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		bodyBytes, _ := io.ReadAll(resp.Body)
		return "", "", fmt.Errorf("paypal create: HTTP %d: %s", resp.StatusCode, string(bodyBytes))
	}
	var r struct {
		ID    string                       `json:"id"`
		Links []struct{ Rel, Href string } `json:"links"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return "", "", fmt.Errorf("paypal decode: %w", err)
	}
	for _, l := range r.Links {
		if l.Rel == "payer-action" {
			return r.ID, l.Href, nil
		}
	}
	return r.ID, "", nil
}

func (h *BillingHandler) payPalCaptureOrder(ctx context.Context, orderID string) (map[string]interface{}, error) {
	token, err := h.payPalAccessToken(ctx)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", h.payPalBaseURL()+"/v2/checkout/orders/"+orderID+"/capture", nil)
	if err != nil {
		return nil, fmt.Errorf("paypal capture request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := h.payPalClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("paypal capture: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		bodyBytes, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("paypal capture: HTTP %d: %s", resp.StatusCode, string(bodyBytes))
	}
	var r map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, fmt.Errorf("paypal capture decode: %w", err)
	}
	return r, nil
}
