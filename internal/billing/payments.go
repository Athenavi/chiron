package billing

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/athenavi/chiron/internal/id"
	"github.com/athenavi/chiron/internal/notify"
)

// 支付渠道
const (
	ChannelAlipay = "alipay" // 支付宝当面付（Native 扫码）
	ChannelWechat = "wechat" // 微信 Native 扫码
	ChannelPayPal = "paypal" // PayPal（保留）
)

// 订单状态
const (
	PayStatusPending = "pending" // 已创建，等待支付
	PayStatusPaid    = "paid"    // 支付成功且已入账
	PayStatusFailed  = "failed"  // 支付失败
	PayStatusExpired = "expired" // 超时未支付
)

// Payment 是一笔充值订单（支付宝/微信/PayPal 共用）。
// 金额以"分"为单位：支付宝/微信为人民币分（CNY），PayPal 为美元分（USD）。
type Payment struct {
	ID              string     `json:"id"`
	UserID          string     `json:"user_id"`
	Channel         string     `json:"channel"`
	Credits         int        `json:"credits"`
	AmountCents     int64      `json:"amount_cents"`
	Currency        string     `json:"currency"`
	Status          string     `json:"status"`
	QRCode          string     `json:"qr_code,omitempty"`           // 扫码支付二维码内容
	ProviderOrderID string     `json:"provider_order_id,omitempty"` // 渠道侧订单号（out_trade_no / paypal order id）
	TradeNo         string     `json:"trade_no,omitempty"`          // 渠道交易流水号（支付成功后回填）
	// PackageCode 非空表示这是一笔套餐订单（购买后一次性发放 credits + 权益快照）。
	// 空表示纯充值（存量订单都是这一类）。
	PackageCode string     `json:"package_code,omitempty"`
	CreatedAt   time.Time  `json:"created_at"`
	PaidAt      *time.Time `json:"paid_at,omitempty"`
	ExpiredAt   *time.Time `json:"expired_at,omitempty"`
}

// PaymentStore 是支付订单的持久化接口。
type PaymentStore interface {
	CreatePayment(ctx context.Context, p *Payment) error
	GetPayment(ctx context.Context, id string) (*Payment, error)
	// GetPaymentByProviderOrderID 供渠道回调按第三方订单号定位（支付宝回调无内部订单号时可回查）
	GetPaymentByProviderOrderID(ctx context.Context, providerOrderID string) (*Payment, error)
	// MarkPaymentPaid 幂等推进 pending→paid；若订单非 pending 返回 (nil, nil) 表示已处理。
	// 成功返回订单（含 credits/user_id，供入账）。
	MarkPaymentPaid(ctx context.Context, id, tradeNo string) (*Payment, error)
	MarkPaymentFailed(ctx context.Context, id string) error
	// RevertPaymentToPending 把「已标记 paid 但入账失败」的订单退回 pending，
	// 使后续查询/回调能重新入账（否则幂等分支会永远跳过它）。
	RevertPaymentToPending(ctx context.Context, id string) error
	// UpdatePaymentProvider 预下单成功后回填二维码与渠道订单号。
	UpdatePaymentProvider(ctx context.Context, id, qrCode, providerOrderID string) error
}

// NewPayment 创建一笔 pending 订单。
func NewPayment(userID, channel string, credits int, amountCents int64, currency string) *Payment {
	now := time.Now()
	exp := now.Add(2 * time.Hour)
	return &Payment{
		ID:          "pay_" + id.NextID(),
		UserID:      userID,
		Channel:     channel,
		Credits:     credits,
		AmountCents: amountCents,
		Currency:    currency,
		Status:      PayStatusPending,
		CreatedAt:   now,
		ExpiredAt:   &exp,
	}
}

// ConfirmPayment 幂等确认一笔支付成功并入账：
//  1. 订单 pending→paid 原子推进（重复回调/轮询只入账一次）；
//  2. 推进成功后向用户加 credits（reason = "<channel>_topup"）。
//
// 返回 (payment, credited, err)：credited=false 表示订单此前已入账（幂等命中）。
func (m *Manager) ConfirmPayment(ctx context.Context, id, tradeNo string) (*Payment, bool, error) {
	p, err := m.store.MarkPaymentPaid(ctx, id, tradeNo)
	if err != nil {
		return nil, false, fmt.Errorf("mark payment paid: %w", err)
	}
	if p == nil {
		// 已处理（幂等）——回查当前状态确认
		cur, err := m.store.GetPayment(ctx, id)
		if err != nil {
			return nil, false, fmt.Errorf("get payment: %w", err)
		}
		return cur, false, nil
	}

	reason := p.Channel + "_topup"
	if _, err := m.AddCredits(p.UserID, reason, p.Credits); err != nil {
		// 入账失败必须把订单退回 pending。
		//
		// MarkPaymentPaid 与 AddCredits 不在同一事务里：若只让订单停在 paid，后续每次
		// 查询/回调都会走 MarkPaymentPaid 的幂等分支（返回 nil → 直接返回），这笔支付
		// 将**永远不入账且无法自愈**。线上表现就是"钱付了、订单显示已支付、余额不动"。
		if rErr := m.store.RevertPaymentToPending(ctx, id); rErr != nil {
			slog.Error("revert payment to pending failed; order is stuck as paid without credit",
				"order", id, "user", p.UserID, "credits", p.Credits, "trade_no", tradeNo, "error", rErr)
		} else {
			slog.Warn("credit failed; payment reverted to pending for retry",
				"order", id, "user", p.UserID, "credits", p.Credits, "error", err)
		}
		return p, false, fmt.Errorf("credit after payment: %w", err)
	}
	// credits 已到账，此后**只做不发错事**的补发：套餐订单发放权益快照。
	//
	// 刻意不把它的错误向上返回：此时 AddCredits 已成功，把订单退回 pending 会让下一次
	// 回调再发一次 credits（付一次、加两次）。权益发放失败只告警，靠幂等重放补
	// （GrantEntitlement 撞 23505 即视为已发）。
	ent := m.grantEntitlementForPayment(ctx, p)
	// 通知放在最后，且失败不影响返回值：此时钱已入账、权益已发放，
	// 因为"通知没写进去"而报错会让调用方以为支付失败。
	m.notifyPaymentSucceeded(ctx, p, ent)
	return p, true, nil
}

// notifyPaymentSucceeded 发「支付成功」站内信。
//
// 去重键只用订单 ID：`MarkPaymentPaid` 的幂等分支已经保证同一订单只会走到这里
// 一次，唯一索引在此是第二道保险（并发回调、事务重放）。
func (m *Manager) notifyPaymentSucceeded(ctx context.Context, p *Payment, ent *Entitlement) {
	if p == nil {
		return
	}
	payload := map[string]any{
		"payment_id":   p.ID,
		"credits":      p.Credits,
		"amount_cents": p.AmountCents,
		"currency":     p.Currency,
		"channel":      p.Channel,
	}
	title := "支付成功"
	body := fmt.Sprintf("充值 %d credits 已到账。", p.Credits)
	if p.PackageCode != "" {
		name := p.PackageCode
		if ent != nil && ent.PackageName != "" {
			name = ent.PackageName
		}
		payload["package_code"] = p.PackageCode
		title = "套餐购买成功"
		body = fmt.Sprintf("套餐「%s」已生效，%d credits 已到账。", name, p.Credits)
		if ent != nil {
			body += fmt.Sprintf("权益有效期至 %s。", ent.ExpiresAt.Format("2006-01-02"))
			payload["entitlement_id"] = ent.ID
			payload["expires_at"] = ent.ExpiresAt.Format(time.RFC3339)
		}
	}
	m.emitNotification(ctx, &notify.Notification{
		UserID:    p.UserID,
		Kind:      notify.KindPaymentSucceeded,
		DedupeKey: notify.KindPaymentSucceeded + ":" + p.ID,
		Title:     title,
		Body:      body,
		Link:      "/billing",
		Payload:   payload,
	})
}
