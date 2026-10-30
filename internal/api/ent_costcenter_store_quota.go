package api

import (
	"context"
	"errors"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
)

// ent_costcenter_store_quota.go —— 成本中心的**配额与用量**存储层。
//
// 由 ent_costcenter_handler.go 按职责拆出（L3-4 批次 A，同包纯移动，逐字未改）：
// 原文件 1135 行同时装着类型、store 实现与 12 个 HTTP handler，改一处配额逻辑
// 得在 1100 行里定位。这里只放 `pgEntCostStore` 上以配额/用量为主题的查询方法，
// 边界是「全部以 `func (s *pgEntCostStore)` 开头」—— 机械可分，不涉及行为。

// ── 配额池 CRUD ──

const quotaPoolColumns = `id, tenant_id, resource_type, total_amount, period, created_at, updated_at`

func scanQuotaPool(row interface{ Scan(...any) error }) (*EntQuotaPool, error) {
	var p EntQuotaPool
	if err := row.Scan(&p.ID, &p.TenantID, &p.ResourceType, &p.TotalAmount, &p.Period,
		&p.CreatedAt, &p.UpdatedAt); err != nil {
		return nil, err
	}
	return &p, nil
}

func (s *pgEntCostStore) ListQuotaPools(ctx context.Context, tenantID string) ([]EntQuotaPool, error) {
	query := `SELECT ` + quotaPoolColumns + ` FROM ent_quota_pools`
	args := []any{}
	if tenantID != "" {
		query += ` WHERE tenant_id = $1`
		args = append(args, tenantID)
	}
	query += ` ORDER BY created_at`
	rows, err := db.GlobalDBManager.Query(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	pools := []EntQuotaPool{}
	for rows.Next() {
		p, err := scanQuotaPool(rows)
		if err != nil {
			return nil, err
		}
		pools = append(pools, *p)
	}
	return pools, rows.Err()
}

func (s *pgEntCostStore) GetQuotaPool(ctx context.Context, id string) (*EntQuotaPool, error) {
	p, err := scanQuotaPool(db.GlobalDBManager.QueryRow(ctx,
		`SELECT `+quotaPoolColumns+` FROM ent_quota_pools WHERE id = $1`, id))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, errQuotaNotFound
		}
		return nil, err
	}
	return p, nil
}

func (s *pgEntCostStore) CreateQuotaPool(ctx context.Context, p *EntQuotaPool) error {
	row := db.GlobalDBManager.QueryRow(ctx,
		`INSERT INTO ent_quota_pools (tenant_id, resource_type, total_amount, period)
		 VALUES ($1, $2, $3, $4) RETURNING `+quotaPoolColumns,
		p.TenantID, p.ResourceType, p.TotalAmount, p.Period)
	created, err := scanQuotaPool(row)
	if err != nil {
		if isUniqueViolation(err) {
			return errQuotaConflict
		}
		return err
	}
	*p = *created
	return nil
}

func (s *pgEntCostStore) UpdateQuotaPool(ctx context.Context, p *EntQuotaPool) error {
	row := db.GlobalDBManager.QueryRow(ctx,
		`UPDATE ent_quota_pools
		 SET resource_type = $2, total_amount = $3, period = $4, updated_at = NOW()
		 WHERE id = $1 RETURNING `+quotaPoolColumns,
		p.ID, p.ResourceType, p.TotalAmount, p.Period)
	updated, err := scanQuotaPool(row)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return errQuotaNotFound
		}
		if isUniqueViolation(err) {
			return errQuotaConflict
		}
		return err
	}
	*p = *updated
	return nil
}

func (s *pgEntCostStore) DeleteQuotaPool(ctx context.Context, id string) error {
	tag, err := db.GlobalDBManager.Exec(ctx, `DELETE FROM ent_quota_pools WHERE id = $1`, id)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return errQuotaNotFound
	}
	return nil
}

// ── 配额分配 ──

func (s *pgEntCostStore) ListAllocations(ctx context.Context, poolID string) ([]EntQuotaAllocation, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT id, pool_id, target_type, target_id, amount, created_at
		 FROM ent_quota_allocations WHERE pool_id = $1 ORDER BY created_at`, poolID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	allocs := []EntQuotaAllocation{}
	for rows.Next() {
		var a EntQuotaAllocation
		if err := rows.Scan(&a.ID, &a.PoolID, &a.TargetType, &a.TargetID, &a.Amount, &a.CreatedAt); err != nil {
			return nil, err
		}
		allocs = append(allocs, a)
	}
	return allocs, rows.Err()
}

func (s *pgEntCostStore) SumAllocated(ctx context.Context, poolID string) (int64, error) {
	var sum int64
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(SUM(amount),0) FROM ent_quota_allocations WHERE pool_id = $1`, poolID).
		Scan(&sum)
	return sum, err
}

func (s *pgEntCostStore) CreateAllocation(ctx context.Context, a *EntQuotaAllocation) error {
	row := db.GlobalDBManager.QueryRow(ctx,
		`INSERT INTO ent_quota_allocations (pool_id, target_type, target_id, amount)
		 VALUES ($1, $2, $3, $4)
		 RETURNING id, pool_id, target_type, target_id, amount, created_at`,
		a.PoolID, a.TargetType, a.TargetID, a.Amount)
	if err := row.Scan(&a.ID, &a.PoolID, &a.TargetType, &a.TargetID, &a.Amount, &a.CreatedAt); err != nil {
		if isUniqueViolation(err) {
			return errQuotaConflict
		}
		return err
	}
	return nil
}

func (s *pgEntCostStore) DeleteAllocation(ctx context.Context, poolID, id string) (bool, error) {
	tag, err := db.GlobalDBManager.Exec(ctx,
		`DELETE FROM ent_quota_allocations WHERE id = $1 AND pool_id = $2`, id, poolID)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() > 0, nil
}

// ── quota 强制 / 用量支撑查询 ──

func (s *pgEntCostStore) TenantTokenPools(ctx context.Context, tenantID string) ([]EntQuotaPool, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT `+quotaPoolColumns+` FROM ent_quota_pools
		 WHERE tenant_id = $1 AND resource_type = 'token' ORDER BY created_at`, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	pools := []EntQuotaPool{}
	for rows.Next() {
		p, err := scanQuotaPool(rows)
		if err != nil {
			return nil, err
		}
		pools = append(pools, *p)
	}
	return pools, rows.Err()
}

func (s *pgEntCostStore) TokenUsageSQL(ctx context.Context, tenantID string, since time.Time) (int64, error) {
	var used int64
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(SUM(input_tokens + output_tokens),0)
		 FROM billing_records WHERE tenant_id = $1 AND created_at >= $2`, tenantID, since).
		Scan(&used)
	return used, err
}

// TenantCreditPools 取该租户的 **credits** 配额池（M24）。
//
// 与 `TenantTokenPools` 只差 `resource_type`：`ent_quota_pools` 一直允许 credits
// （后台「成本中心」的下拉里就有"积分"），但执行侧此前只查 token ⇒ 管理员配的成本池
// **永远不会被检查**。
func (s *pgEntCostStore) TenantCreditPools(ctx context.Context, tenantID string) ([]EntQuotaPool, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT `+quotaPoolColumns+` FROM ent_quota_pools
		 WHERE tenant_id = $1 AND resource_type = 'credits' ORDER BY created_at`, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	pools := []EntQuotaPool{}
	for rows.Next() {
		p, err := scanQuotaPool(rows)
		if err != nil {
			return nil, err
		}
		pools = append(pools, *p)
	}
	return pools, rows.Err()
}

// CreditUsageSQL 按**扣减量**求和：`credit_transactions` 里 `amount < 0` 的绝对值之和。
//
// 单位是 credits（与用户余额、流水同源），**不是** `billing_records.cost_cents` 的"分"
// —— 两个量在库里并存，混用会让"已用量"与管理员看到的账单对不上。
// 该表只有 `user_id`，租户维度必须 join `users`（与 `ResolveTenantID` 同源）。
func (s *pgEntCostStore) CreditUsageSQL(ctx context.Context, tenantID int16, since time.Time) (int64, error) {
	var used int64
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(SUM(-ct.amount), 0)
		   FROM credit_transactions ct
		   JOIN users u ON u.id = ct.user_id
		  WHERE u.tenant_id = $1 AND ct.created_at >= $2 AND ct.amount < 0`,
		tenantID, since).Scan(&used)
	return used, err
}

func (s *pgEntCostStore) ResolveTenantID(ctx context.Context, userID string) (int16, error) {
	var tenantID int16
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT tenant_id FROM users WHERE id = $1`, userID).Scan(&tenantID)
	if err != nil {
		return 0, err
	}
	return tenantID, nil
}
