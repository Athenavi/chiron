package billing

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/jackc/pgx/v5"
)

// payment_refunds.go —— 支付退款记录（租户级）。
//
// 一个订单允许多次部分退款：每次一条记录，金额不可超过订单金额与已成功退款之和的差额。
// 渠道调用是同步的（支付宝/微信/PayPal 的退款申请接口都返回受理/成功结果），
// 失败时保留记录并把渠道错误写进 error_message，便于重试与排查。

const (
	RefundPending    = "pending"
	RefundProcessing = "processing"
	RefundSucceeded  = "succeeded"
	RefundFailed     = "failed"
)

// Refund 是一次退款申请/结果。
type Refund struct {
	ID              string    `json:"id"`
	PaymentID       string    `json:"payment_id"`
	TenantID        int       `json:"tenant_id"`
	UserID          string    `json:"user_id"`
	Channel         string    `json:"channel"`
	AmountCents     int64     `json:"amount_cents"`
	Currency        string    `json:"currency"`
	Reason          string    `json:"reason"`
	Status          string    `json:"status"`
	OperatorID      string    `json:"operator_id,omitempty"`
	ChannelRefundID string    `json:"channel_refund_id,omitempty"`
	ErrorMessage    string    `json:"error_message,omitempty"`
	CreatedAt       time.Time `json:"created_at"`
	UpdatedAt       time.Time `json:"updated_at"`
}

const refundColumns = `id, payment_id, tenant_id, user_id, channel, amount_cents, currency,
	reason, status, COALESCE(operator_id, ''),
	COALESCE(channel_refund_id, ''), COALESCE(error_message, ''),
	created_at, updated_at`

func scanRefund(row pgx.Row) (*Refund, error) {
	var r Refund
	if err := row.Scan(&r.ID, &r.PaymentID, &r.TenantID, &r.UserID, &r.Channel,
		&r.AmountCents, &r.Currency, &r.Reason, &r.Status, &r.OperatorID,
		&r.ChannelRefundID, &r.ErrorMessage, &r.CreatedAt, &r.UpdatedAt); err != nil {
		return nil, err
	}
	return &r, nil
}

// CreateRefund 写入一条退款记录（状态默认 pending）。
func CreateRefund(ctx context.Context, r *Refund) error {
	if r.ID == "" {
		uid, err := id.UUID()
		if err != nil {
			return err
		}
		r.ID = uid
	}
	if r.Currency == "" {
		r.Currency = "CNY"
	}
	if r.Status == "" {
		r.Status = RefundPending
	}
	now := time.Now()
	if _, err := db.GlobalDBManager.Exec(ctx, `INSERT INTO payment_refunds
		(id, payment_id, tenant_id, user_id, channel, amount_cents, currency, reason,
		 status, operator_id, channel_refund_id, error_message, created_at, updated_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$13)`,
		r.ID, r.PaymentID, r.TenantID, r.UserID, r.Channel, r.AmountCents, r.Currency,
		r.Reason, r.Status, r.OperatorID, r.ChannelRefundID, r.ErrorMessage, now); err != nil {
		return fmt.Errorf("insert refund: %w", err)
	}
	r.CreatedAt, r.UpdatedAt = now, now
	return nil
}

// GetRefund 按 id 取退款记录。
func GetRefund(ctx context.Context, refundID string) (*Refund, error) {
	r, err := scanRefund(db.GlobalDBManager.QueryRow(ctx,
		`SELECT `+refundColumns+` FROM payment_refunds WHERE id = $1`, refundID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}
	return r, nil
}

// ListRefundsByTenant 分页列出租户退款（按创建时间倒序）。
func ListRefundsByTenant(ctx context.Context, tenantID, limit, offset int) ([]*Refund, int, error) {
	if limit <= 0 {
		limit = 20
	}
	if limit > 200 {
		limit = 200
	}
	if offset < 0 {
		offset = 0
	}
	var total int
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*)::int FROM payment_refunds WHERE tenant_id = $1`, tenantID).Scan(&total); err != nil {
		return nil, 0, err
	}
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT `+refundColumns+` FROM payment_refunds
		 WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3`,
		tenantID, limit, offset)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	out := make([]*Refund, 0)
	for rows.Next() {
		r, err := scanRefund(rows)
		if err != nil {
			return nil, 0, err
		}
		out = append(out, r)
	}
	return out, total, rows.Err()
}

// ListRefundsByPayment 列出某订单的退款记录。
func ListRefundsByPayment(ctx context.Context, paymentID string) ([]*Refund, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT `+refundColumns+` FROM payment_refunds
		 WHERE payment_id = $1 ORDER BY created_at DESC`, paymentID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]*Refund, 0)
	for rows.Next() {
		r, err := scanRefund(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// UpdateRefundStatus 更新退款状态与渠道结果。
func UpdateRefundStatus(ctx context.Context, refundID, status, channelRefundID, errMsg string) error {
	_, err := db.GlobalDBManager.Exec(ctx, `UPDATE payment_refunds
		SET status = $2,
		    channel_refund_id = CASE WHEN $3 = '' THEN channel_refund_id ELSE $3 END,
		    error_message = $4,
		    updated_at = NOW()
		WHERE id = $1`, refundID, status, channelRefundID, errMsg)
	return err
}

// SumSucceededRefunds 已成功退款的金额合计（用于可退余额校验）。
func SumSucceededRefunds(ctx context.Context, paymentID string) (int64, error) {
	var total int64
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(SUM(amount_cents), 0)::bigint FROM payment_refunds
		 WHERE payment_id = $1 AND status = 'succeeded'`, paymentID).Scan(&total)
	return total, err
}
