package api

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/db"
)

// refund_handler.go —— 租户级支付退款。
//
// 退款按订单归属的租户隔离：`payments.user_id → users.tenant_id` 必须等于 claims.tenant_id，
// 否则一律 404（不泄露其他租户的订单是否存在）。
//
// 一个订单允许多次部分退款；可退金额 = 订单金额 - 已成功退款合计。

type refundPaymentRow struct {
	ID              string
	UserID          string
	Channel         string
	AmountCents     int64
	Currency        string
	Status          string
	ProviderOrderID string
}

func (h *BillingHandler) refundPaymentForTenant(ctx context.Context, claims *auth.Claims, paymentID string) (*refundPaymentRow, error) {
	var p refundPaymentRow
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT p.id, p.user_id, p.channel, p.amount_cents, COALESCE(p.currency, ''),
		        p.status, COALESCE(p.provider_order_id, '')
		   FROM payments p JOIN users u ON u.id = p.user_id
		  WHERE p.id = $1 AND u.tenant_id = $2`,
		paymentID, claims.TenantID).
		Scan(&p.ID, &p.UserID, &p.Channel, &p.AmountCents, &p.Currency, &p.Status, &p.ProviderOrderID)
	return &p, err
}

// CreateRefund POST /v1/tenant/payments/{id}/refunds
func (h *BillingHandler) CreateRefund(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID <= 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	paymentID := strings.TrimSpace(r.PathValue("id"))
	if paymentID == "" {
		BadRequest(w, "payment id required")
		return
	}
	var body struct {
		AmountCents int64  `json:"amount_cents"`
		Reason      string `json:"reason"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.AmountCents <= 0 {
		BadRequest(w, "amount_cents must be positive")
		return
	}
	p, err := h.refundPaymentForTenant(r.Context(), claims, paymentID)
	if err != nil {
		NotFound(w, "payment not found")
		return
	}
	if p.Status != "paid" {
		BadRequest(w, "only paid orders can be refunded")
		return
	}
	refunded, err := billing.SumSucceededRefunds(r.Context(), paymentID)
	if err != nil {
		InternalError(w, "sum refunds failed")
		return
	}
	if refunded+body.AmountCents > p.AmountCents {
		BadRequest(w, fmt.Sprintf("refund amount exceeds remaining %d", p.AmountCents-refunded))
		return
	}

	ref := &billing.Refund{
		PaymentID:   paymentID,
		TenantID:    int(claims.TenantID),
		UserID:      p.UserID,
		Channel:     p.Channel,
		AmountCents: body.AmountCents,
		Currency:    p.Currency,
		Reason:      strings.TrimSpace(body.Reason),
		OperatorID:  claims.UserID,
	}
	if err := billing.CreateRefund(r.Context(), ref); err != nil {
		InternalError(w, "create refund failed")
		return
	}
	_ = billing.UpdateRefundStatus(r.Context(), ref.ID, billing.RefundProcessing, "", "")

	var channelRefundID string
	var refundErr error
	switch p.Channel {
	case "alipay":
		_, alipay, _ := h.clientsForPayment(r.Context(), paymentID)
		if alipay == nil {
			refundErr = fmt.Errorf("支付宝渠道未配置")
		} else {
			channelRefundID, refundErr = alipay.TradeRefund(r.Context(), p.ProviderOrderID, ref.ID, body.AmountCents, ref.Reason)
		}
	case "wechat":
		_, _, wechat := h.clientsForPayment(r.Context(), paymentID)
		if wechat == nil {
			refundErr = fmt.Errorf("微信支付渠道未配置")
		} else {
			channelRefundID, refundErr = wechat.Refund(r.Context(), p.ProviderOrderID, ref.ID, body.AmountCents, p.AmountCents, ref.Reason)
		}
	case "paypal":
		cfg := h.paymentConfigForTenant(r.Context(), int(claims.TenantID))
		if !cfg.PayPalEnabled || cfg.PayPalClientID == "" || cfg.PayPalSecret == "" {
			refundErr = fmt.Errorf("PayPal 渠道未配置")
		} else {
			channelRefundID, refundErr = h.payPalRefund(r.Context(), cfg, p.ProviderOrderID, body.AmountCents, p.Currency)
		}
	default:
		refundErr = fmt.Errorf("不支持的支付渠道: %s", p.Channel)
	}
	if refundErr != nil {
		_ = billing.UpdateRefundStatus(r.Context(), ref.ID, billing.RefundFailed, "", refundErr.Error())
		ref.Status = billing.RefundFailed
		ref.ErrorMessage = refundErr.Error()
		JSON(w, http.StatusBadGateway, APIResponse{Success: false, Error: refundErr.Error()})
		return
	}
	_ = billing.UpdateRefundStatus(r.Context(), ref.ID, billing.RefundSucceeded, channelRefundID, "")
	ref.Status = billing.RefundSucceeded
	ref.ChannelRefundID = channelRefundID
	OK(w, ref)
}

// ListTenantRefunds GET /v1/tenant/refunds?page=1&page_size=20
func (h *BillingHandler) ListTenantRefunds(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID <= 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	page, _ := strconv.Atoi(r.URL.Query().Get("page"))
	if page < 1 {
		page = 1
	}
	limit, _ := strconv.Atoi(r.URL.Query().Get("page_size"))
	items, total, err := billing.ListRefundsByTenant(r.Context(), int(claims.TenantID), limit, (page-1)*limit)
	if err != nil {
		InternalError(w, "list refunds failed")
		return
	}
	OK(w, map[string]interface{}{"refunds": items, "total": total, "page": page})
}

// ListPaymentRefunds GET /v1/tenant/payments/{id}/refunds
func (h *BillingHandler) ListPaymentRefunds(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID <= 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	paymentID := strings.TrimSpace(r.PathValue("id"))
	if _, err := h.refundPaymentForTenant(r.Context(), claims, paymentID); err != nil {
		NotFound(w, "payment not found")
		return
	}
	items, err := billing.ListRefundsByPayment(r.Context(), paymentID)
	if err != nil {
		InternalError(w, "list refunds failed")
		return
	}
	OK(w, map[string]interface{}{"refunds": items})
}
