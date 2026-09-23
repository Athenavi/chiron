package billing

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
)

// PGStore implements Store using the chiron PostgreSQL database.
// It uses the existing users table for balance and adds a new billing table.

type PGStore struct{}

func NewPGStore() *PGStore {
	return &PGStore{}
}

// EnsureTables creates the billing tables if they don't exist.
func (s *PGStore) EnsureTables(ctx context.Context) error {
	if db.Pool == nil {
		return nil // no database available, skip table initialization
	}

	// 只读自检放在 DDL 之前：schema 若由 DBA 管理（应用 DB 用户没有 DDL 权限），
	// 下面的 CREATE/ALTER 会直接失败返回，自检就没机会跑了。
	if issue := s.paymentUserIDIssue(ctx); issue != "" {
		slog.Error(issue, "table", "payments", "column", "user_id")
	}

	// Add balance column to users table if not exists
	_, err := db.GlobalDBManager.Exec(ctx,
		`ALTER TABLE users ADD COLUMN IF NOT EXISTS credits INTEGER NOT NULL DEFAULT 1000`)
	if err != nil {
		return fmt.Errorf("add credits column: %w", err)
	}

	// Create credit_transactions table
	// id / user_id 均按 36 字符 UUID 定长：历史 DDL 写成 32，与 init.sql（36）不一致，
	// 靠这里建表的库会在插入时报 value too long。
	_, err = db.GlobalDBManager.Exec(ctx,
		`CREATE TABLE IF NOT EXISTS credit_transactions (
			id VARCHAR(36) PRIMARY KEY,
			user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
			amount INTEGER NOT NULL,
			balance INTEGER NOT NULL,
			reason VARCHAR(64) NOT NULL,
			turn_id VARCHAR(36),
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
		)`)
	if err != nil {
		return fmt.Errorf("create credit_transactions: %w", err)
	}

	// Create payments table（支付宝/微信/PayPal 通用充值订单）
	// 注意 user_id 长度：用户 ID 是 36 字符 UUID，而这里（以及 init.sql / models.yaml）
	// 历史上写成 32，导致 PgStore.CreatePayment 一律报
	//   value too long for type character varying(32)
	// 充值下单全线失败。已有库需由 DBA 执行一次
	//   ALTER TABLE payments ALTER COLUMN user_id TYPE VARCHAR(36);
	// （应用用户通常不是表所有者，EnsureTables 改不动既有表。）
	_, err = db.GlobalDBManager.Exec(ctx,
		`CREATE TABLE IF NOT EXISTS payments (
			id VARCHAR(64) PRIMARY KEY,
			user_id VARCHAR(36) NOT NULL,
			channel VARCHAR(16) NOT NULL,
			credits INTEGER NOT NULL,
			amount_cents BIGINT NOT NULL DEFAULT 0,
			currency VARCHAR(8) NOT NULL DEFAULT 'CNY',
			status VARCHAR(16) NOT NULL DEFAULT 'pending',
			qr_code TEXT,
			provider_order_id VARCHAR(64) NOT NULL DEFAULT '',
			trade_no VARCHAR(64) NOT NULL DEFAULT '',
			created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			paid_at TIMESTAMPTZ,
			expired_at TIMESTAMPTZ
		)`)
	if err != nil {
		return fmt.Errorf("create payments: %w", err)
	}
	_, err = db.GlobalDBManager.Exec(ctx,
		`CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id, created_at DESC)`)
	if err != nil {
		return fmt.Errorf("create payments user index: %w", err)
	}
	_, err = db.GlobalDBManager.Exec(ctx,
		`CREATE INDEX IF NOT EXISTS idx_payments_provider ON payments(provider_order_id) WHERE provider_order_id <> ''`)
	if err != nil {
		return fmt.Errorf("create payments provider index: %w", err)
	}

	// Index for fast history lookups
	_, err = db.GlobalDBManager.Exec(ctx,
		`CREATE INDEX IF NOT EXISTS idx_credit_tx_user ON credit_transactions(user_id, created_at DESC)`)
	if err != nil {
		return fmt.Errorf("create index: %w", err)
	}

	// 回合维度幂等（B4）：credit_transactions.turn_id + 唯一索引。
	// 唯一索引允许多个 NULL（非 turn 场景的流水不受影响），
	// 扣费语句用 ON CONFLICT (turn_id) DO NOTHING 实现"同一回合只扣一次"。
	_, err = db.GlobalDBManager.Exec(ctx,
		`ALTER TABLE credit_transactions ADD COLUMN IF NOT EXISTS turn_id VARCHAR(36)`)
	if err != nil {
		return fmt.Errorf("add credit_transactions.turn_id: %w", err)
	}
	_, err = db.GlobalDBManager.Exec(ctx,
		`CREATE UNIQUE INDEX IF NOT EXISTS uniq_credit_tx_turn ON credit_transactions(turn_id)`)
	if err != nil {
		return fmt.Errorf("create uniq_credit_tx_turn: %w", err)
	}

	return nil
}

func (s *PGStore) GetBalance(ctx context.Context, userID string) (int, error) {
	var balance int
	// 主库：余额为钱包语义，扣减后/跨实例读取不允许依赖副本延迟
	err := db.Pool.QueryRow(ctx,
		`SELECT COALESCE(credits, 0) FROM users WHERE id = $1`, userID).Scan(&balance)
	if err != nil {
		return 0, fmt.Errorf("get user credits: %w", err)
	}
	return balance, nil
}

func (s *PGStore) SetBalance(ctx context.Context, userID string, balance int) error {
	_, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE users SET credits = $1 WHERE id = $2`, balance, userID)
	return err
}

func (s *PGStore) GetHistory(ctx context.Context, userID string, limit int) ([]CreditChange, error) {
	if limit <= 0 {
		limit = 50
	}

	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT id, user_id, amount, balance, reason, created_at
		 FROM credit_transactions WHERE user_id = $1 AND reason <> 'free_chat'
		 ORDER BY created_at DESC LIMIT $2`, userID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var result []CreditChange
	for rows.Next() {
		var tx CreditChange
		if err := rows.Scan(&tx.ID, &tx.UserID, &tx.Amount, &tx.Balance, &tx.Reason, &tx.CreatedAt); err != nil {
			slog.Warn("scan transaction row skipped", "error", err)
			continue
		}
		result = append(result, tx)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate transactions: %w", err)
	}
	return result, nil
}

// DailyFreeCount returns the number of free conversations used today (UTC).
func (s *PGStore) DailyFreeCount(ctx context.Context, userID string) (int, error) {
	var count int
	todayUTC := time.Now().UTC().Truncate(24 * time.Hour)
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*) FROM credit_transactions
		 WHERE user_id = $1 AND reason = 'free_chat' AND created_at >= $2`, userID, todayUTC).Scan(&count)
	if err != nil {
		return 0, err
	}
	return count, nil
}

// MarkFreeUsage records a free conversation usage for today.
// turnID 非空时按回合幂等（credit_transactions.turn_id 唯一索引）：同一回合重试
// 不会重复占用免费额度（此前重试会多记一次，见其余幂等项）。
func (s *PGStore) MarkFreeUsage(ctx context.Context, userID, turnID string) error {
	tx := &CreditChange{
		ID:        fmt.Sprintf("free_%d", time.Now().UnixNano()),
		UserID:    userID,
		Amount:    0,
		Balance:   0,
		Reason:    "free_chat",
		CreatedAt: time.Now(),
	}
	var tid *string
	if turnID != "" {
		tid = &turnID
	}
	_, err := db.GlobalDBManager.Exec(ctx,
		`INSERT INTO credit_transactions (id, user_id, amount, balance, reason, created_at, turn_id)
		 VALUES ($1, $2, $3, $4, $5, $6, $7)
		 ON CONFLICT (turn_id) DO NOTHING`,
		tx.ID, tx.UserID, tx.Amount, tx.Balance, tx.Reason, tx.CreatedAt, tid)
	return err
}

// RecordBillingRecord 写入一条企业成本中心记录（billing_records）。
// tenant_id 取自 users；group_id 取用户主群组（ent_group_members 首条，无则 NULL）。
// 单语句原子完成；用户不存在返回错误。调用方为扣费成功后的网关（submit 链路）。
func (s *PGStore) RecordBillingRecord(ctx context.Context, userID, sessionID string, inputTokens, outputTokens, costCents int, turnID string) error {
	var sid *string
	if sessionID != "" {
		sid = &sessionID
	}
	var tid *string
	if turnID != "" {
		tid = &turnID
	}
	tag, err := db.GlobalDBManager.Exec(ctx,
		`INSERT INTO billing_records (tenant_id, user_id, session_id, input_tokens, output_tokens, cost_cents, group_id, turn_id)
		 SELECT u.tenant_id, u.id, $2, $3, $4, $5,
		        (SELECT g.group_id FROM ent_group_members g
		          WHERE g.user_id = u.id ORDER BY g.group_id LIMIT 1),
		        $6
		 FROM users u WHERE u.id = $1
		 ON CONFLICT (turn_id) DO NOTHING`,
		userID, sid, inputTokens, outputTokens, costCents, tid)
	if err != nil {
		return fmt.Errorf("insert billing record: %w", err)
	}
	// turnID 非空时 0 行也可能是"该回合已记账"（幂等跳过），不算错误（B4）
	if tag.RowsAffected() == 0 && turnID == "" {
		return fmt.Errorf("billing record: user %s not found", userID)
	}
	return nil
}

// applyCreditTx 在同一事务内完成余额变更 + 流水落库：
// 余额以 PG 原子语句为唯一事实源，流水与余额同生共死，杜绝"已扣/已加未记流水"窗口。
// guardMin>0：仅当余额 >= guardMin 才允许（扣减防负）；否则无条件加减（充值/退款）。
func (s *PGStore) applyCreditTx(ctx context.Context, userID string, delta int, guardMin int, reason, turnID string) (int, error) {
	var newBalance int
	txID := fmt.Sprintf("tx_%d", time.Now().UnixNano())
	err := db.GlobalDBManager.WithTransaction(ctx, func(tx pgx.Tx) error {
		var q string
		args := []interface{}{delta, userID}
		// COALESCE 不可省：users.credits 列在部分环境里是 nullable 且无默认值
		// （由 init.sql/历史建表创建；EnsureTables 那句 ADD COLUMN ... NOT NULL DEFAULT
		// 在生产上会因"应用不是表所有者"而失败）。此时 `credits + $1` 结果是 NULL，
		// RETURNING 回来扫描进 int 会直接报 "cannot scan NULL into *int"，
		// 表现为"支付成功但余额不到账、订单却已标记 paid"。
		if guardMin > 0 {
			q = `UPDATE users SET credits = COALESCE(credits, 0) + $1
			      WHERE id = $2 AND COALESCE(credits, 0) >= $3 RETURNING credits`
			args = append(args, guardMin)
		} else {
			q = `UPDATE users SET credits = COALESCE(credits, 0) + $1 WHERE id = $2 RETURNING credits`
		}

		// 幂等扣费（B4）：turnID 非空时先用流水的唯一索引占位，
		// 占位失败＝该回合已扣过 => 不再改余额，直接返回当前余额（重试安全）。
		// turnID 为空时完全保持原有事务语义（零行为变更）。
		if turnID != "" {
			tag, err := tx.Exec(ctx,
				`INSERT INTO credit_transactions (id, user_id, amount, balance, reason, turn_id, created_at)
				 VALUES ($1, $2, $3, 0, $4, $5, NOW())
				 ON CONFLICT (turn_id) DO NOTHING`,
				txID, userID, delta, reason, turnID)
			if err != nil {
				return fmt.Errorf("claim credit tx for turn: %w", err)
			}
			if tag.RowsAffected() == 0 {
				return tx.QueryRow(ctx, `SELECT COALESCE(credits, 0) FROM users WHERE id = $1`, userID).Scan(&newBalance)
			}
			if err := tx.QueryRow(ctx, q, args...).Scan(&newBalance); err != nil {
				return fmt.Errorf("apply credit balance: %w", err)
			}
			if _, err := tx.Exec(ctx,
				`UPDATE credit_transactions SET balance = $2 WHERE id = $1`, txID, newBalance); err != nil {
				return fmt.Errorf("update credit transaction balance: %w", err)
			}
			return nil
		}

		if err := tx.QueryRow(ctx, q, args...).Scan(&newBalance); err != nil {
			return fmt.Errorf("apply credit balance: %w", err)
		}
		_, err := tx.Exec(ctx,
			`INSERT INTO credit_transactions (id, user_id, amount, balance, reason, created_at)
			 VALUES ($1, $2, $3, $4, $5, NOW())`,
			txID, userID, delta, newBalance, reason)
		if err != nil {
			return fmt.Errorf("insert credit transaction: %w", err)
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return newBalance, nil
}

// AtomicDeductBalance 在同一事务内扣减余额并写入流水（reason）。
// 余额不足/用户不存在返回错误。多副本部署下不超扣、不重复扣费、流水不缺失。
func (s *PGStore) AtomicDeductBalance(ctx context.Context, userID string, amount int, reason, turnID string) (int, error) {
	if amount <= 0 {
		return 0, fmt.Errorf("invalid deduction amount: %d", amount)
	}
	b, err := s.applyCreditTx(ctx, userID, -amount, amount, reason, turnID)
	if err != nil {
		return 0, fmt.Errorf("atomic deduct failed (insufficient credits or user not found): %w", err)
	}
	return b, nil
}

// AtomicAddBalance 在同一事务内增加余额并写入流水（reason）。
// Returns the new balance, or an error if user not found.
func (s *PGStore) AtomicAddBalance(ctx context.Context, userID string, amount int, reason string) (int, error) {
	if amount <= 0 {
		return 0, fmt.Errorf("invalid add amount: %d", amount)
	}
	return s.applyCreditTx(ctx, userID, amount, 0, reason, "")
}

// JSON serialization helpers for API responses
type BalanceResponse struct {
	UserID  string `json:"user_id"`
	Balance int    `json:"balance"`
}

func FormatBalance(userID string, balance int) string {
	data, _ := json.Marshal(BalanceResponse{UserID: userID, Balance: balance})
	return string(data)
}

// ── PaymentStore ──────────────────────────────────────────────────────────

const _paymentColumns = `id, user_id, channel, credits, amount_cents, currency, status,
	COALESCE(qr_code, ''), provider_order_id, trade_no, created_at, paid_at, expired_at`

func scanPayment(row interface{ Scan(...any) error }) (*Payment, error) {
	var p Payment
	var qr string
	var paidAt, expiredAt *time.Time
	err := row.Scan(&p.ID, &p.UserID, &p.Channel, &p.Credits, &p.AmountCents, &p.Currency,
		&p.Status, &qr, &p.ProviderOrderID, &p.TradeNo, &p.CreatedAt, &paidAt, &expiredAt)
	if err != nil {
		return nil, err
	}
	p.QRCode = qr
	p.PaidAt = paidAt
	p.ExpiredAt = expiredAt
	return &p, nil
}

// paymentUserIDIssue 检测 payments.user_id 是否短于 36 字符的用户 UUID，
// 返回可直接执行的修复语句作为诊断；无需修复时返回空串。
//
// 历史 schema（init.sql / models.yaml / 本文件的建表 DDL）曾把它定为 VARCHAR(32)，
// 而用户 ID 是 36 字符 UUID，于是 PgStore.CreatePayment 一律报
//
//	value too long for type character varying(32)
//
// 充值下单全线失败，且只在用户点「立即充值」时以笼统的 500 暴露。
//
// 这类历史漂移无法靠 CREATE TABLE IF NOT EXISTS 修正（表已存在时它是空操作），
// 应用 DB 用户通常也不是表所有者（ALTER 会报 must be owner of table），
// 因此只做只读检查，把修复语句交给 DBA。
func (s *PGStore) paymentUserIDIssue(ctx context.Context) string {
	const required = 36
	var maxLen *int
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT character_maximum_length FROM information_schema.columns
		  WHERE table_schema = current_schema() AND table_name = 'payments' AND column_name = 'user_id'`).Scan(&maxLen)
	if err != nil || maxLen == nil {
		// 表尚未建立 / 列为 TEXT 无长度限制 / 查询失败：都无需告警
		return ""
	}
	if *maxLen < required {
		return fmt.Sprintf(
			"payments.user_id 长度 %d 不足以容纳 %d 字符的用户 UUID，充值下单会失败；请由 DBA 执行："+
				"ALTER TABLE payments ALTER COLUMN user_id TYPE VARCHAR(36)", *maxLen, required)
	}
	return ""
}

func (s *PGStore) CreatePayment(ctx context.Context, p *Payment) error {
	_, err := db.GlobalDBManager.Exec(ctx,
		`INSERT INTO payments (id, user_id, channel, credits, amount_cents, currency, status,
			qr_code, provider_order_id, trade_no, created_at, paid_at, expired_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)`,
		p.ID, p.UserID, p.Channel, p.Credits, p.AmountCents, p.Currency, p.Status,
		p.QRCode, p.ProviderOrderID, p.TradeNo, p.CreatedAt, p.PaidAt, p.ExpiredAt)
	return err
}

func (s *PGStore) GetPayment(ctx context.Context, id string) (*Payment, error) {
	// 主库：支付回调确认前读取订单状态，不允许副本延迟导致 "unknown order"
	row := db.Pool.QueryRow(ctx,
		`SELECT `+_paymentColumns+` FROM payments WHERE id = $1`, id)
	return scanPayment(row)
}

func (s *PGStore) GetPaymentByProviderOrderID(ctx context.Context, providerOrderID string) (*Payment, error) {
	row := db.GlobalDBManager.QueryRow(ctx,
		`SELECT `+_paymentColumns+` FROM payments WHERE provider_order_id = $1`, providerOrderID)
	return scanPayment(row)
}

// MarkPaymentPaid 幂等推进 pending→paid。返回 nil 表示订单非 pending（已处理/不存在）。
func (s *PGStore) MarkPaymentPaid(ctx context.Context, id, tradeNo string) (*Payment, error) {
	row := db.GlobalDBManager.QueryRow(ctx,
		`UPDATE payments SET status = 'paid', trade_no = $2, paid_at = NOW()
		 WHERE id = $1 AND status = 'pending'
		 RETURNING `+_paymentColumns,
		id, tradeNo)
	p, err := scanPayment(row)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil // 已处理或不存在
		}
		return nil, err
	}
	return p, nil
}

func (s *PGStore) MarkPaymentFailed(ctx context.Context, id string) error {
	_, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE payments SET status = 'failed' WHERE id = $1 AND status = 'pending'`, id)
	return err
}

// RevertPaymentToPending 把订单从 paid 退回 pending。
// 仅用于「已标记支付成功、但入账失败」的补偿：让下一次查询/回调重新走一遍入账，
// 否则 MarkPaymentPaid 的幂等分支会永远跳过这笔订单（钱付了、余额却永远不动）。
func (s *PGStore) RevertPaymentToPending(ctx context.Context, id string) error {
	_, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE payments SET status = 'pending', paid_at = NULL WHERE id = $1 AND status = 'paid'`, id)
	return err
}

func (s *PGStore) UpdatePaymentProvider(ctx context.Context, id, qrCode, providerOrderID string) error {
	_, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE payments SET qr_code = $2, provider_order_id = $3 WHERE id = $1 AND status = 'pending'`,
		id, qrCode, providerOrderID)
	return err
}
