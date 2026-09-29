package billing

import (
	"context"
	"log/slog"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5/pgxpool"
)

// EnterpriseBillingObserver 是把消费事件归集到 billing_records 的
// BillingObserver（企业成本中心）。写入完全异步（goroutine + recover），
// 任何失败仅 slog.Warn，绝不阻塞或影响主计费链路。
//
// 接线位置（集成任务）：gateway_router.go NewGatewayRouter 中
// billingMgr.Subscribe 处（现有 TransactionRecorder / BalanceSyncer 旁）：
//
//	billingMgr.Subscribe(billing.NewEnterpriseBillingObserver(db.Pool))
type EnterpriseBillingObserver struct {
	pool *pgxpool.Pool // nil = 事件时回退到全局 db.Pool
}

var _ BillingObserver = (*EnterpriseBillingObserver)(nil)

// NewEnterpriseBillingObserver 创建企业计费观察者。
// pool 为 nil 时事件处理回退到全局 db.Pool（沿用项目 PGStore 模式）。
func NewEnterpriseBillingObserver(pool *pgxpool.Pool) *EnterpriseBillingObserver {
	return &EnterpriseBillingObserver{pool: pool}
}

// OnCreditChange 异步写一条 billing_records 归集记录。
// 仅归集消费（扣减）事件；充值/退款等入账由 payments / credit_transactions 覆盖。
func (o *EnterpriseBillingObserver) OnCreditChange(evt CreditEvent) {
	if evt.Amount >= 0 {
		return
	}
	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Warn("enterprise billing observer panic", "panic", r, "user_id", evt.UserID)
			}
		}()
		o.record(evt)
	}()
}

// record 执行一次归集写入：tenant_id 经 users 表关联，
// group_id 取用户主群组（ent_group_members 第一条），无群组则 NULL。
func (o *EnterpriseBillingObserver) record(evt CreditEvent) {
	pool := o.pool
	if pool == nil {
		pool = db.Pool
	}
	if pool == nil {
		slog.Warn("enterprise billing record skipped: no postgres pool", "user_id", evt.UserID)
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// tenant_id：经 users 表关联查询（users.id → users.tenant_id）
	var tenantID string
	if err := pool.QueryRow(ctx,
		`SELECT tenant_id FROM users WHERE id = $1`, evt.UserID).Scan(&tenantID); err != nil {
		slog.Warn("enterprise billing record skipped: tenant lookup failed",
			"user_id", evt.UserID, "error", err)
		return
	}

	// group_id：用户主群组（ent_group_members 第一条，按 group_id 排序保证确定性）。
	// 无成员关系或其它查询错误均降级为 NULL 归集，不影响成本落账。
	var groupID *string
	_ = pool.QueryRow(ctx,
		`SELECT group_id FROM ent_group_members WHERE user_id = $1 ORDER BY group_id LIMIT 1`,
		evt.UserID).Scan(&groupID)

	// 1 credit = 1 分（与 payments.amount_cents 口径一致）
	costCents := -evt.Amount
	// A5 修复：token 两列写 **NULL** 而不是 0。
	//
	// `CreditEvent` 只携带金额（见该类型定义：UserID/Amount/Balance/Reason/Timestamp），
	// 因此这条记录**不包含** token 明细。原先写 0 会让读的人无法区分两种完全不同的情况：
	//   * `input_tokens = 0` → "这次调用真的用了 0 token"（可能）；
	//   * `input_tokens = 0` → "这条记录不含 token 信息"（实际语义）。
	// 两列在 baseline 里都是**可空**的（`input_tokens bigint`，无 NOT NULL），
	// 所以用 NULL 表达"不适用"既精确又不需要迁移。
	//
	// 读取侧（ent_costcenter_handler.go 的明细查询）已用 COALESCE 兜底为 0，
	// API 契约不变；聚合查询本就用 COALESCE(SUM(...),0)，NULL 天然安全。
	if _, err := pool.Exec(ctx,
		`INSERT INTO billing_records
			(tenant_id, user_id, session_id, input_tokens, output_tokens, cost_cents, group_id)
		 VALUES ($1, $2, NULL, NULL, NULL, $3, $4)`,
		tenantID, evt.UserID, costCents, groupID); err != nil {
		slog.Warn("enterprise billing record insert failed",
			"user_id", evt.UserID, "tenant_id", tenantID, "error", err)
	}
}
