package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/db"
)

// billing_paypal.go —— PayPal 支付渠道。
//
// 由 billing_handler.go 按渠道拆出（L3-4 批次 A，同包纯移动，逐字未改）。
//
// 为什么按渠道切而不是按端点切：支付渠道之间除了「下单→回调→发货」这条骨架，
// 几乎不共享代码 —— 支付宝当面付、微信 Native、PayPal Checkout 各有自己的
// 签名/验签与订单生命周期。按渠道分文件后，新增一个渠道就是新增一个文件，
// 不必在 1000 行的公共文件里翻找该改哪几段。
//
// 留在 billing_handler.go 的是与渠道无关的部分：余额、流水、用量分摊、
// 套餐与权益、以及 createPayment 的分发骨架与 paymentResponse 的共用响应。

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
