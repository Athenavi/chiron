package billing

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
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

// VerifySchema 只读校验计费相关的表与列是否齐全。
//
// P0-3 之后 schema 的唯一权威是 Alembic（migrations/versions/0001_authoritative_baseline.py）：
// 应用**不再执行任何 DDL** —— 既不需要 DDL 权限，也不会产生与迁移并存的第二份真相。
// 这里把「表缺失」从「首次下单时笼统 500」提前成启动即可见的明确告警。
//
// 历史上这里叫 EnsureTables，会 CREATE TABLE / ALTER TABLE 兜底建表。那些 DDL 与
// Alembic 迁移长期并存并已漂移（payments.user_id 在一处是 32、另一处是 36，库由哪条
// 路径建出来决定了充值能否成功）。收敛到单一权威后这类漂移不再可能发生。
func (s *PGStore) VerifySchema(ctx context.Context) error {
	if db.Pool == nil {
		return nil
	}

	// 只读自检：payments.user_id 长度曾因历史 DDL 写成 32 而让充值全线失败
	if issue := s.paymentUserIDIssue(ctx); issue != "" {
		slog.Error(issue, "table", "payments", "column", "user_id")
	}

	var missing []string
	for _, table := range []string{"credit_transactions", "payments"} {
		exists, err := s.tableExists(ctx, table)
		if err != nil {
			return err
		}
		if !exists {
			missing = append(missing, table)
		}
	}

	var hasCredits bool
	if err := db.Pool.QueryRow(ctx, `
		SELECT EXISTS (
			SELECT 1 FROM information_schema.columns
			 WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'credits'
		)`).Scan(&hasCredits); err != nil {
		return fmt.Errorf("check users.credits: %w", err)
	}
	if !hasCredits {
		missing = append(missing, "users.credits")
	}

	if len(missing) > 0 {
		return fmt.Errorf("billing schema incomplete (missing: %s) — run: alembic upgrade head",
			strings.Join(missing, ", "))
	}
	return nil
}

// tableExists 只读判断 public schema 下的表是否存在。
func (s *PGStore) tableExists(ctx context.Context, table string) (bool, error) {
	var exists bool
	if err := db.Pool.QueryRow(ctx,
		`SELECT to_regclass($1) IS NOT NULL`, "public."+table).Scan(&exists); err != nil {
		return false, fmt.Errorf("check table %s: %w", table, err)
	}
	return exists, nil
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
		// （由权威迁移创建；历史上的 ADD COLUMN ... NOT NULL DEFAULT
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
